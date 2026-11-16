"""
协议对照工具（对应 Java 版 RequestDump）：把同一段对话分别按 OpenAI 和
Anthropic 两种格式渲染出来。

这是本项目里最省钱的调试手段 —— 不用调真 API 就能确认协议转换写对了没有。
特别要看两点：
    1. system 提示词去哪了（OpenAI 在 messages 里，Anthropic 在顶层字段）
    2. 两条并行工具结果有没有被合并成一条 user 消息（Anthropic 的硬性要求）

    python -m mycode --dump
"""

from __future__ import annotations

import json

from mycode.kernel.context import AgentContext
from mycode.kernel.tool import SimpleTool, Tool
from mycode.kernel.types import ToolCall
from mycode.clients.anthropic_client import AnthropicClient
from mycode.clients.openai_client import OpenAiCompatibleClient


def run() -> None:
    """打印两种协议的请求报文。构造的对话刻意包含「并行工具调用」，
    这是两种协议差异最大的地方。全程不联网，纯本地拼装。"""
    # 1. 手工拼一段对话：system → user → assistant(两个工具调用) → 两条工具结果
    ctx = AgentContext.of("你是乡村文旅助手。")
    ctx.user("零陵区有什么景点？两个人要多少钱？")
    ctx.assistant("我先查一下景点和价格。", [
        ToolCall("call_1", "search_spots", '{"district": "零陵区"}', {"district": "零陵区"}),
        ToolCall("call_2", "get_weather", '{"city": "永州"}', {"city": "永州"}),
    ])
    ctx.tool_result("call_1", "search_spots", "柳子庙 60 元；零陵古城 40 元")
    ctx.tool_result("call_2", "get_weather", "多云 22~28℃")

    # 2. 准备两个工具声明，只为让报文里的 tools 字段有内容。
    #    用 SimpleTool 直接给声明（与 Java 版用真工具不同）：这里关心的只是
    #    协议字段怎么转换，SimpleTool 的声明结构与真工具完全一致
    tools: list[Tool] = [
        SimpleTool("web_search", "联网搜索，输入关键词返回若干条结果",
                   Tool.schema({"query": Tool.str_param("搜索关键词")}, "query"),
                   lambda args: "(演示占位)"),
        SimpleTool("fetch_page", "抓取网页正文",
                   Tool.schema({"url": Tool.str_param("网页地址")}, "url"),
                   lambda args: "(演示占位)"),
    ]

    # 3. 分别渲染两种报文
    print("============ OpenAI 兼容格式 ============")
    print(_pretty(OpenAiCompatibleClient(
        "https://api.openai.com/v1/chat/completions", "", "gpt-4.1").build_request_json(ctx, tools)))

    print("\n============ Anthropic 原生格式 ============")
    print(_pretty(AnthropicClient("", "claude-sonnet-4-5").build_request_json(ctx, tools)))

    # 4. 顺手把差异点列出来，免得读者自己对比
    print("""

============ 重点看这几处差异 ============
1. system 在 OpenAI 里是 messages[0]，在 Anthropic 里是顶层 system 字段；
2. 工具声明：OpenAI 用 function.parameters，Anthropic 用 input_schema；
3. 两条工具结果：OpenAI 是两条 role=tool 消息，
   Anthropic 必须合并成一条 role=user 消息里的两个 tool_result 块；
4. Anthropic 强制要求 max_tokens。
""")


def _pretty(json_text: str) -> str:
    """JSON 美化输出。报文里嵌套很深，不格式化基本没法看。"""
    return json.dumps(json.loads(json_text), ensure_ascii=False, indent=2)
