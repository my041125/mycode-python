"""
演示入口（对应 Java 版 Main 的 --mock 分支与 CodingDemo）。

--mock / --real：门票演示 —— 查景点 → 查天气 → 算门票，跑一遍完整 ReAct 循环。
--demo / --code-demo：代码编辑演示 —— 重置靶子文件 → 读文件 → 精准替换 → 语法校验 → 出差异。

离线模式用脚本化假模型：预设工具调用序列，验证的是循环、上下文拼接、工具分发
这些「agent 的骨架」，不联网不花钱；--real 交给工厂接真模型，
走 agent.toml / 环境变量 / 命令行参数决定的厂商。
"""

from __future__ import annotations

from pathlib import Path

from mycode.kernel.agent import Agent
from mycode.kernel.hooks import LoggingHook
from mycode.kernel.registry import ToolRegistry
from mycode.kernel.tool import SimpleTool, Tool
from mycode.tools.code_edit import CodeEditTool
from mycode.tools.code_tools import list_tool, read_tool
from mycode.clients import factory as llm_factory
from mycode.clients.scripted import ScriptedLlmClient


def mock_demo(args: list[str], mock: bool = True) -> None:
    """门票演示主函数。mock=True 用假模型；False 接真模型。"""
    tools = _ticket_tools()

    # 离线模式用脚本化假模型：预设三步工具调用，用来验证循环是否正确。
    # 真实模式交给工厂，由 --provider / 环境变量 / agent.toml 决定用哪家
    if mock:
        llm = ScriptedLlmClient.of(
            [
                ScriptedLlmClient.call("call_1", "search_spots", {"district": "零陵区"}),
                ScriptedLlmClient.call("call_2", "get_weather", {"city": "永州"}),
                ScriptedLlmClient.call("call_3", "calc_ticket_price",
                                       {"basePrice": 60, "people": 2, "discount": 0.8}),
            ],
            lambda ctx: ("（离线模拟回答）已查景点、天气，并按 8 折算好两个人的门票。\n"
                         "最后一条工具返回：\n" + ScriptedLlmClient.last_observation(ctx)),
        )
    else:
        llm = llm_factory.create(args)

    agent = Agent(
        "乡村文旅助手",
        llm=llm,
        tools=tools,
        system_prompt=(
            "你是「乡村文旅综合服务系统」的行程助手。\n"
            "回答要求：先给结论，再给依据；涉及价格必须调用工具计算，禁止心算。\n"
            "如果工具返回 ERROR，先判断是参数问题还是无数据，再决定重试或告知用户。"
        ),
        hooks=[LoggingHook()],  # 打印每一轮的过程，调试时必开
        max_steps=8,
    )

    answer = agent.run("帮我安排零陵区一日游，两个人，把门票钱算出来")
    print("\n================ 最终回答 ================")
    print(answer)
    print("=========================================")


def _ticket_tools() -> ToolRegistry:
    """装配三个演示工具（真实项目里它们分别换成数据库、天气 API 和计算器）。"""
    return ToolRegistry().register(
        # 工具 1：按区县查景点
        SimpleTool(
            "search_spots",
            "按区县检索乡村文旅景点，返回景点名、等级、门票原价、建议游玩时长",
            Tool.schema({"district": Tool.str_param("区县名称，例如 零陵区")}, "district"),
            _search_spots,
        ),
        # 工具 2：查天气
        SimpleTool(
            "get_weather",
            "获取城市当日天气，返回天气状况与气温区间",
            Tool.schema({"city": Tool.str_param("城市名，例如 永州")}, "city"),
            _get_weather,
        ),
        # 工具 3：算门票。故意做成工具而不是让模型心算，因为大模型算数并不可靠
        SimpleTool(
            "calc_ticket_price",
            "按原价、人数、折扣计算门票总价，用于给游客报价",
            Tool.schema({
                "basePrice": Tool.num_param("单人门票原价，单位元"),
                "people": Tool.num_param("人数"),
                "discount": Tool.num_param("折扣系数，0.8 表示八折"),
            }, "basePrice", "people"),
            _calc_ticket_price,
        ),
    )


def _search_spots(args: dict) -> str:
    """景点数据 handler。"""
    district = str(args.get("district") or "")
    if not district.strip():
        raise ValueError("district 不能为空")
    if "零陵" not in district:
        return f"未检索到「{district}」的景点数据，请换一个区县"
    return (
        "1. 柳子庙 | 4A | 门票 60 元 | 建议 2 小时 | 唐代柳宗元纪念馆\n"
        "2. 零陵古城 | 4A | 门票 40 元 | 建议 3 小时 | 夜间灯光秀 19:30 开始\n"
        "3. 周家大院 | 3A | 门票 35 元 | 建议 1.5 小时 | 明清古建筑群\n"
        "4. 潇水风光带 | 免费 | 建议 1 小时 | 适合傍晚散步"
    )


def _get_weather(args: dict) -> str:
    """天气数据 handler。"""
    city = str(args.get("city") or "")
    if not city.strip():
        raise ValueError("city 不能为空")
    if "永州" in city or "零陵" in city:
        return "永州 零陵区：多云转小雨，22~28℃，东南风 2 级，建议带伞"
    return f"暂无「{city}」的天气数据"


def _calc_ticket_price(args: dict) -> str:
    """门票计算 handler。"""
    base = _to_double(args.get("basePrice"), "basePrice")
    people = int(_to_double(args.get("people"), "people"))
    discount = 1.0 if args.get("discount") is None else _to_double(args.get("discount"), "discount")
    return (f"门票 = {base:.0f} 元/人 × {people} 人 × {discount:.2f} 折 = "
            f"{base * people * discount:.2f} 元")


def _to_double(value, name: str) -> float:
    """把模型给的参数转成 float。

    模型有时会把数字写成字符串（"60"），所以这里不能直接强转，必须兼容字符串。
    """
    if value is None:
        raise ValueError(f"缺少必填参数: {name}")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} 必须是数字，实际收到: {value}") from None


# ============================================================
# --demo / --code-demo：代码编辑离线演示（对应 Java 版 CodingDemo）
# ============================================================

# 演示靶子文件与「未实现折扣」的初始内容。每次演示前重置，保证可重复运行
SAMPLE_PATH = "samples/InvoiceService.py"
_SAMPLE = '''"""发票计价示例，供代码编辑能力演示使用。

演示时本文件会被重置为「未实现折扣」的版本，随后由 agent 修改。
"""


class InvoiceService:
    """发票计价服务。"""

    def total(self, base_price: float, quantity: int, discount: float = 0.0) -> float:
        """计算发票总价：单价 × 数量。"""
        # 目前只按原价乘数量，折扣参数未生效
        return base_price * quantity
'''


def coding_demo(args: list[str]) -> None:
    """离线演示：用脚本化假模型走一遍「读文件 → 精确替换 → 语法校验 → 输出差异」。

    背后的 agent 只有三个工具：list_files → read_code → edit_code。
    没有 read_code 就一定改不对，这是代码类 agent 和聊天类 agent 最大的区别。
    """
    root = Path.cwd()
    sample = root / SAMPLE_PATH
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(_SAMPLE, encoding="utf-8")
    print(f"已重置演示文件: {SAMPLE_PATH}\n")

    llm = ScriptedLlmClient.of(
        [
            ScriptedLlmClient.call("call_1", "read_code", {"path": SAMPLE_PATH}),
            ScriptedLlmClient.call("call_2", "edit_code", {
                "path": SAMPLE_PATH,
                "op": "replace",
                "anchor": "        # 目前只按原价乘数量，折扣参数未生效\n        return base_price * quantity",
                "newCode": ("        # 折扣按比例计入（0.8 表示八折）\n"
                            "        return base_price * quantity * discount"),
            }),
        ],
        lambda ctx: "已修改 InvoiceService.total 方法：折扣参数正式生效，"
                    "保持原有的中文注释与 4 空格缩进风格。",
    )

    agent = _coding_agent(root, llm)
    task = f"让 total 方法支持折扣参数\n\n目标文件: {SAMPLE_PATH}"
    answer = agent.run(task)
    print("\n================ 修改结果 ================")
    print(answer)
    print("=========================================")


def _coding_agent(root: Path, llm) -> Agent:
    """组装代码编辑 agent：三个工具 + 一份严格的编辑规范。

    提示词里那 7 条规则不是装饰，每一条都对应一个真实的失败模式：
    不读原文就改 → 锚点对不上；顺手重构 → diff 里混入无关改动；
    自己加缩进 → 缩进层级错乱；盲目重试 → 同一处反复报错。
    """
    tools = ToolRegistry().register(
        list_tool(root),
        read_tool(root),
        CodeEditTool(root),
    )
    return Agent(
        "代码助手",
        llm=llm,
        tools=tools,
        system_prompt=(
            "你是代码编辑助手，负责按指令精确修改已有代码。\n"
            "工作规则：\n"
            "1. 不知道文件在哪，先 list_files；不确定原文，必须先 read_code。\n"
            "2. 只改指令要求的范围，禁止顺手重构、禁止重写整个文件。\n"
            "3. anchor 必须从 read_code 的结果里逐字复制（含缩进），带足够上下文保证唯一。\n"
            "4. 保持原文件的风格：缩进宽度、注释语言、命名习惯。\n"
            "5. newCode 不要自己加最外层公共缩进，工具会自动对齐到锚点位置。\n"
            "6. 改完在回答里说明：改了哪个方法的第几行、为什么这么改。\n"
            "7. 如果 edit_code 返回 ERROR（锚点不唯一、校验失败等），先读文件再修正，"
            "不要盲目重试同一个 anchor。"
        ),
        hooks=[LoggingHook()],
        max_steps=10,
    )
