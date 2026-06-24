from __future__ import annotations

from pathlib import Path
import textwrap

from PIL import Image, ImageDraw, ImageFont


DESKTOP = Path.home() / "Desktop"
OUT_PREFIX = "Agentic-RAG"
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    if bold:
        for path in (Path("C:/Windows/Fonts/msyhbd.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
            if path.exists():
                return ImageFont.truetype(str(path), size)
    for path in FONT_CANDIDATES:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


F_TITLE = font(40, bold=True)
F_SUBTITLE = font(18)
F_H = font(24, bold=True)
F_TEXT = font(18)
F_SMALL = font(15)
F_NODE = font(19, bold=True)
F_NODE_TEXT = font(15)
F_TINY = font(13)


COLORS = {
    "blue": ("#2563eb", "#eff6ff"),
    "indigo": ("#4f46e5", "#eef2ff"),
    "green": ("#16a34a", "#ecfdf5"),
    "orange": ("#ea580c", "#fff7ed"),
    "red": ("#dc2626", "#fef2f2"),
    "amber": ("#ca8a04", "#fffbeb"),
    "slate": ("#334155", "#f8fafc"),
}


def rounded(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    outline: str,
    fill: str = "#ffffff",
    width: int = 3,
    radius: int = 16,
) -> None:
    draw.rounded_rectangle(box, radius=radius, outline=outline, fill=fill, width=width)


def _arrow_head(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    fill: str,
) -> None:
    x1, y1 = start
    x2, y2 = end
    if abs(y2 - y1) >= abs(x2 - x1):
        direction = 1 if y2 >= y1 else -1
        pts = [(x2, y2), (x2 - 9, y2 - 15 * direction), (x2 + 9, y2 - 15 * direction)]
    else:
        direction = 1 if x2 >= x1 else -1
        pts = [(x2, y2), (x2 - 15 * direction, y2 - 9), (x2 - 15 * direction, y2 + 9)]
    draw.polygon(pts, fill=fill)


def line(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    fill: str = "#263241",
    width: int = 3,
    arrow: bool = True,
) -> None:
    draw.line([start, end], fill=fill, width=width)
    if arrow:
        _arrow_head(draw, start, end, fill)


def polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[int, int]],
    *,
    fill: str = "#263241",
    width: int = 3,
    arrow: bool = True,
) -> None:
    draw.line(points, fill=fill, width=width)
    if arrow and len(points) >= 2:
        _arrow_head(draw, points[-2], points[-1], fill)


def dashed(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    fill: str = "#16a34a",
    width: int = 2,
    dash: int = 14,
) -> None:
    x1, y1 = start
    x2, y2 = end
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if not length:
        return
    steps = max(1, int(length // dash))
    for i in range(0, steps, 2):
        a = i / steps
        b = min((i + 1) / steps, 1)
        draw.line(
            [
                (x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b),
            ],
            fill=fill,
            width=width,
        )


def wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    max_chars: int,
    *,
    fill: str = "#253044",
    fnt: ImageFont.ImageFont = F_TEXT,
    gap: int = 7,
) -> int:
    x, y = xy
    lines: list[str] = []
    for raw in text.split("\n"):
        lines.extend(
            textwrap.wrap(
                raw,
                width=max_chars,
                break_long_words=True,
                replace_whitespace=False,
            )
            or [""]
        )
    for item in lines:
        draw.text((x, y), item, font=fnt, fill=fill)
        y += getattr(fnt, "size", 16) + gap
    return y


def title(draw: ImageDraw.ImageDraw, text: str, subtitle: str) -> None:
    draw.text((58, 34), text, font=F_TITLE, fill="#101827")
    wrapped(draw, subtitle, (60, 88), 130, fill="#526179", fnt=F_SUBTITLE, gap=5)


def node(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    heading: str,
    body: str,
    color_name: str = "slate",
    *,
    heading_center: bool = True,
    body_chars: int = 26,
) -> None:
    color, fill = COLORS[color_name]
    rounded(draw, box, outline=color, fill=fill, width=3, radius=14)
    x1, y1, x2, _ = box
    if heading_center:
        draw.text(((x1 + x2) // 2, y1 + 25), heading, font=F_NODE, fill="#111827", anchor="mm")
        wrapped(draw, body, (x1 + 18, y1 + 54), body_chars, fill="#4b5563", fnt=F_NODE_TEXT, gap=4)
    else:
        draw.text((x1 + 22, y1 + 20), heading, font=F_NODE, fill="#111827")
        wrapped(draw, body, (x1 + 22, y1 + 56), body_chars, fill="#4b5563", fnt=F_NODE_TEXT, gap=4)


def legend(draw: ImageDraw.ImageDraw, lines: list[str], y: int) -> None:
    rounded(draw, (60, y, 2140, y + 82), outline="#cbd5e1", fill="#ffffff", width=2, radius=10)
    wrapped(draw, " | ".join(lines), (82, y + 22), 160, fill="#334155", fnt=F_TEXT, gap=4)


def save(img: Image.Image, name: str) -> Path:
    path = DESKTOP / name
    img.save(path)
    return path


def render_overall() -> Path:
    img = Image.new("RGB", (2200, 1650), "#f8fafc")
    draw = ImageDraw.Draw(img)
    title(
        draw,
        "Agentic RAG 客服机器人整体链路图",
        "当前目标链路：所有客户输入先进入问题重写/意图分析；Planner 读取结构化任务包后调用工具；工具结果回来后生成 reply_text；最终自检通过后才发送。",
    )

    boxes = {
        "A": (70, 150, 410, 235),
        "B": (70, 285, 430, 390),
        "C": (70, 450, 510, 575),
        "D": (155, 640, 425, 760),
        "E": (560, 625, 970, 770),
        "F": (70, 825, 520, 940),
        "G": (70, 1000, 520, 1128),
        "H": (585, 965, 1110, 1160),
        "I": (70, 1195, 520, 1318),
        "J": (585, 1195, 1110, 1318),
        "K": (70, 1370, 520, 1480),
        "L": (585, 1370, 1040, 1480),
        "M": (1090, 1370, 1450, 1480),
    }
    node(draw, boxes["A"], "A 客户输入", "新问题 / 追问 / 补充 / 继续发送", "slate")
    node(draw, boxes["B"], "B 上下文读取", "读取 raw_dialog_context、最近 turn_records、候选、confirmed、pending", "slate")
    node(draw, boxes["C"], "C 问题重写 + 意图分析 LLM", "结合最新房源事实索引和规则卡，归一区域/小区/房号，输出结构化任务或真实追问", "indigo", body_chars=32)
    node(draw, boxes["D"], "D 任务是否明确", "目标 / 意图 / 约束 / 证据", "amber")
    node(draw, boxes["E"], "E 意图层追问", "目标不明确时才发给客户；追问必须基于真实房源、素材、缺失字段", "amber", body_chars=34)
    node(draw, boxes["F"], "F 结构化任务包", "intent / effective_query / query_state / EntityResolutionResult / ConstraintProof / target_rows", "green", body_chars=36)
    node(draw, boxes["G"], "G Planner 第一阶段", "只读任务包，规划要查询、获取或发送哪些工具；不重新解释用户意图", "indigo", body_chars=35)
    node(draw, boxes["H"], "H 知识库与工具执行", "房源表 / 素材库 / 房源表 PNG / 规则库 / 看房密码 / 定房免押 / pending 视频", "orange", body_chars=38)
    node(draw, boxes["I"], "I 工具证据汇总", "命中房源、价格、户型、素材数量、发送结果、缺失原因、规则证据", "blue", body_chars=35)
    node(draw, boxes["J"], "J Planner 第二阶段", "读取工具结果，根据证据生成客户可见 reply_text 和动作说明", "indigo", body_chars=34)
    node(draw, boxes["K"], "K 最终自检", "检查事实一致、动作一致、真实需求、上下文连贯、拟人化", "red", body_chars=34)
    node(draw, boxes["L"], "L 发送客户", "自检通过后发送文本、图片、视频、房源表 PNG，并写回黑匣子", "blue", body_chars=34)
    node(draw, boxes["M"], "M 安全兜底", "一次重试仍失败时，只发守规兜底或人工联系方式", "blue", body_chars=28)

    line(draw, (240, 235), (240, 285))
    line(draw, (250, 390), (250, 450))
    line(draw, (290, 575), (290, 640))
    line(draw, (425, 700), (560, 700), fill="#ca8a04")
    line(draw, (290, 760), (290, 825))
    line(draw, (295, 940), (295, 1000))
    line(draw, (520, 1065), (585, 1065), fill="#ea580c")
    line(draw, (850, 1160), (295, 1195), fill="#2563eb")
    line(draw, (520, 1255), (585, 1255), fill="#4f46e5")
    line(draw, (295, 1318), (295, 1370), fill="#dc2626")
    line(draw, (520, 1425), (585, 1425), fill="#2563eb")
    line(draw, (1040, 1425), (1090, 1425), fill="#dc2626")
    polyline(draw, [(295, 1370), (295, 1340), (850, 1340), (850, 1318)], fill="#dc2626")
    draw.text((540, 1346), "自检失败：带 RetryPacket 回 Planner 第二阶段重写一次", font=F_SMALL, fill="#b91c1c")
    polyline(draw, [(70, 1065), (28, 1065), (28, 512), (70, 512)], fill="#dc2626")
    draw.text((34, 800), "证据不足：回问题重写层补目标", font=F_SMALL, fill="#b91c1c")

    # Memory panel.
    mx1, my1, mx2, my2 = 1510, 150, 2140, 1480
    rounded(draw, (mx1, my1, mx2, my2), outline="#16a34a", fill="#ecfdf5", width=4, radius=22)
    draw.text(((mx1 + mx2) // 2, my1 + 36), "结构化会话记忆 structured_memory", font=F_H, fill="#111827", anchor="mm")
    node(
        draw,
        (mx1 + 42, my1 + 92, mx2 - 42, my1 + 330),
        "raw_dialog_context",
        "唯一滚动原始对话上下文。客户输入和最终发送内容都会刷新，供下一轮问题重写和自检判断连贯性。",
        "green",
        heading_center=False,
        body_chars=40,
    )
    node(
        draw,
        (mx1 + 42, my1 + 380, mx2 - 42, my1 + 690),
        "turn_records",
        "按轮保存：turn_id / turn_index / user_raw / rewritten_query / intent / query_state / needs_clarification / assistant_sent_summary。",
        "green",
        heading_center=False,
        body_chars=40,
    )
    node(
        draw,
        (mx1 + 42, my1 + 740, mx2 - 42, my1 + 1065),
        "读取边界",
        "问题重写读取原始上下文和上一轮最小记录；Planner 不读完整黑匣子；自检读取原始上下文、结构化任务、工具证据和待发送包。",
        "green",
        heading_center=False,
        body_chars=40,
    )
    node(
        draw,
        (mx1 + 42, my1 + 1115, mx2 - 42, my1 + 1395),
        "发送写回",
        "发送阶段写入 final_reply、消息类型、图片/视频/房源表数量、候选状态摘要；大文件和接口明细不写入黑匣子。",
        "green",
        heading_center=False,
        body_chars=40,
    )
    dashed(draw, (1510, 270), (430, 335))
    dashed(draw, (1510, 525), (510, 515))
    dashed(draw, (1510, 900), (520, 1425))
    dashed(draw, (1040, 1425), (1510, 1260))
    legend(
        draw,
        [
            "蓝色=事实/发送",
            "紫色=LLM",
            "绿色=记忆/任务",
            "橙色=工具",
            "红色=自检/失败回流",
            "Planner 第一阶段规划工具，第二阶段基于工具结果生成 reply_text",
        ],
        1540,
    )
    return save(img, f"{OUT_PREFIX}-00-整体链路图.png")


def render_module_principle() -> Path:
    img = Image.new("RGB", (1900, 1420), "#f8fafc")
    draw = ImageDraw.Draw(img)
    title(
        draw,
        "Agentic RAG 模块工作原理图",
        "每个模块单独展示输入、处理、输出和写入记忆；用于理解连续对话、工具调用、自检回流和批量视频动作。",
    )

    cards = [
        ((60, 150, 930, 390), "1 上下文读取层", ["输入", "读取", "输出"], ["客户原话 + 会话 ID", "读取 raw_dialog_context、turn_records、候选/confirmed/pending", "给问题重写层的最小上下文包"], "blue"),
        ((990, 150, 1860, 390), "2 问题重写 + 意图分析 LLM", ["输入", "处理", "输出"], ["先看当前原话和原始上下文，再判断新问题/追问/补充", "结合最新房源事实索引做区域、小区、房号归一和约束证明", "输出明确结构化任务；不明确时基于真实候选追问"], "indigo"),
        ((60, 450, 930, 690), "3 结构化会话记忆", ["保存", "记录", "复用"], ["raw_dialog_context 保存原始对话滚动上下文", "turn_records 按轮保存 user_raw、rewrite、intent、发送摘要", "下一轮问题重写和自检按最小视图读取"], "green"),
        ((990, 450, 1860, 690), "4 Planner 第一阶段", ["输入", "规划", "输出"], ["读取结构化任务包、实体归一、约束证明、工具目录、RetryPacket", "只规划工具，不重新解释用户意图，不直接追问客户", "输出工具动作计划：查房源/查素材/发房源表/取规则知识"], "indigo"),
        ((60, 750, 930, 990), "5 知识库与工具目录", ["索引", "检索", "收敛"], ["房源事实索引、素材库索引、规则知识卡、房源表 PNG", "Planner 按 intent 取最相关部分，不整库塞给 LLM", "降低噪声，避免泛化和幻觉"], "orange"),
        ((990, 750, 1860, 990), "6 工具执行与事实证据", ["执行", "返回", "约束"], ["执行查询、获取素材、准备发送动作、记录缺失项", "返回命中行、素材数量、发送结果、失败原因、规则证据", "回复不能编造工具没给出的事实"], "orange"),
        ((60, 1050, 930, 1290), "7 Planner 第二阶段", ["输入", "生成", "输出"], ["读取工具调用结果和必要原始上下文", "根据工具证据生成自然客服 reply_text 和动作说明", "必须生成非空回复，不能把空回复交给自检补"], "indigo"),
        ((990, 1050, 1860, 1290), "8 最终自检 + 发送", ["检查", "回流", "发送"], ["检查事实、动作、上下文连贯和拟人化口吻", "不通过时带 RetryPacket 回 Planner 第二阶段重写一次", "通过后再发送文本/图片/视频/房源表；批量视频未完成写 pending"], "red"),
    ]
    for box, heading, flow, body, color_name in cards:
        module_card(draw, box, heading, flow, body, color_name)
    legend(
        draw,
        ["关键顺序：重写定任务 -> Planner 调工具 -> 工具取证 -> Planner 写回复 -> 自检 -> 发送写回"],
        1340,
    )
    return save(img, f"{OUT_PREFIX}-01-模块工作原理图.png")


def mini_flow(draw: ImageDraw.ImageDraw, x: int, y: int, labels: list[str], color: str) -> None:
    for i, label in enumerate(labels):
        top = y + i * 56
        rounded(draw, (x, top, x + 108, top + 36), outline=color, fill="#ffffff", width=2, radius=8)
        draw.text((x + 54, top + 18), label, font=F_SMALL, anchor="mm", fill="#111827")
        if i < len(labels) - 1:
            line(draw, (x + 54, top + 38), (x + 54, top + 54), fill=color, width=2)


def module_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    heading: str,
    flow: list[str],
    body: list[str],
    color_name: str,
) -> None:
    color, fill = COLORS[color_name]
    rounded(draw, box, outline=color, fill=fill, width=3, radius=16)
    x1, y1, _, _ = box
    draw.text((x1 + 26, y1 + 22), heading, font=F_H, fill="#111827")
    mini_flow(draw, x1 + 28, y1 + 72, flow, color)
    tx = x1 + 158
    y = y1 + 72
    for item in body:
        y = wrapped(draw, item, (tx, y), max_chars=42, fill="#344056", fnt=F_TEXT, gap=6) + 6


def render_internal(
    index: int,
    slug: str,
    heading: str,
    subtitle: str,
    steps: list[tuple[str, str, str]],
    side_panels: list[tuple[str, str, str]] | None = None,
    footer: str = "",
) -> Path:
    img = Image.new("RGB", (1800, 1160), "#f8fafc")
    draw = ImageDraw.Draw(img)
    title(draw, heading, subtitle)
    side_panels = side_panels or []

    start_x = 80
    top = 170
    box_w = 380
    box_h = 128
    gap_x = 70
    gap_y = 95
    positions: list[tuple[int, int, int, int]] = []
    for i, _ in enumerate(steps):
        row = i // 3
        col = i % 3
        x = start_x + col * (box_w + gap_x)
        y = top + row * (box_h + gap_y)
        positions.append((x, y, x + box_w, y + box_h))

    for i, ((step_title, body, color_name), box) in enumerate(zip(steps, positions)):
        node(draw, box, step_title, body, color_name, heading_center=False, body_chars=31)
        if i < len(positions) - 1:
            x1, y1, x2, y2 = box
            nx1, ny1, _, _ = positions[i + 1]
            if (i + 1) % 3 != 0:
                line(draw, (x2, (y1 + y2) // 2), (nx1, (ny1 + ny1 + box_h) // 2), fill="#334155")
            else:
                polyline(
                    draw,
                    [
                        ((x1 + x2) // 2, y2),
                        ((x1 + x2) // 2, y2 + 46),
                        (start_x - 30, y2 + 46),
                        (start_x - 30, ny1 + box_h // 2),
                        (nx1, ny1 + box_h // 2),
                    ],
                    fill="#334155",
                )

    if side_panels:
        px1, py = 1420, 170
        for panel_title, panel_body, color_name in side_panels:
            node(
                draw,
                (px1, py, 1735, py + 205),
                panel_title,
                panel_body,
                color_name,
                heading_center=False,
                body_chars=25,
            )
            py += 245

    if footer:
        legend(draw, [footer], 1042)
    return save(img, f"{OUT_PREFIX}-{index:02d}-{slug}.png")


def render_internal_modules() -> list[Path]:
    outputs: list[Path] = []
    outputs.append(
        render_internal(
            2,
            "上下文读取与黑匣子",
            "模块 1：上下文读取与黑匣子",
            "负责把客户原话、最近对话、候选状态和发送状态整理成问题重写可读的最小上下文，不把调试日志塞进语义链路。",
            [
                ("1 接收回调原文", "企业微信回调文本、会话 ID、客户 ID、时间戳。先写入 raw_dialog_context。", "slate"),
                ("2 读取会话状态", "读取 structured_memory、confirmed_room、last_candidate_set、last_media_context、pending_video_sends。", "green"),
                ("3 生成 rewrite_memory_view", "只保留最近原始对话、上一轮 turn_record、候选摘要、confirmed 摘要、pending 摘要。", "green"),
                ("4 生成 selfcheck_memory_view", "给自检读取最近原始对话，用于检查上下文连贯和拟人化接话。", "green"),
                ("5 控制读取边界", "Planner 不直接读取完整黑匣子；工具证据、自检过程不写入黑匣子。", "amber"),
                ("6 输出上下文包", "输出给问题重写层；后续发送阶段再写回 assistant_sent_summary。", "blue"),
            ],
            [
                ("黑匣子保留", "raw_dialog_context、turn_records、发送摘要、候选状态。", "green"),
                ("黑匣子不保留", "Planner 内部推理、完整工具响应、自检内部过程、大文件内容。", "red"),
            ],
            "目标：下一轮能理解“这套/上一个/1和5/继续发/刚才那个”，同时避免无关日志干扰 LLM。",
        )
    )
    outputs.append(
        render_internal(
            3,
            "问题重写意图分析",
            "模块 2：问题重写 + 意图分析 LLM",
            "唯一语义入口。它先读上下文和最新房源事实索引，再输出明确任务；如果目标不明确，由它生成基于真实候选的追问。",
            [
                ("1 读取输入", "当前客户原话 + raw_dialog_context + 最近 turn_records + 房源事实索引摘要 + 规则卡。", "indigo"),
                ("2 判断对话类型", "判断新问题、追问、补充回答、继续发送、换房源、换预算、换区域。", "indigo"),
                ("3 实体归一", "区域/小区/房号归一。万达/新天地等区域别名直接归一；多义小区或模糊房号不猜。", "green"),
                ("4 约束证明", "抽取区域、小区、房号、预算、户型、素材类型、候选编号、看房/免押/定房意图。", "green"),
                ("5 目标明确性判定", "房源或素材目标不唯一时输出 needs_clarification 和真实候选；不进入 Planner 瞎发。", "amber"),
                ("6 输出结构化任务", "effective_query、intent、query_state、EntityResolutionResult、ConstraintProof、StructuredTask。", "blue"),
            ],
            [
                ("重要保护", "泛问小区视频属于模糊房号：先列候选追问，不自动挑一套。", "red"),
                ("事实来源", "最新 rewrite_inventory_index 和房源表字段语义，不靠 LLM 自己联想。", "orange"),
            ],
            "目标：把“新天地4000-5000的呢”理解为继承上一轮区域/户型，只替换预算，而不是当成孤立问题。",
        )
    )
    outputs.append(
        render_internal(
            4,
            "结构化任务包",
            "模块 3：结构化任务包",
            "问题重写输出给 Planner 的唯一语义输入。Planner 只照任务包做工具规划，不重新猜客户意图。",
            [
                ("1 intent", "inventory、inventory_sheet、media_video、media_image、viewing、utilities、deposit、contract、greeting 等。", "green"),
                ("2 effective_query", "合并上下文后的查询句，给工具检索使用；原始客户话只保留作口吻和连贯参考。", "green"),
                ("3 query_state", "区域、预算、户型、目标房源、素材需求、是否补充上一轮。", "green"),
                ("4 EntityResolutionResult", "标准区域/小区/房号、置信度、多义候选、是否需要追问。", "green"),
                ("5 ConstraintProof", "硬约束：区域、小区、房号、预算范围、户型、候选编号、素材数量。", "green"),
                ("6 tool_requirements", "Planner 可调用工具要求：查房源、查素材、发房源表、规则知识、pending 视频。", "blue"),
            ],
            [
                ("给 Planner", "任务包 + 工具目录 + RetryPacket。", "indigo"),
                ("不给 Planner", "完整原始长对话、整张房源表、完整黑匣子。", "red"),
            ],
            "目标：把 LLM 的理解变成可校验结构，后续工具和自检都按同一份需求执行。",
        )
    )
    outputs.append(
        render_internal(
            5,
            "Planner工具规划与回复",
            "模块 4：Planner LLM",
            "Planner 分两段：工具前只规划工具；工具后基于工具结果生成 reply_text。它不能直接追问客户，目标不足要回问题重写层。",
            [
                ("1 读取任务包", "只接收 StructuredTask、EntityResolutionResult、ConstraintProof、ToolCatalog、RetryPacket。", "indigo"),
                ("2 规划工具动作", "选择查房源、素材、房源表 PNG、规则卡、看房密码、定房免押、pending 视频等工具。", "indigo"),
                ("3 证据不足回流", "如果目标不足或证据冲突，返回 need_rewrite_clarification 给问题重写层，不直接问客户。", "red"),
                ("4 等待工具结果", "工具执行后返回命中行、素材结果、发送动作、缺失项、规则证据。", "orange"),
                ("5 生成 reply_text", "根据工具证据生成自然客服回复；必须解释每个动作，例如“这是某某房号的视频”。", "indigo"),
                ("6 输出待发送包", "reply_text + 图片/视频/房源表动作 + 缺失说明 + pending 写入请求。", "blue"),
            ],
            [
                ("异常处理", "缺视频/图片要说明暂无；密码缺失/错误/未空出要给三个联系方式。", "amber"),
                ("硬约束", "不得把押一付一/押二付一说成押金；不得编造工具没有的事实。", "red"),
            ],
            "目标：Planner 必须生成回答，不能把空回复丢给自检；自检只检查和回流，不替 Planner 做主回复。",
        )
    )
    outputs.append(
        render_internal(
            6,
            "工具执行事实证据",
            "模块 5：工具执行与事实证据",
            "工具层负责确定性事实，不负责理解客户意图。房源、价格、房态、密码、图片、视频只从工具证据来。",
            [
                ("1 房源表查询", "读取服务器房源表缓存，按 ConstraintProof 过滤区域、预算、户型、小区、房号。", "orange"),
                ("2 房源表 PNG", "客户要房源表时刷新/发送飞书表格渲染 PNG。", "orange"),
                ("3 素材库查询", "从服务器素材库匹配小区+房号的视频/图片；模糊房号不自动选。", "orange"),
                ("4 规则知识库", "免押、合同、联系方式、看房规则等按 intent 召回规则卡。", "orange"),
                ("5 异常结果", "记录 missing_media、sync_status、素材数量、失败原因、可联系号码。", "amber"),
                ("6 证据汇总", "输出统一工具证据，交给 Planner 第二阶段生成 reply_text。", "blue"),
            ],
            [
                ("字段语义", "押一付一/押二付一=付款方式下月租；备注=水电费；户型描述=详细介绍。", "green"),
                ("事实边界", "工具没返回的素材/密码/价格不能让 LLM 编。", "red"),
            ],
            "目标：工具结果不完整也要显式返回缺失原因，不能让机器人沉默或误说房源不存在。",
        )
    )
    outputs.append(
        render_internal(
            7,
            "最终自检回流",
            "模块 6：最终自检与回流",
            "自检检查 Planner 有没有按问题重写的真实需求做对、工具事实有没有一致、回复是否像真人接话。失败就带证据回 Planner。",
            [
                ("1 读取检查包", "原始上下文、结构化任务、ConstraintProof、工具证据、待发送文本和动作。", "red"),
                ("2 事实检查", "小区、房号、价格、户型、水电、密码、素材动作必须和工具证据一致。", "red"),
                ("3 需求检查", "回复必须覆盖真实需求。问有没有先回答有/没有；问视频不能只发文本。", "red"),
                ("4 连贯与拟人化", "判断是否顺着上下文接话，避免模板味、让用户重复已给信息、无编号却让回序号。", "red"),
                ("5 RetryPacket", "失败时输出失败原因、证据、需要 Planner 重写的点。动作也一起拦住。", "amber"),
                ("6 通过或兜底", "通过才发送；一次重试仍失败只发安全兜底，不发送错误图片/视频/表格。", "blue"),
            ],
            [
                ("自检不做", "不代替 Planner 编主回复，不修改事实字段，不绕过工具证据。", "red"),
                ("自检要做", "拦住所有客户可见文本和动作，包括硬规则/兜底回复。", "green"),
            ],
            "目标：自检不是礼貌润色，而是最终闸门；文本和图片/视频/房源表动作必须一起通过。",
        )
    )
    outputs.append(
        render_internal(
            8,
            "发送阶段与记忆写回",
            "模块 7：发送阶段与记忆写回",
            "发送阶段只执行已经通过自检的待发送包，并把客户可见结果写回黑匣子，供下一轮理解上下文。",
            [
                ("1 接收待发送包", "reply_text、房源表 PNG、图片、视频、目标房源、pending 请求。", "blue"),
                ("2 发送前顺序", "先发解释文本，再发视频/图片；房源表请求发送 PNG；视频超限分批。", "blue"),
                ("3 视频处理", "企业微信失败或文件过大时用服务器 ffmpeg 转码压缩，链接只是兜底。", "orange"),
                ("4 pending 写入", "批量视频未发完写 pending_video_sends；只有用户说继续才补发。", "green"),
                ("5 assistant_sent_summary", "记录 final_reply、消息类型、图片/视频/房源表数量、房源 key、候选状态摘要。", "green"),
                ("6 刷新原始上下文", "把最终客户可见文本写入 raw_dialog_context。下一轮问题重写能看到机器人刚说过什么。", "green"),
            ],
            [
                ("禁止", "发送未经自检通过的文本、图片、视频或房源表。", "red"),
                ("保留", "只记录摘要，不记录 token、完整接口响应、大文件内容。", "green"),
            ],
            "目标：客户根据机器人上句话接话时，下一轮能从黑匣子恢复真实上下文。",
        )
    )
    outputs.append(
        render_internal(
            9,
            "同步事实索引素材库",
            "模块 8：同步、事实索引与素材库",
            "RAG 不直接读本机临时数据，以服务器同步后的房源表、房源表 PNG、素材库和 rewrite_inventory_index 为准。",
            [
                ("1 飞书源同步", "定时同步源飞书房源表和房源笔记素材到目标云盘/服务器素材库。", "orange"),
                ("2 房源表 PNG", "目标房源表导出并渲染为 PNG，客户要房源表时优先发送。", "blue"),
                ("3 rewrite_inventory_index", "同步后生成问题重写专用索引：区域别名、小区列表、房号、价格、户型分布。", "green"),
                ("4 素材库本地化", "视频和图片同步到服务器本地素材目录，机器人运行时直接读取服务器素材。", "orange"),
                ("5 缓存健康检查", "记录同步版本、签名、行数、素材数量和失败项；定时器一天三次。", "amber"),
                ("6 RAG 使用", "问题重写读事实索引，工具层读房源表和素材库，避免 LLM 自己猜。", "indigo"),
            ],
            [
                ("字段语义", "区域/小区/房号/户型描述/户型分类/押一付一/押二付一/看房方式密码/备注。", "green"),
                ("运行要求", "服务器无人值守 systemd + 环境变量凭证，不依赖本机或浏览器弹窗。", "red"),
            ],
            "目标：房源事实和素材事实始终来自服务器最新同步结果，测试和线上都用同一事实源。",
        )
    )
    outputs.append(
        render_internal(
            10,
            "测试巡检",
            "模块 9：测试巡检与质量门禁",
            "用于证明链路不是靠单条兜底规则凑过，而是能覆盖随机多轮对话、上下文继承、动作一致和事实一致。",
            [
                ("1 固定回归集", "覆盖万达/新天地/石桥/东站、房源表、视频、图片、看房密码、水电、免押、定房。", "blue"),
                ("2 随机问题集", "通过 rewrite_inventory_index 生成新的区域/小区/房号/价格/户型组合。", "green"),
                ("3 UTF-8 校验", "测试输入必须保留中文，防止 PowerShell 乱码导致假测试。", "red"),
                ("4 记录 stage timing", "按 rewrite、planner、tools、final_selfcheck、send 统计耗时和失败阶段。", "amber"),
                ("5 问题归因", "定位是问题重写、Planner、工具、素材、发送还是自检失败。", "indigo"),
                ("6 上线门禁", "本地全量 pytest、服务器全量 pytest、健康检查、定时器状态和真实日志抽检。", "blue"),
            ],
            [
                ("保底规则", "固定集通过后还要随机 10 个新问题最终测试。", "green"),
                ("完成标准", "发现上下文丢失、候选错绑、错误追问、答非所问就不能算完成。", "red"),
            ],
            "目标：用测试和日志定位链路问题，而不是只针对某一句话补兜底。",
        )
    )
    return outputs


def main() -> None:
    DESKTOP.mkdir(parents=True, exist_ok=True)
    outputs = [
        render_overall(),
        render_module_principle(),
        *render_internal_modules(),
    ]
    print("\n".join(str(path) for path in outputs))


if __name__ == "__main__":
    main()
