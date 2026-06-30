import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.services.config_check import is_missing_or_placeholder
from app.services.kf_contracts import safe_artifact_payload
from app.services.kf_dual_llm_production import DUAL_LLM_PRODUCTION_LLM1_PROMPT_VERSION
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
        inventory_snapshot_id: str = "",
        candidate_set_id: str = "",
        mode: str = "shadow",
    ):
        """LLM1 shadow：只输出 StructuredTaskPacket，不生成客户可见回复。"""
        production_mode = str(mode or "").strip().lower() == "production"
        prompt_version = DUAL_LLM_PRODUCTION_LLM1_PROMPT_VERSION if production_mode else ""
        source_label = "llm1_production" if production_mode else ""
        if self._stage_api_key_missing("rewrite"):
            if production_mode:
                raise RuntimeError("LLM1 production rewrite API key is missing")
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
                inventory_snapshot_id=inventory_snapshot_id,
                candidate_set_id=candidate_set_id,
                prompt_version=prompt_version or "dual_llm_shadow.llm1_task_packet.v1",
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
            legacy_rewrite=None if production_mode else legacy_rewrite,
            legacy_planner=None if production_mode else legacy_planner,
            prompt_version=prompt_version or "dual_llm_shadow.llm1_task_packet.v1",
            source="production" if production_mode else "shadow",
            include_legacy_summary=not production_mode,
        )
        llm1_mode_label = "production" if production_mode else "shadow"
        system_prompt = (
            f"你是长租公寓客服 Agentic RAG 的 LLM1 {llm1_mode_label}。"
            "你只做问题理解、意图分析、上下文继承、实体绑定和工具计划，不生成客户可见回复。"
            "输出必须能转换为 StructuredTaskPacket：task_atoms 支持多任务，constraint_operation 只能是 inherit/replace/exclude/clear。"
            "production 下必须直接输出 tool_plan.actions，不能只输出 task_atoms/task_type 让系统推导工具动作。"
            "constraints 要表达继承、替换、排除、清空；本轮明确修改的条件用 replace，明确不要的条件用 exclude，清空条件用 clear。"
            "candidate_binding 只能绑定输入 candidate_set 中存在的编号；没有 candidate_set 或无法唯一绑定时 selected_candidate_numbers 必须为空。"
            "tool_plan 只列工具动作和内部原因，不能包含 reply_text、clarification_text、客户可见话术、真实看房密码、完整手机号、token 或密钥。"
            "房源、价格、房态、图片、视频事实只能交给工具计划去取证，不得在 LLM1 中编造。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
脱敏 {llm1_mode_label} 输入：
{json.dumps(prompt_artifact, ensure_ascii=False, default=str)}

Validator / Tool Resolver 回流证据：
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
      "task_type": "inventory_search|send_video|send_image|send_inventory_sheet|deposit_policy|contract_contact|viewing_guidance|clarification|reply_compose_signal",
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
- production 下 tool_plan.actions 是工具执行的唯一权威；目标明确时必须直接给 actions，不能依赖 task_atoms/task_type 补动作。
- tool_plan 绝不能输出 reply_text、pre_tool_reply_text、planner_missing_reply 或任何客户可见话术。
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
            inventory_snapshot_id=inventory_snapshot_id,
            candidate_set_id=candidate_set_id,
            prompt_version=prompt_version or "dual_llm_shadow.llm1_task_packet.v1",
            source_label=source_label,
            mode=llm1_mode_label,
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
            "本阶段不生成客户可见回复，只输出内部澄清需求、结构化任务和工具计划。"
            "业务域固定为杭州当前房源表，不处理全国城市查询。"
            "已知区域别名必须直接归一：万达=拱墅万达/北部软件园/城北万象城，不得追问“哪个城市的万达”或“哪个万达广场”。"
            "你只能读取当前原始消息、黑匣子最小记忆、最新房源表事实索引和 Validator / Tool Resolver 回流证据。"
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
            "如果 Validator 或 Tool Resolver 回传缺失证据，你必须重新读取用户原话、上下文、候选、已确认房源和结构化记忆，"
            "决定是补出更明确的任务，还是生成基于真实房源/素材索引的追问。"
            "回流证据不是客户可见内容，不能原样发给客户。"
            "只返回 JSON，不要 Markdown。"
        )
        user_prompt = f"""
用户原话：
{content}

黑匣子最小记忆：
{json.dumps(structured_memory or {}, ensure_ascii=False, default=str)}

最新房源表事实索引：
{json.dumps(inventory_index or {}, ensure_ascii=False, default=str)}

Validator / Tool Resolver 回传的内部缺失证据：
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
  "clarification_text": "内部澄清需求摘要，不得写客户可见追问句",
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
- tool_plan 不能包含客户可见 reply_text；最终客户可见话术只能由 LLM2 在工具取证后生成。
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
- “这三套视频/前两套视频/1和3视频/都发视频”要在 query_state 中保留数量、序号或全部发送意图，LLM1 只给出工具计划，具体目标绑定由 Tool Resolver 基于候选和证据完成。
- 有 pending_video_sends 时，新问题优先按新问题重写；只有明确继续补发才 pending_video_action=continue。
- 如果 Validator 或 Tool Resolver 回传 need_rewrite_clarification，必须结合真实上下文重写出更明确的任务；仍无法唯一绑定时，needs_clarification=true，并只标记当前缺少的真实字段。
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

    async def compose_kf_outbound_shadow(
        self,
        *,
        task_packet: dict[str, Any],
        evidence_bundle: dict[str, Any],
        response_strategy: dict[str, Any] | str | None = None,
        retry_reason: str = "",
        mode: str = "shadow",
    ) -> dict[str, Any]:
        production_mode = str(mode or "").strip().lower() == "production"
        mode_label = "production" if production_mode else "shadow"
        source = f"llm2_outbound_{mode_label}"
        if self._stage_api_key_missing("reply"):
            return {
                "reply_text": "",
                "answered_task_ids": [],
                "claims": [],
                "action_captions": [],
                "self_review": {
                    "status": "retry",
                    "source": "missing_llm_key",
                    "retry_reason": f"LLM2 outbound {mode_label} 缺少 reply 阶段 API key，未生成客户可见文本。",
                    "rewrite_retry_reason": f"LLM2 outbound {mode_label} missing reply API key",
                    "llm2_decides_media_targets": False,
                },
                "source": "missing_llm_key",
            }
        safe_task = safe_artifact_payload(task_packet or {})
        safe_evidence = safe_artifact_payload(evidence_bundle or {})
        safe_strategy = safe_artifact_payload(response_strategy or {})
        system_prompt = (
            f"你是租房客服 Agentic RAG 的 LLM2 outbound {mode_label}，只负责怎么说。"
            "你必须基于 StructuredTaskPacket、ToolEvidenceBundle 和 ResponseStrategy 生成 PreparedOutboundPackage 的文本字段。"
            "production 模式下，客户可见自然话术只能来自你生成的 reply_text/action_captions；Sender 只执行已验证 send action 和授权槽位追加，不生成客服话术。"
            "确定性 inventory/media/deposit/contract fallback 只允许进入 ToolEvidenceBundle 或 error code，不会替 LLM2 生成或改写最终话术。"
            "不得决定发哪套房、发什么素材、改 candidate_number、改 listing_id、改 send action。"
            "价格、房态、密码、链接、素材目标只能来自 ToolEvidenceBundle；证据没有返回就不能写。"
            "房源表、缺素材、免押政策、合同联系、看房联系/密码都会以 evidence_type 或受控 send action 形式提供，"
            "你只能引用这些证据来组织客户可见表达。"
            "朝南、有电梯、已空出、可养猫、近地铁等普通事实也必须来自 ToolEvidenceBundle，并写入 claims。"
            "密码和链接属于高风险内容：不要抄写真值，只引用 evidence_id/slot 让 Sender 按已验证 action 处理。"
            "合同联系电话和看房密码如出现在受控 send action，只写 action_captions 或交给 Sender 按已验证 action 追加授权槽位，不要在 reply_text 里输出手机号或密码真值。"
            "话术要像真实租房客服，短句、自然、直接；不要暴露 listing_id、evidence_id、ToolEvidence、send action 等内部名。"
            "已有媒体 send action 时，用“这是某某房间的视频/图片。”这类当前动作说明，"
            "不要说“稍后发、等下发、会发你、素材已准备好”。"
            "如果上一轮 retry/rewrite reason 是 L3 文案问题，只重写表达和 captions，必须保留原有 facts/actions 绑定，不要要求回 LLM1。"
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
- action_captions 只能引用已有 action_id，且只能描述该 action 对应 evidence，不能新增 send_actions。
- 不要输出真实密码、完整手机号、token、URL 真值。
- 合同联系电话、看房密码必须通过受控 action/evidence slot 表达；LLM2 不能自己写手机号或密码真值。
- evidence_type=deposit_policy 时可以回答免押条件/服务费；evidence_type=missing_media 时只能说明对应素材缺失，不能承诺稍后补发。
- evidence_type=inventory_sheet 或已有 send_inventory_sheet 动作时，只说明房源表图片会由动作发送，不要改成纯文字房源表。
- 受控合同/看房 action 存在时，reply_text 写自然引导，真实手机号/密码由 Sender 按已验证 action 追加授权槽位；你不得直接生成这些真值。
- 不要把房号数字当价格，不要新增工具证据外的价格、房态、朝南、有电梯、已空出、可养猫、近地铁等事实。
- 客户可见话术必须口语化、短句，不出现内部字段名或工具名。
- 已有媒体 send action 时，reply_text/action_captions 用“这是某某房间的视频/图片。”，不要写稍后、等下、会发你或素材已准备好。
- 如果上一轮 retry/rewrite reason 提到 L3、internal name、template 或 action tense，只改话术，不改 claims/send action/action_id/evidence_ref。
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
            review = {"status": "retry", "reason": f"LLM2 {mode_label} 未返回 self_review"}
        status = str(review.get("status") or "retry").strip().lower()
        if status not in {"pass", "retry"}:
            status = "retry"
        review["status"] = status
        review["source"] = source
        review["llm2_decides_media_targets"] = False
        data["self_review"] = review
        data["source"] = source
        return data

    async def compose_kf_outbound_production(
        self,
        *,
        task_packet: dict[str, Any],
        evidence_bundle: dict[str, Any],
        response_strategy: dict[str, Any] | str | None = None,
        retry_reason: str = "",
    ) -> dict[str, Any]:
        return await self.compose_kf_outbound_shadow(
            task_packet=task_packet,
            evidence_bundle=evidence_bundle,
            response_strategy=response_strategy,
            retry_reason=retry_reason,
            mode="production",
        )

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
            "你只做质检，不重新解释客户意图，不替问题重写层新增需求，不替 LLM1、Tool Resolver 或 LLM2 生成客户可见回答；不通过时按失败层级生成回流证据。"
            "你的职责只有四件事：检查 LLM1 工具计划与 Tool Resolver 动作是否完成结构化任务、回复是否匹配问题重写意图、上下文是否连贯、口吻是否像真人客服。"
            "还必须检查完整待发送包：文本、图片、视频、房源表动作是否一致；动作说明是否包含标准小区名和房号。"
            "ConstraintProof 里的区域、预算、户型、小区、房号、候选编号用于校验 LLM1 工具计划和 Tool Resolver 动作有没有跑偏；"
            "不要要求每个约束都逐字出现在客户可见文本里，除非客户要的是文字查询结果或文本本身会让人误解。"
            "房源字段语义固定：押一付一/押二付一是不同付款方式下的月租价格，备注是水电费，户型描述是详细特点，看房方式密码才是密码/空出/提前联系。"
            "如果用户问房源表，回复不能追问小区/价位；如果用户问免押，回复不能发房间图片；"
            "如果用户问区域和预算，LLM1 工具计划和 Tool Resolver 必须按区域和预算执行；文字列表回复要让客户看得出筛选条件，动作型回复不能和动作相矛盾。"
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
  "planner_retry_reason": "给 LLM1 / Tool Resolver 的重规划说明，必须包含缺什么工具证据或应纠正什么动作",
  "fallback_reply": "连续失败时可发给客户的安全回复",
  "human_score": 0 到 100,
  "fact_score": 0 到 100,
  "demand_fit_score": 0 到 100
}}

        判断规则：
        - 通过才 status=pass。
        - 如果待发送草稿回复为空，必须 status=retry；最终自检不能生成回复，也不能把空回复改成安全兜底。
        - 事实不一致、LLM1 工具计划或 Tool Resolver 动作不满足 StructuredTask、文本答非所问、上下文断裂、动作与文本矛盾、语气不自然到影响使用，才 status=retry。
        - 不要因为回复没有逐字复述区域/预算/户型就 retry；先看 LLM1 工具计划、Tool Resolver 动作和文本是否已经满足问题重写后的真实需求。
        - 如果待发送包里有视频/图片/房源表，客户可见文本或动作说明必须自然说明“这是某某小区+房号的视频/图片”或“房源表发你了”。
        - 如果文本和动作矛盾，例如动作有房源表但文本说发不了，必须 retry。
- 如果字段语义误读，例如把押一付一说成押金金额、把备注当普通备注、从备注猜密码，必须 retry。
- 如果无法通过重规划修复，才 status=fallback，并给安全兜底回复。
- 重规划说明要写清楚证据：用户真实需求、草稿问题、需要重新调用或补充的工具。
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
