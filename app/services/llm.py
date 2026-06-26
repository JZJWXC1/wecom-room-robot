import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.services.config_check import is_missing_or_placeholder
from app.services.kf_contracts import safe_artifact_payload
from app.services.kf_llm1_task_packet import (
    build_kf_task_packet_prompt_artifact,
    build_kf_task_packet_shadow,
)
from app.services.rule_knowledge import RuleKnowledgeService


class ReplyGenerator:
    def __init__(
        self,
        *,
        rule_knowledge: RuleKnowledgeService | None = None,
    ) -> None:
        default_provider = "dashscope"
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key_for(default_provider) or "missing-key",
            base_url=settings.llm_base_url_for(default_provider),
        )
        self._clients: dict[str, AsyncOpenAI] = {default_provider: self._client}
        self.rule_knowledge = rule_knowledge or RuleKnowledgeService()

    def _client_for_stage(self, stage: str, *, retry: bool = False) -> AsyncOpenAI:
        provider = settings.llm_provider_for(stage, retry=retry)
        if provider == "dashscope":
            return self._client
        client = self._clients.get(provider)
        if client is None:
            client = AsyncOpenAI(
                api_key=settings.llm_api_key_for(provider) or "missing-key",
                base_url=settings.llm_base_url_for(provider),
            )
            self._clients[provider] = client
        return client

    @staticmethod
    def _stage_model(stage: str, *, retry: bool = False) -> str:
        return settings.llm_model_for(stage, retry=retry)

    @staticmethod
    def _stage_api_key_missing(stage: str, *, retry: bool = False) -> bool:
        provider = settings.llm_provider_for(stage, retry=retry)
        return is_missing_or_placeholder(settings.llm_api_key_for(provider))

    async def build_kf_task_packet(
        self,
        *,
        content: str,
        raw_dialog_context: list[dict[str, Any]] | None = None,
        structured_memory: dict[str, Any] | None = None,
        inventory_index: dict[str, Any] | None = None,
        candidate_set: dict[str, Any] | list[dict[str, Any]] | None = None,
        legacy_rewrite: dict[str, Any] | None = None,
        legacy_planner: dict[str, Any] | None = None,
        planner_feedback: dict[str, Any] | None = None,
        conversation_id: str = "",
        turn_id: str = "",
        case_id: str = "",
    ):
        """LLM1 shadow：只输出 StructuredTaskPacket，不生成客户可见回复。"""
        if self._stage_api_key_missing("rewrite"):
            return build_kf_task_packet_shadow(
                None,
                content=content,
                raw_dialog_context=raw_dialog_context,
                structured_memory=structured_memory,
                inventory_index=inventory_index,
                candidate_set=candidate_set,
                legacy_rewrite=legacy_rewrite,
                legacy_planner=legacy_planner,
                conversation_id=conversation_id,
                turn_id=turn_id,
                case_id=case_id,
            ).packet
        safe_planner_feedback = safe_artifact_payload(planner_feedback or {})
        rule_cards = self.rule_knowledge.retrieve_text(
            stage="rewrite",
            query_text=content,
            query_state=(structured_memory or {}).get("last_turn_record", {}).get("query_state", {}),
            retry_packet=json.dumps(safe_planner_feedback, ensure_ascii=False, default=str),
            tool_result_summary=inventory_index or {},
        )
        safe_rule_cards = safe_artifact_payload(rule_cards or "无")
        prompt_artifact = build_kf_task_packet_prompt_artifact(
            content=content,
            raw_dialog_context=raw_dialog_context,
            structured_memory=structured_memory,
            inventory_index=inventory_index,
            candidate_set=candidate_set,
            legacy_rewrite=legacy_rewrite,
            legacy_planner=legacy_planner,
        )
        system_prompt = (
            "你是长租公寓客服 Agentic RAG 的 LLM1 shadow。"
            "你只做问题理解、意图分析、上下文继承、实体绑定和工具计划，不生成客户可见回复。"
            "输出必须能转换为 StructuredTaskPacket：task_atoms 支持多任务，constraint_operation 只能是 inherit/replace/exclude/clear。"
            "constraints 要表达继承、替换、排除、清空；本轮明确修改的条件用 replace，明确不要的条件用 exclude，清空条件用 clear。"
            "candidate_binding 只能绑定输入 candidate_set 中存在的编号；没有 candidate_set 或无法唯一绑定时 selected_candidate_numbers 必须为空。"
            "tool_plan 只列工具动作和内部原因，不能包含 reply_text、clarification_text、客户可见话术、真实看房密码、完整手机号、token 或密钥。"
            "房源、价格、房态、图片、视频事实只能交给工具计划去取证，不得在 LLM1 中编造。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
脱敏 shadow 输入：
{json.dumps(prompt_artifact, ensure_ascii=False, default=str)}

Planner 回流证据：
{json.dumps(safe_planner_feedback, ensure_ascii=False, default=str)}

问题重写规则卡片：
{safe_rule_cards}

返回 JSON：
{{
  "rewritten_query": "结合上下文重写后的需求，不含客户可见回复",
  "response_strategy": {{"mode": "tool_first|send_media|ask_clarification|answer|handoff|safe_fallback"}},
  "constraints": {{
    "inherit": {{}},
    "replace": {{}},
    "exclude": {{}},
    "clear": []
  }},
  "task_atoms": [
    {{
      "task_id": "task-1-search",
      "task_type": "inventory_search|send_video|send_image|send_inventory_sheet|deposit_policy|contract_contact|viewing_guidance|clarification|reply_text",
      "user_text": "脱敏后的用户本轮需求",
      "constraint_operation": "inherit|replace|exclude|clear",
      "constraints": {{}},
      "required_tools": ["inventory.search"]
    }}
  ],
  "candidate_binding": {{
    "selected_candidate_numbers": [],
    "reason": "如何绑定；没有候选集时写 no_candidate_set"
  }},
  "tool_plan": {{
    "actions": ["search_inventory", "generate_reply"],
    "required_tools": ["inventory.search", "reply.compose"],
    "need_rewrite_clarification": false,
    "reason": "内部取证计划，不是客户回复"
  }}
}}

规则：
- 需要房源、价格、房态、预算、户型：tool_plan.actions 包含 search_inventory 和 generate_reply；多套列表加 compact_listing。
- 需要视频/图片：包含 search_inventory、context_tools、send_video/send_image、explain_missing_media、generate_reply。
- 需要房源表：包含 send_inventory_sheet；如果同时要视频或看房，再补对应动作。
- 需要看房、密码、今天能看：包含 search_inventory、context_tools、explain_unavailable_viewing、generate_reply，但不得输出真实密码。
- 需要免押：包含 send_deposit_policy 和 generate_reply；合同、订房、交定金包含 send_contract_contact 和 generate_reply。
- “第几套/前两套/这几套视频”只有在 candidate_set 存在且编号有效时才能写入 selected_candidate_numbers。
- 不确定或无法绑定时，使用 clarification task 和 need_rewrite_clarification=true，但不要写客户可见追问句。
"""
        response = await self._client_for_stage("rewrite").chat.completions.create(
            model=self._stage_model("rewrite"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        raw_output = self._parse_json_object(response.choices[0].message.content or "{}")
        return build_kf_task_packet_shadow(
            raw_output,
            content=content,
            raw_dialog_context=raw_dialog_context,
            structured_memory=structured_memory,
            inventory_index=inventory_index,
            candidate_set=candidate_set,
            legacy_rewrite=legacy_rewrite,
            legacy_planner=legacy_planner,
            conversation_id=conversation_id,
            turn_id=turn_id,
            case_id=case_id,
        ).packet

    async def rewrite_kf_message(
        self,
        *,
        content: str,
        structured_memory: dict[str, Any] | None = None,
        inventory_index: dict[str, Any] | None = None,
        planner_feedback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._stage_api_key_missing("rewrite"):
            return {}
        rule_cards = self.rule_knowledge.retrieve_text(
            stage="rewrite",
            query_text=content,
            query_state=(structured_memory or {}).get("last_turn_record", {}).get("query_state", {}),
            retry_packet=json.dumps(planner_feedback or {}, ensure_ascii=False, default=str),
            tool_result_summary=inventory_index or {},
        )
        system_prompt = (
            "你是租房客服 Agentic RAG Orchestrator 的工具前阶段。你要合并完成问题重写、意图分析、目标是否明确、结构化任务包和工具计划。"
            "本阶段不生成客户可见回复，只输出追问、结构化任务和工具计划。"
            "业务域固定为杭州当前房源表，不处理全国城市查询。"
            "已知区域别名必须直接归一：万达=拱墅万达/北部软件园/城北万象城，不得追问“哪个城市的万达”或“哪个万达广场”。"
            "你只能读取当前原始消息、黑匣子最小记忆、最新房源表事实索引和 Planner 回流证据。"
            "你还必须读取最新房源表事实索引；小区、区域、房号、可查询字段、是否需要追问，只能基于这个索引和上下文判断。"
            "事实索引里的 exact_community_hits 只代表客户明确命中的小区；area_related_communities 只是区域下的小区示例，不能当成客户已经指定的小区。"
            "房源字段语义固定：押一付一/押二付一是不同付款方式下的月租价格；备注是水电费；户型描述是详细户型介绍和特点；看房方式密码是密码、空出时间、提前联系等看房方式。"
            "用户问“价格多少/多少钱/租金多少”但没有限定押一或押二时，必须理解为同时查询押一付一和押二付一两种月租价格，不能只写押一付一。"
            "用户原话里的房号必须逐字保留，不得删减楼栋、单元或连字符，例如 15-1-603 不能改成 15-603。"
            "房号里的数字不能拆出来当预算或价格，例如 15-2-801B 不能生成 801预算、801元或 strict price。"
            "“客户又问/客户问/又问/再问/那小区”等只是话语前缀，不能并入小区名；小区名只取真实楼盘实体。"
            "用户说“一室”默认包含一室户和一室一厅，不要追问是否包含一室一厅；只有用户明确说带厅、有厅、一室一厅时才收窄到一室一厅。"
            "用户说原视频、原片、高清、源文件、下载链接、太糊、保存转发时，必须识别为 wants_original_video=true，同时仍保留 wants_video=true。"
            "如果用户说房源表、表格、总表、房源表发我，必须判定为 inventory_sheet，不要追问小区或价位。"
            "用户补充回答上一轮澄清时，要合并上一轮未完成需求，例如预算、区域、户型，不能把短句当成全新问题。"
            "用户也可能直接接机器人上一句回复说话，例如“4000-5000的呢”“两室的呢”“带视频的呢”“今天能看的呢”“第3套呢”。"
            "这类短句必须先读取 raw_dialog_context 里最近的客户原话和机器人可见回复，再继承上一轮筛选任务的区域/小区/户型/预算/候选列表，"
            "只替换用户本轮明确改动的条件；不能把它当孤立新问题，也不能要求用户重复已经在上下文里出现的区域或户型。"
            "如果用户本轮只给新预算范围，例如“4000-5000的呢”，必须继承上一轮区域和户型，把预算替换为 4000-5000 后重写。"
            "如果用户本轮只给新户型，例如“两室的呢”，必须继承上一轮区域和预算，把户型替换为两室后重写。"
            "如果用户本轮只给素材动作，例如“带视频的呢/图片呢”，必须继承上一轮候选或筛选任务，把素材需求加入任务。"
            "用户说“还有哪几套/剩下的/继续/后面的”时，优先参考黑匣子里的上一轮发送摘要和候选状态。"
            "如果上下文里有 pending_video_sends，只有用户明确说继续、补发、发剩下的视频，才设置 pending_video_action=continue；"
            "用户问新问题时 pending_video_action=hold，不能让待补发视频抢走新问题。"
            "用户取消补发时 pending_video_action=cancel。"
            "如果用户说错小区或房号但不唯一，必须标记需要澄清。"
            "模糊确认只针对房源表内不唯一的小区或房号；命中已知区域别名时直接归一。"
            "你还要判断最可能的意图和置信度；意图不明确时返回 intent=unclear、低置信度，并给一句自然追问。"
            "如果 Planner 回传缺失证据，你必须重新读取用户原话、上下文、候选、已确认房源和结构化记忆，"
            "决定是补出更明确的任务，还是生成基于真实房源/素材索引的追问。"
            "Planner 反馈不是客户可见内容，不能原样发给客户。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
用户原话：
{content}

黑匣子最小记忆：
{json.dumps(structured_memory or {}, ensure_ascii=False, default=str)}

最新房源表事实索引：
{json.dumps(inventory_index or {}, ensure_ascii=False, default=str)}

Planner 回传的内部缺失证据：
{json.dumps(planner_feedback or {}, ensure_ascii=False, default=str)}

问题重写相关规则卡片：
{rule_cards or "无"}

返回 JSON：
{{
  "rewritten_query": "结合上下文重写后的用户需求",
  "query_state": {{"intent": "意图", "area": "区域", "budget": "预算", "layout": "户型", "wants_video": false, "wants_original_video": false, "wants_image": false, "pending_video_action": "continue|cancel|hold|"}},
  "intent": "inventory|media|viewing|deposit|contract|greeting|context_followup|inventory_sheet|general|unclear",
  "intent_confidence": 0.0 到 1.0,
  "context_reference": true 或 false,
  "candidate_action": "remainder|select|none",
  "selected_indices": [候选编号，1 开始],
  "needs_clarification": true 或 false,
  "clarification_text": "需要澄清时给用户的一句话",
  "tool_plan": {{
    "actions": ["按执行顺序排列的工具动作"],
    "confidence": 0.0 到 1.0,
    "need_rewrite_clarification": false,
    "missing_evidence": "",
    "reason": "为什么这样取证/执行"
  }}
}}

规则：
- tool_plan 是给确定性工具执行层的唯一工具前计划；目标明确时必须给出 actions，目标不明确时 tool_plan.need_rewrite_clarification=true 且 actions=[]。
- tool_plan 不能包含客户可见 reply_text；最终话术只能在工具执行后生成。
- 可用工具动作包括 search_inventory、compact_listing、context_tools、send_video、send_image、send_inventory_sheet、send_deposit_policy、send_contract_contact、explain_missing_media、explain_unavailable_viewing、generate_reply。
- 需要查房源、价格、房态、预算、户型时，tool_plan 必须包含 search_inventory 和 generate_reply；多套列表加 compact_listing。
- 需要视频/图片时，tool_plan 必须包含 search_inventory、context_tools、send_video/send_image、explain_missing_media、generate_reply。
- 需要房源表时，tool_plan 只需要 send_inventory_sheet；如果同时还有视频/看房等明确需求，再补对应动作。
- 需要看房/密码/今天能看时，tool_plan 必须包含 search_inventory、context_tools、explain_unavailable_viewing、generate_reply。
- 需要免押时，tool_plan 必须包含 send_deposit_policy 和 generate_reply；需要定房/合同/交定金时，必须包含 send_contract_contact 和 generate_reply。
- “还有哪4套”这类话，candidate_action 必须是 remainder，selected_indices 填未展示候选编号。
- “万达1500左右有哪些”必须重写为拱墅万达/北部软件园/城北万象城区域 + 1500左右 + 在租房源查询。
- “一室”默认宽匹配一室和一室一厅；不得追问“一室户还是一室一厅”。“一室带厅/有没有带厅/一室一厅”才精确匹配一室一厅。
- 如果上一轮已经有预算，当前只补充“拱墅万达附近”，rewritten_query 必须保留上一轮预算条件。
- 如果客户上一轮刚收到房源列表，本轮只说“4000-5000的呢/再高点呢/两室的呢/视频呢/今天能看的呢”，必须把它理解为对上一轮列表条件的修改或补充：继承上一轮区域、小区、户型、候选，替换或新增本轮明确条件。
- 例如机器人上一轮回复“东新园、杭氧、新天地、3500-4500左右、两室...”，客户说“4000-5000的呢”，rewritten_query 必须是“东新园/杭氧/新天地 4000-5000 两室 在租房源”，不能追问小区+房号。
- 判断小区/区域是否存在、是否多义、是否需要追问，必须优先看“最新房源表事实索引”，不能自己联想城市、商场或表外小区。
- 用户原话出现房号时，rewritten_query、effective_query、room_refs 都必须保留原始房号结构；不要把 15-1-603 改成 15-603，也不要把 9-2-402B 改成 9-402B。
- 房号数字不能进入 query_state.budget / budget_range / budget_label；例如“15-2-801B还在吗”不能输出“801预算”。
- “客户又问杨家新雅苑有没有三室的”里的小区是“杨家新雅苑”，不能输出“又问杨家新雅苑”。
- 如果客户同时问视频/图片/密码/看房，intent 要按真实动作返回，不要只返回 inventory。
- 如果客户要原视频/高清版/可保存转发的视频，query_state 必须同时设置 wants_video=true 和 wants_original_video=true。
- 如果客户问“价格多少/多少钱/租金多少”，query_state 或 StructuredTask 要保留同时回答押一付一、押二付一的需求；不要窄化成只查押一付一。
- “这三套视频/前两套视频/1和3视频/都发视频”要在 query_state 中保留数量、序号或全部发送意图，Planner 只负责后续工具规划。
- 有 pending_video_sends 时，新问题优先按新问题重写；只有明确继续补发才 pending_video_action=continue。
- 如果 Planner 回传 need_rewrite_clarification，必须结合真实上下文重写出更明确的任务；仍无法唯一绑定时，needs_clarification=true，并只追问当前缺少的真实字段。
- 如果客户原话看不出要查什么，或者上下文无法绑定，intent=unclear，needs_clarification=true。
- 不能选择候选列表外的房源。
- 不确定就 needs_clarification=true，不要强行猜。
"""
        response = await self._client_for_stage("rewrite").chat.completions.create(
            model=self._stage_model("rewrite"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        text = response.choices[0].message.content or "{}"
        return self._parse_json_object(text)

    async def plan_kf_reply_text(
        self,
        *,
        content: str,
        structured_task: dict[str, Any] | None = None,
        entity_resolution: dict[str, Any] | None = None,
        constraint_proof: dict[str, Any] | None = None,
        planner_result: dict[str, Any] | None = None,
        tool_evidence: dict[str, Any] | None = None,
        planner_retry_reason: str = "",
    ) -> dict[str, Any]:
        use_retry_model = bool(planner_retry_reason.strip())
        if self._stage_api_key_missing("planner", retry=use_retry_model):
            return {"reply_text": "", "source": "missing_llm_key"}
        task = structured_task or {}
        query_state = task.get("query_state") if isinstance(task.get("query_state"), dict) else {}
        intent = str(task.get("intent") or query_state.get("intent") or "")
        evidence = tool_evidence or {}
        rule_cards = self.rule_knowledge.retrieve_text(
            stage="planner",
            intent=intent,
            query_text=content,
            query_state=query_state,
            constraint_proof=constraint_proof or {},
            retry_packet=planner_retry_reason,
            tool_result_summary=evidence,
        )
        system_prompt = (
            "你是租房客服 Agentic RAG Orchestrator 的工具后阶段，负责在工具执行后生成客户可见 reply_text，并同时完成最终 LLM 自检。"
            "你只能依据结构化任务包、实体归一结果、约束证明、第一阶段工具计划和工具结果证据写回复。"
            "不得重新解释客户意图，不得改写 intent，不得编造工具证据里没有的房源、价格、密码、视频或图片。"
            "如果工具结果不足以回答，reply_text 也必须自然说明缺什么、下一步怎么处理；不能空回复。"
            "如果目标仍无法绑定，返回 need_rewrite_clarification=true 和 missing_evidence，交回问题重写层。"
            "如果任务是 inventory_sheet 或工具动作包含 send_inventory_sheet，只看工具结果里的 inventory_image_count/inventory_images："
            "只要数量大于 0，就表示房源表图片已经准备好，reply_text 必须说“房源表发你了，可以先给客户看整体”，不能说没查到房源表、暂未更新或稍后再试。"
            "回复必须贴着客户真实需求接话：问有没有/还有吗/有哪些/还在吗，先明确“有的/还在/暂时没查到”。"
            "客户问“有视频吗/有没有视频/有图片吗/有没有图片”，且工具证据已经找到对应素材时，reply_text 必须先说“有的，”，再说明“这是某小区+房号的视频/图片”。"
            "客户问“有哪些/哪几套/附近有没有/这边有没有”这类列表或区域查询，开头用“有的，...”或“暂时没查到...”，不要用“还在/在的”。"
            "如果客户问“还在吗”且工具证据命中目标房源，reply_text 必须以“还在，”或“还在的，”开头。"
            "发送视频/图片/房源表时，reply_text 要自然说明“这是某小区+房号的视频/图片”或“房源表发你了”。"
            "客户要原视频/高清/可保存转发时，必须查看 ToolEvidence 是否有原视频文件、下载链接或素材页；没有时不能说已发原视频，只能说明普通视频可能被压缩，原视频需要下载链接/素材页或暂时没找到。"
            "缺视频/图片时只能说“某小区+房号暂时没找到视频/图片素材”，不能说“稍后发你”“正在补同步”“等补全后再发”。"
            "如果客户同时要房源表和视频/今天能看，但工具只准备好了房源表、没有命中可发视频或具体看房房源，必须说：房源表发你了；按当前区域/预算/看房条件暂时没匹配到可直接发视频的具体房源；让客户从表里选小区+房号后再查视频或看房方式。"
            "这种情况也不能说“等补全后再发”“稍后发视频”。"
            "如果 EntityResolutionResult 里有 community_corrections，说明用户原小区名被房号唯一命中纠正了，reply_text 必须透明说明“你说的应该是某小区+房号”，再继续回答。"
            "字段语义固定：押一付一/押二付一是对应付款方式下的月租价格；备注是水电费；户型描述是详细特点；看房方式密码才是密码/空出/提前联系。"
            "客户泛问价格/多少钱/租金时，命中房源必须同时回答押一付一和押二付一两种月租；只有客户明确只问押一或只问押二时才只答一种。"
            "客户问价格是否一样、哪个便宜、哪个价格低时，必须先给直接结论，再列两套押一付一/押二付一月租。"
            "如果客户请求比较两套或多套价格，但工具只命中部分房源，不能判断哪套更便宜，只能说明已查到哪套、哪套没查到，建议确认房号。"
            "预算查询里，如果某套房只有押一付一或押二付一其中一种付款方式在预算内，回复必须说明“有些房源是其中一种付款方式在预算内”，不能笼统说全部符合预算。"
            "对具体价格做预算判断时必须先比较数字；价格小于等于预算上限时，绝不能说刚过预算、超预算、超出预算或高出预算。"
            "如果 ConstraintProof.features 里有燃气、阳台、独立厨卫等特征，匹配成功或没查到时都必须在 reply_text 里复述这些特征，不能只说户型。"
            "客户只问价格、还在不在、水电、户型、视频、图片、房源推荐时，不要主动报看房密码；即使 ToolEvidence 里有看房方式密码，也不能写出来。"
            "只有客户明确问看房、密码、今天能不能看、自助看、打不开门、怎么约看时，才引用看房方式密码或预约联系方式。"
            "如果 RetryPacket 里提示“用户未问看房/密码时，多房源推荐不能泛化看房密码”，必须重写去掉所有密码字段，不能重复原回复。"
            "客户没有问视频/图片/房源表时，不要主动说暂时没找到视频/图片素材，也不要引导先看房源表。"
            "客户问还没空出、密码不对、打不开门、预约看房时，必须引导联系 18758141785 / 13282125992 / 19941091943。"
            "客户问今天能不能看、今天想看、怎么约看、自己看、密码时，reply_text 必须包含看房方式、空出时间、提前联系要求或预约联系方式，不能只回答价格水电。"
            "如果看房方式字段写的是“6.xx空出/看房提前联系”，必须按具体房源说空出时间或提前联系，不能泛称“这些房源都已空出/全部已空出”。"
            "话术要像真人中介客服，短一点、直接一点；不要写“系统显示”“根据您提供的信息”“作为机器人”等模板话。"
            "返回 JSON 必须同时包含 reply_text 和 selfcheck；selfcheck 只检查你本次生成的回复是否事实一致、需求贴合、上下文连贯、口吻自然。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
客户真实需求：
{content}

结构化任务包 StructuredTask：
{json.dumps(structured_task or {}, ensure_ascii=False, default=str)}

实体归一结果 EntityResolutionResult：
{json.dumps(entity_resolution or {}, ensure_ascii=False, default=str)}

约束证明 ConstraintProof：
{json.dumps(constraint_proof or {}, ensure_ascii=False, default=str)}

第一阶段 Planner 工具计划：
{json.dumps(planner_result or {}, ensure_ascii=False, default=str)}

工具结果证据 ToolEvidence：
{json.dumps(tool_evidence or {}, ensure_ascii=False, default=str)}

自检失败回流证据 RetryPacket：
{planner_retry_reason or "无"}

Planner 相关规则卡片：
{rule_cards or "无"}

返回 JSON：
{{
  "reply_text": "基于工具结果生成的客户可见回复，不能为空，必须自然且事实可证明",
  "need_rewrite_clarification": true 或 false,
  "missing_evidence": "无法生成时缺少的真实证据，只给内部链路使用",
  "reason": "一句话说明为什么这样回复",
  "selfcheck": {{
    "status": "pass|retry|fallback",
    "reason": "不通过原因；通过则为空",
    "planner_retry_reason": "给 Orchestrator 重试的证据包摘要",
    "human_score": 0 到 100,
    "fact_score": 0 到 100,
    "demand_fit_score": 0 到 100
  }}
}}

规则：
- reply_text 必须来自工具结果证据；不能使用第一阶段 pre_tool_reply_text 里的猜测内容。
- 有匹配房源时，至少说清标准小区名+房号；多套列表必须编号。
- 没有匹配房源时，直接说暂时没查到，不能说有的。
- 泛问价格/多少钱/租金时，每套命中房源都要同时写押一付一和押二付一月租。
- 问“价格一样吗/哪个便宜/哪个价格低”时，必须第一句先回答“一样/不一样/哪套更便宜”，再列证据价格。
- 问两套或多套价格对比时，如果工具只命中部分房源，不能说哪套更低；必须说“只查到A，B没查到，暂时没法比较，先确认房号”。
- 预算查询里，如果某套房只有押一付一或押二付一其中一种付款方式在预算内，要明确说明这一点，避免让客户误以为两种付款方式都在预算内。
- 对具体价格做预算判断时必须先比较数字；价格小于等于预算上限时，不能说“刚过预算/超预算/超出预算/高出预算”。
- ConstraintProof.features 非空时，回复必须复述这些特征；例如用户问“带燃气的一室一厅”，没查到也要说“暂时没查到带燃气的一室一厅”，不能只说“一室一厅”。
- 列表或区域查询不要用“还在/在的”开头；“还在”只用于具体单套还在不在。
- 有素材动作时，必须说明对应小区+房号的视频/图片；缺素材时说明哪套暂无素材。
- ConstraintProof.wants_original_video=true 时，如果 ToolEvidence 没有 original_video_paths/original_video_urls/material_page_urls，reply_text 不能写“原视频已发/高清原片已发”；只能说明当前普通视频可能压缩，原视频需要通过下载链接或素材页提供，或说明暂时没找到原视频。
- 如果 send_inventory_sheet 已成功但 video_count=0 或没有 target_rows，回复必须保留“房源表发你了”，同时说明当前条件下暂时没匹配到可直接发视频/看房的具体房源，并请客户从表里选小区+房号；不要承诺后续补发。
- 客户问“有视频吗/有没有视频/有图片吗/有没有图片”，且 ToolEvidence 里 video_count/image_count 大于 0 时，reply_text 必须以“有的，”开头，不能只说“视频发你了”。
- EntityResolutionResult.community_corrections 非空时，第一句要透明说明“你说的应该是某小区+房号”，不能静默把客户原小区名替换成标准名。
- 缺素材时不能承诺后续主动发送；只说明本轮缺哪套，已经找到的正常发。
- 客户问“还在吗”且工具证据命中房源时，必须先回答“还在，”再说明价格、视频或看房信息。
- 客户没有问看房或密码时，不要主动输出看房密码；工具证据里有密码也不能写。
- 如果 RetryPacket 要求去掉看房密码，本次 reply_text 必须完全去掉密码和“看房密码”字样，只保留视频/图片/价格/房源事实。
- 客户问今天看/能不能看/怎么约看/密码时，必须回答看房方式、空出时间、提前联系要求或预约联系方式，不能只回答价格、水电、户型。
- 看房方式字段包含具体空出时间时，不能说“都已空出/全部已空出”；必须按小区+房号列出空出时间，或者说“这几套看房都要提前联系确认时间”。
- 客户没有问视频/图片/房源表时，不要主动输出“没找到视频素材/可以先看房源表”。
- 如果工具结果里有 suppress_actions=true，不要声称已经发送图片/视频/房源表。
- 如果无法唯一绑定目标，need_rewrite_clarification=true，reply_text 为空。
- selfcheck.status=pass 才代表工具后阶段认为回复可发送；如果发现事实不一致、动作说明缺失、上下文不连贯、口吻太模板、遗漏客户真实需求，必须 selfcheck.status=retry，并写清 planner_retry_reason。
"""
        response = await self._client_for_stage(
            "planner",
            retry=use_retry_model,
        ).chat.completions.create(
            model=self._stage_model("planner", retry=use_retry_model),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        text = response.choices[0].message.content or "{}"
        data = self._parse_json_object(text)
        selfcheck = data.get("selfcheck")
        if not isinstance(selfcheck, dict):
            data["selfcheck"] = {
                "status": "pass",
                "source": "planner_reply_text_default_selfcheck",
                "reason": "Planner 工具后阶段未显式返回 selfcheck，按兼容模式通过；主流程仍会执行本地硬规则自检。",
            }
        else:
            status = str(selfcheck.get("status") or "pass").strip().lower()
            if status not in {"pass", "retry", "fallback"}:
                status = "retry"
            selfcheck["status"] = status
            selfcheck["source"] = "planner_reply_text_selfcheck"
            data["selfcheck"] = selfcheck
        data["source"] = "llm_planner_reply_from_tools"
        return data

    async def compose_kf_outbound_shadow(
        self,
        *,
        task_packet: dict[str, Any],
        evidence_bundle: dict[str, Any],
        response_strategy: dict[str, Any] | str | None = None,
        retry_reason: str = "",
    ) -> dict[str, Any]:
        if self._stage_api_key_missing("reply"):
            return {
                "reply_text": "",
                "answered_task_ids": [],
                "claims": [],
                "action_captions": [],
                "self_review": {
                    "status": "retry",
                    "source": "missing_llm_key",
                    "retry_reason": "LLM2 outbound shadow 缺少 reply 阶段 API key，未生成客户可见文本。",
                    "rewrite_retry_reason": "LLM2 outbound shadow missing reply API key",
                    "llm2_decides_media_targets": False,
                },
                "source": "missing_llm_key",
            }
        safe_task = safe_artifact_payload(task_packet or {})
        safe_evidence = safe_artifact_payload(evidence_bundle or {})
        safe_strategy = safe_artifact_payload(response_strategy or {})
        system_prompt = (
            "你是租房客服 Agentic RAG 的 LLM2 outbound shadow，只负责怎么说。"
            "你必须基于 StructuredTaskPacket、ToolEvidenceBundle 和 ResponseStrategy 生成 PreparedOutboundPackage 的文本字段。"
            "不得决定发哪套房、发什么素材、改 candidate_number、改 listing_id、改 send action。"
            "价格、房态、密码、链接、素材目标只能来自 ToolEvidenceBundle；证据没有返回就不能写。"
            "密码和链接属于高风险内容：不要抄写真值，只引用 evidence_id/slot 让受控发送边界处理。"
            "如果证据不足或发现会越界，只返回 retry/rewrite reason，不生成事实文本。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
StructuredTaskPacket：
{json.dumps(safe_task, ensure_ascii=False, default=str)[:4000]}

ToolEvidenceBundle：
{json.dumps(safe_evidence, ensure_ascii=False, default=str)[:5000]}

ResponseStrategy：
{json.dumps(safe_strategy, ensure_ascii=False, default=str)[:1500]}

上一轮 retry/rewrite reason：
{retry_reason or "无"}

返回 JSON：
{{
  "reply_text": "客户可见文本；证据不足时留空",
  "answered_task_ids": ["已回答的 task_id"],
  "claims": [
    {{"claim_id":"claim-1","task_id":"task-id","field":"字段名","value":"只填证据支持的值","evidence_ref":"evidence_id","text":"声明文本"}}
  ],
  "action_captions": [
    {{"caption_id":"caption-1","action_id":"既有 send action id","text":"动作说明，只描述该 evidence 对应素材"}}
  ],
  "self_review": {{
    "status": "pass|retry",
    "reason": "retry 时说明不通过原因",
    "retry_reason": "给 Orchestrator 的重试原因",
    "rewrite_retry_reason": "需要回到 LLM1 时的原因",
    "llm2_decides_media_targets": false
  }}
}}

规则：
- claims 必须有 evidence_ref，且只能声明该 evidence 里已有的事实。
- action_captions 只能引用已有 action_id，不能新增 send_actions。
- 不要输出真实密码、完整手机号、token、URL 真值。
- 不要把房号数字当价格，不要新增工具证据外的价格或房态。
- 失败时 reply_text 为空，self_review.status=retry，并写 retry_reason。
"""
        response = await self._client_for_stage("reply").chat.completions.create(
            model=self._stage_model("reply"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        text = response.choices[0].message.content or "{}"
        data = self._parse_json_object(text)
        review = data.get("self_review")
        if not isinstance(review, dict):
            review = {"status": "retry", "reason": "LLM2 shadow 未返回 self_review"}
        status = str(review.get("status") or "retry").strip().lower()
        if status not in {"pass", "retry"}:
            status = "retry"
        review["status"] = status
        review["source"] = "llm2_outbound_shadow"
        review["llm2_decides_media_targets"] = False
        data["self_review"] = review
        data["source"] = "llm2_outbound_shadow"
        return data

    async def assess_kf_final_reply(
        self,
        *,
        content: str,
        raw_dialog_context: list[dict[str, Any]] | None = None,
        structured_task: dict[str, Any] | None = None,
        constraint_proof: dict[str, Any] | None = None,
        tool_evidence: dict[str, Any] | None = None,
        outbound_package: dict[str, Any] | None = None,
        draft_reply: str = "",
        rule_selfcheck: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._stage_api_key_missing("selfcheck"):
            return {"status": "pass", "source": "missing_llm_key"}
        task = structured_task or {}
        query_state = task.get("query_state") if isinstance(task.get("query_state"), dict) else {}
        intent = str(task.get("intent") or query_state.get("intent") or "")
        rule_cards = self.rule_knowledge.retrieve_text(
            stage="selfcheck",
            intent=intent,
            query_text="\n".join([content, draft_reply]),
            query_state=query_state,
            constraint_proof=constraint_proof or {},
            tool_result_summary={
                "tool_evidence": tool_evidence or {},
                "outbound_package": outbound_package or {},
                "rule_selfcheck": rule_selfcheck or {},
            },
        )
        system_prompt = (
            "你是租房客服 Agentic RAG 的最终回复自检 LLM。"
            "你只做质检，不重新解释客户意图，不替问题重写层新增需求，不替 Planner 生成客户可见回答；不通过时给 Planner 重规划证据。"
            "你的职责只有四件事：检查 Planner/工具动作是否完成结构化任务、回复是否匹配问题重写意图、上下文是否连贯、口吻是否像真人客服。"
            "还必须检查完整待发送包：文本、图片、视频、房源表动作是否一致；动作说明是否包含标准小区名和房号。"
            "ConstraintProof 里的区域、预算、户型、小区、房号、候选编号用于校验 Planner 和动作有没有跑偏；"
            "不要要求每个约束都逐字出现在客户可见文本里，除非客户要的是文字查询结果或文本本身会让人误解。"
            "房源字段语义固定：押一付一/押二付一是不同付款方式下的月租价格，备注是水电费，户型描述是详细特点，看房方式密码才是密码/空出/提前联系。"
            "如果用户问房源表，回复不能追问小区/价位；如果用户问免押，回复不能发房间图片；"
            "如果用户问区域和预算，Planner/工具必须按区域和预算执行；文字列表回复要让客户看得出筛选条件，动作型回复不能和动作相矛盾。"
            "如果有明确工具结果，不能让用户重复已经给过的信息。"
            "客服机器人不能声称自己会给客户打电话或电话核对；只能让客户/中介联系指定号码，或请对方补充具体小区房号后继续查。"
            "如果 PreparedOutboundPackage.reply_source 表示硬规则或工具证据生成的确定性回复，且规则自检已通过，"
            "你只能检查口吻是否自然、是否接住上下文；不能改变事实、不能清空动作、不能把确定性回复改成泛兜底。"
            "这种场景如果只是口吻还可以更自然，或者文本没有逐字复述所有约束但动作已经满足任务，status 仍返回 pass，并在 reason 里给口吻建议。"
            "客户可见回复要自然直接，不能模板感重、答非所问、遗漏关键条件或编造事实。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
客户原话：
{content}

原始对话上下文 raw_dialog_context：
{json.dumps(raw_dialog_context or [], ensure_ascii=False, default=str)[:3500]}

结构化任务包 StructuredTask：
{json.dumps(structured_task or {}, ensure_ascii=False, default=str)[:2500]}

约束证明 ConstraintProof：
{json.dumps(constraint_proof or {}, ensure_ascii=False, default=str)[:1800]}

工具证据摘要：
{json.dumps(tool_evidence or {}, ensure_ascii=False, default=str)[:3500]}

完整待发送包 PreparedOutboundPackage：
{json.dumps(outbound_package or {}, ensure_ascii=False, default=str)[:2500]}

规则自检结果：
{json.dumps(rule_selfcheck or {}, ensure_ascii=False, default=str)[:1500]}

最终自检相关规则卡片：
{rule_cards or "无"}

待发送草稿回复：
{draft_reply}

返回 JSON：
{{
  "status": "pass|retry|fallback",
  "reason": "不通过原因；通过则为空",
  "planner_retry_reason": "给 Planner 的重规划说明，必须包含缺什么工具证据或应纠正什么动作",
  "fallback_reply": "连续失败时可发给客户的安全回复",
  "human_score": 0 到 100,
  "fact_score": 0 到 100,
  "demand_fit_score": 0 到 100
}}

        判断规则：
        - 通过才 status=pass。
        - 如果待发送草稿回复为空，必须 status=retry；最终自检不能生成回复，也不能把空回复改成安全兜底。
        - 事实不一致、Planner 动作不满足 StructuredTask、文本答非所问、上下文断裂、动作与文本矛盾、语气不自然到影响使用，才 status=retry。
        - 不要因为回复没有逐字复述区域/预算/户型就 retry；先看 Planner/工具动作和文本是否已经满足问题重写后的真实需求。
        - 如果待发送包里有视频/图片/房源表，客户可见文本或动作说明必须自然说明“这是某某小区+房号的视频/图片”或“房源表发你了”。
        - 如果文本和动作矛盾，例如动作有房源表但文本说发不了，必须 retry。
- 如果字段语义误读，例如把押一付一说成押金金额、把备注当普通备注、从备注猜密码，必须 retry。
- 如果无法通过重规划修复，才 status=fallback，并给安全兜底回复。
- planner_retry_reason 要写清楚证据：用户真实需求、草稿问题、需要重新调用或补充的工具。
"""
        response = await self._client_for_stage("selfcheck").chat.completions.create(
            model=self._stage_model("selfcheck"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        text = response.choices[0].message.content or "{}"
        data = self._parse_json_object(text)
        status = str(data.get("status") or data.get("action") or "pass").strip().lower()
        if status not in {"pass", "retry", "fallback"}:
            status = "retry"
        data["status"] = status
        data["source"] = "llm_final_selfcheck"
        return data

    @staticmethod
    def _format_candidate_for_intent(row: dict[str, Any]) -> str:
        def value(*keys: str) -> str:
            for key in keys:
                raw = row.get(key)
                if raw is not None and str(raw).strip():
                    return str(raw).strip()
            return ""

        parts = [
            value("小区", "社区", "楼盘"),
            value("房号", "房间号", "room_id", "RoomID", "编号"),
            value("户型", "房型", "户型分类"),
            value("押一付", "押一付一", "押一付一月租金"),
            value("押二付", "押二付一", "押二付一月租金"),
            value("看房方式", "看房方式密码", "密码"),
            value("备注", "说明"),
        ]
        return "，".join(part for part in parts if part) or json.dumps(row, ensure_ascii=False)

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return {}
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
        return data if isinstance(data, dict) else {}
