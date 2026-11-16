"""
内核数据类：全项目共享的数据定义，只有这一个地方（防冲突的核心约定）。

包含三个 record / dataclass：

    Usage       一次模型调用的 token 用量（含前缀缓存命中数，供 /cost 折算）
    ToolCall    模型发出的一次工具调用（原始参数串必须原样保留）
    ChatResult  模型一次调用的结果：纯文本 / 工具调用 / 两者都有

这里刻意不定义消息结构 —— 会话消息直接用 dict（键序固定），见 context.py 的说明。
模型客户端的接口（LlmClient / StreamSink）在 llm.py，不属于数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Usage:
    """一次模型调用的 token 用量。

    cache_read_tokens 是输入中命中服务商「前缀缓存」的部分（DeepSeek 的
    prompt_cache_hit_tokens、OpenAI 的 prompt_tokens_details.cached_tokens、
    Anthropic 的 cache_read_input_tokens）。注意 prompt_tokens 口径已含命中部分，
    也就是「输入 = 全部提示词」，其中一段走了便宜的缓存价。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0

    def total(self) -> int:
        """输入 + 输出，界面展示用的总量。"""
        return self.prompt_tokens + self.completion_tokens

    def plus(self, other: "Usage | None") -> "Usage":
        """会话累计：把另一段用量加进来；other 为 None 时原样返回。"""
        if other is None:
            return self
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


# 离线假模型 / 服务商不给 usage 时的兜底值
Usage.ZERO = Usage()


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型要求执行的一次工具调用。

    args_json 是模型返回的「原始参数串」，必须原样保留：OpenAI 协议要求
    assistant 消息里的 tool_calls 原样回传，一旦重新序列化（字段顺序变了、
    数字格式变了），部分服务商的接口就会报 400。args 只是给本地代码用的
    便利视图（已解析成 dict），两者并存、各司其职。
    """

    id: str
    name: str
    args_json: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatResult:
    """模型一次调用的结果，三种形态：

        1. 只有文字          —— 一般就是最终答案，主循环到此收工
        2. 只有工具调用       —— content 常为空串，主循环继续执行工具
        3. 文字 + 工具调用    —— 模型边说边做，两者都保留
    """

    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage = Usage.ZERO

    @classmethod
    def of_text(cls, text: str) -> "ChatResult":
        """构造纯文本结果（离线假模型 / 简单场景用）。"""
        return cls(text=text, tool_calls=[], usage=Usage.ZERO)

    @classmethod
    def of_tools(cls, calls: list[ToolCall]) -> "ChatResult":
        """构造纯工具调用结果。"""
        return cls(text=None, tool_calls=list(calls), usage=Usage.ZERO)

    @property
    def has_tool_calls(self) -> bool:
        """主循环靠它决定「继续执行工具」还是「给出最终答案」。"""
        return len(self.tool_calls) > 0
