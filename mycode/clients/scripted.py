"""
脚本化假模型（对应 Java 版 ScriptedLlmClient）：按预设顺序返回工具调用，
脚本走完就输出最终答案。

用途是「先验证控制流，再接真模型」。调 agent 时 80% 的 bug 出在循环、
上下文拼接、工具分发上，跟模型本身没关系。用假模型把这些跑对，剩下就好办了。

典型用法：
    ScriptedLlmClient.of(
        [ScriptedLlmClient.call("call_1", "read_code", {"path": "A.py"}),
         ScriptedLlmClient.call("call_2", "edit_code", {"op": "replace", ...})],
        lambda ctx: "改好了")
"""

from __future__ import annotations

import json

from mycode.kernel.context import AgentContext
from mycode.kernel.llm import LlmClient
from mycode.kernel.tool import Tool
from mycode.kernel.types import ChatResult, ToolCall


class ScriptedLlmClient(LlmClient):
    """按脚本顺序吐工具调用，脚本走完吐最终答案。"""

    def __init__(self, script: list[ToolCall], answer) -> None:
        self._script = script            # 预设的工具调用序列，按顺序依次吐出
        self._answer = answer            # 脚本走完后用来生成最终回答的函数，入参是当前上下文
        self._cursor = 0                 # 脚本游标，记录下一条该吐哪个调用

    @classmethod
    def of(cls, script: list[ToolCall], answer) -> "ScriptedLlmClient":
        """工厂方法：脚本 + 最终回答生成函数。"""
        return cls(script, answer)

    def chat(self, ctx: AgentContext, tools: list[Tool]) -> ChatResult:
        """假模型的一轮「推理」：还有脚本就返回工具调用，没有就返回最终答案。

        注意完全不看模型能力、不联网，纯粹用来验证主循环。
        """
        if self._cursor < len(self._script):
            call = self._script[self._cursor]
            self._cursor += 1
            return ChatResult.of_tools([call])
        return ChatResult.of_text(self._answer(ctx))

    # ---------------- 便捷构造 ----------------

    @staticmethod
    def call(tool_id: str, name: str, args: dict) -> ToolCall:
        """便捷构造一次工具调用，省去手写 JSON 字符串。

        同时构造两份数据：args_json（协议层要原样回填）和 args（本地工具直接用）。
        json.dumps 自带转义，代码片段里的换行和引号不会造出非法 JSON。
        """
        args_json = json.dumps(args, ensure_ascii=False)
        return ToolCall(tool_id, name, args_json, dict(args))

    @staticmethod
    def last_observation(ctx: AgentContext) -> str:
        """取最后一次工具返回。

        方便假模型把 observation 原样反射进最终答案，证明「结果确实回填成功了」。
        """
        for msg in reversed(ctx.messages):
            if msg.get("role") == "tool":
                return str(msg.get("content"))
        return "(无)"
