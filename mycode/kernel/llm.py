"""
模型客户端端口（对应 Java 版 LlmClient / StreamSink）。

整个项目里只有这一个接口需要跟「厂商」打交道，换模型只需换实现类，
主循环、工具层、上下文层一行都不用改。
这也是 LangChain / Spring AI 都要先做一层 ChatModel 抽象的原因。

为什么放在 kernel 而不是 clients：Agent（内核）依赖本接口，
而分层铁律规定 kernel 禁止 import 上层 —— 所以「端口」必须定义在内核，
clients/ 里的各个实现类反过来实现它（依赖倒置）。

现有实现（见 mycode/clients/）：
    OpenAiCompatibleClient —— OpenAI 兼容协议（OpenAI、DeepSeek、Kimi、智谱、通义、Ollama）
    AnthropicClient        —— Claude 原生 Messages 协议
    ScriptedLlmClient      —— 假模型，用于离线验证循环
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from mycode.kernel.context import AgentContext
from mycode.kernel.tool import Tool
from mycode.kernel.types import ChatResult


class StreamSink(Protocol):
    """流式输出回调（结构化类型，实现方只需提供 on_text 一个方法）。

    模型边生成边回调，界面就能边收到边打印 ——
    用户不用对着黑屏等十秒，也顺便解决了「不知道它是不是卡死了」的焦虑。

    故意做得极简：只有 on_text 一个方法。
    工具调用不走这里，因为工具调用是「攒完才能执行」的，
    边收边执行会拿半个参数去调工具，那是灾难。
    """

    def on_text(self, delta: str) -> None:
        """收到一段文本增量，可能只有一两个字，也可能是一整段。"""
        ...


def as_sink(sink: StreamSink | None) -> StreamSink | None:
    """把两种回调形态统一成 StreamSink：实现了 on_text 的对象，或裸函数。

    Java 版的 StreamSink 是函数式接口，lambda 可以直接当 sink；
    Python 版保留同样的便利：直接传 lambda delta: print(delta) 也能用。
    """
    if sink is None or hasattr(sink, "on_text"):
        return sink

    class _FnSink:
        """裸函数的适配器。"""

        def __init__(self, fn) -> None:
            self._fn = fn

        def on_text(self, delta: str) -> None:
            self._fn(delta)

    return _FnSink(sink)


class LlmClient(ABC):
    """模型调用抽象。"""

    @abstractmethod
    def chat(self, ctx: AgentContext, tools: list[Tool]) -> ChatResult:
        """发起一轮对话。

        实现方要做三件事：把上下文转成该厂商的报文格式、发请求、把响应解析成
        ChatResult。注意不要在这里处理「要不要继续循环」—— 那是 Agent 的职责。

        tools 为空列表表示这一轮不给工具。
        """

    def chat_stream(
        self,
        ctx: AgentContext,
        tools: list[Tool],
        sink: StreamSink | None,
    ) -> ChatResult:
        """流式版本：边生成边把文本增量推给 sink。

        默认实现退化成「一次性拿到结果再整段推给 sink」，
        这样不支持流式的客户端（比如 AnthropicClient）不用写任何代码就能用上
        这个接口，代价只是没有逐字效果。真正的流式由具体客户端覆盖实现。
        """
        result = self.chat(ctx, tools)
        normalized = as_sink(sink)
        if normalized is not None and result.text is not None:
            normalized.on_text(result.text)
        return result
