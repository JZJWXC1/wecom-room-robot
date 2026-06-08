from openai import AsyncOpenAI

from app.config import settings
from app.models import IncomingMessage, ReplyPlan
from app.services.config_check import is_missing_or_placeholder


class ReplyGenerator:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.dashscope_api_key or "missing-key",
            base_url=settings.dashscope_base_url,
        )

    async def generate(
        self,
        message: IncomingMessage,
        inventory_snapshot: str,
        media_images: list[str],
        media_videos: list[str],
        conversation_context: str = "",
    ) -> ReplyPlan:
        if is_missing_or_placeholder(settings.dashscope_api_key):
            return ReplyPlan(text=settings.default_fallback_reply)

        system_prompt = (
            "你是企业微信上的租房客服自动回复机器人。"
            "必须只依据提供的房源库存和媒体资料回答，不能编造不存在的房源、价格、房态。"
            "如果库存表没有相关信息，就说明需要人工确认。"
            "当房源库存来自图片 OCR/视觉解析时，必须以解析文本为准，不要使用示例房源或历史记忆。"
            "客户问房源时，优先列出小区、房号、户型、押一付、押二付、空置/密码/看房说明、备注。"
            "回复要像真人中介客服，直接、短一点，不要像 AI 报告。"
            "不要写“根据您提供的信息”“系统显示”“作为机器人”“如果您需要更多帮助”这类套话。"
            "不要用三段式总结、粗体标题、过度礼貌开场或免责声明。"
            "可以说“这套”“我这边看到”“我把视频发你”这类自然口语。"
            "客户只是说你好、您好、在吗这类问候时，要像真人客服一样回应并引导对方直接问小区、房号、价格、视频或房源表。"
            "不要说“笔记”，我们没有笔记，只有视频和房间详细信息；客户说笔记时统一理解为房间详细信息或视频资料。"
            "除房源表 PNG 外，不发送房间图片、照片、实拍图；客户要房间图片或照片时，不要承诺发图片，"
            "只引导对方发小区和房号，我方按房源表查详情和视频。"
            "客户要视频时，只有在可发送视频链接里确实有对应素材，才可以说有视频或我把视频发你；没有素材时只能说需要再确认。"
            "如果客户询问还没空出的房子能不能看、帮忙联系或预约看房，必须引导客户联系 18758141785 / 13282125992 / 19941091943 预约。"
            "客户问免押金、免押、无忧住、芝麻信用时，必须说明免押是支付宝无忧住信用免押服务，"
            "需要符合芝麻信用风控并支付押金金额5.5%-8%的免押服务费，不要说成完全免押或只要一年起租就行。"
            "如果没有匹配到信息，只说需要再确认，不要编理由。"
        )
        user_prompt = f"""
客户消息类型：{message.msg_type}
客户消息内容：{message.content or message.media_id or "非文字消息"}

实时房源库存：
{inventory_snapshot}

可发送房源表图片链接（仅客户明确要房源表时才可提及；不要当作房间照片发送）：
{chr(10).join(media_images) if media_images else "暂无"}

可发送视频链接：
{chr(10).join(media_videos) if media_videos else "暂无"}

请生成一条企业微信回复。需要视频时，在文本里自然说明“我把视频发你”。
不要说“我把图片发你”“我把照片发你”，除非客户明确要的是房源表。
"""
        if conversation_context:
            user_prompt += f"\n\n最近10条对话上下文：\n{conversation_context}"
        response = await self._client.chat.completions.create(
            model=settings.dashscope_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        text = response.choices[0].message.content or settings.default_fallback_reply
        allow_inventory_images = any(
            keyword in (message.content or "")
            for keyword in ("房源表", "表格", "空房表", "在租表")
        )
        return ReplyPlan(
            text=text.strip(),
            images=media_images[:3] if allow_inventory_images else [],
            videos=media_videos[:1],
        )
