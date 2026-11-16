"""
SSE 增量累加器（对应 Java 版 SseAccumulator）+ 工具参数解析公共件。

流式响应比一次性响应麻烦得多，三件事都得自己处理：
    1. 文本是分片的，直接拼起来就行；
    2. tool_calls 更是分片的 —— 第一片只有 id 和函数名，参数是后续一片片追加的，
       所以必须按 index 分组，把 arguments 逐片拼成一个完整 JSON 字符串；
    3. usage 只在最后一个 chunk 里出现，前面的 chunk 都没有。

把累加逻辑单独抽出来，是为了能脱离网络做验证：喂几段假 JSON 进去，
断言拼出来的结果对不对（--dump / 冒烟测试就这么做）。
"""

from __future__ import annotations

import json

from mycode.kernel.types import ToolCall, Usage


def parse_tool_args(args_json: str) -> dict:
    """把模型返回的参数字符串解析成 dict。

    模型偶尔会吐出不合法 JSON（比如字符串里带未转义的引号），
    这里吞掉异常返回空 dict，让工具自己报「缺参数」，而不是让整个循环崩掉。
    """
    try:
        node = json.loads(args_json)
        return node if isinstance(node, dict) else {}
    except Exception:
        return {}  # 故意忽略：交给工具层的参数校验去报错，信息更准确


def cached_tokens(usage_node: dict) -> int:
    """从 usage 节点里提取「命中前缀缓存的输入 token 数」。

    同一件事各家字段名不同，在这里统一兼容：
        DeepSeek：usage.prompt_cache_hit_tokens
        OpenAI ：usage.prompt_tokens_details.cached_tokens
    其他厂商不返回该字段时按 0 处理，只影响成本展示，不影响主流程。
    """
    hit = usage_node.get("prompt_cache_hit_tokens") or 0
    if int(hit) > 0:
        return int(hit)
    details = usage_node.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0)


class SseAccumulator:
    """把一串 data 片段拼成一个完整的 ChatResult。"""

    def __init__(self, on_text=None) -> None:
        """on_text：收到文本增量时的回调，传 None 表示只关心最终结果。"""
        self._on_text = on_text
        self._text_parts: list[str] = []
        # 按 index 分组的工具调用片段。dict 自带插入序，顺序与下发一致
        self._partials: dict[int, _Partial] = {}
        self._usage = Usage.ZERO

    def accept(self, data_json: str) -> None:
        """吃进一条 data 负载（不含 "data:" 前缀）的完整 JSON 字符串。"""
        if not data_json or not data_json.strip():
            return
        try:
            root = json.loads(data_json)
        except Exception:
            return  # 半个 JSON 或心跳包，跳过
        if not isinstance(root, dict):
            return

        # 1. 用量：通常只在最后一个 chunk 出现
        usage_node = root.get("usage")
        if isinstance(usage_node, dict):
            self._usage = Usage(
                int(usage_node.get("prompt_tokens") or 0),
                int(usage_node.get("completion_tokens") or 0),
                cached_tokens(usage_node),
            )

        choices = root.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        delta = (choices[0] or {}).get("delta") or {}

        # 2. 文本增量
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            self._text_parts.append(piece)
            if self._on_text is not None:
                self._on_text(piece)

        # 3. 工具调用增量：按 index 分组累积
        for tc in delta.get("tool_calls") or []:
            index = int(tc.get("index") or 0)
            partial = self._partials.setdefault(index, _Partial())
            if tc.get("id"):
                partial.id = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                partial.name = fn["name"]
            if fn.get("arguments"):
                partial.args.append(fn["arguments"])

    def text(self) -> str | None:
        """已拼好的完整文本。"""
        return "".join(self._text_parts) if self._text_parts else None

    def tool_calls(self) -> list[ToolCall]:
        """已拼好的工具调用列表。arguments 为空时给个合法空对象，避免下游解析炸掉。"""
        out: list[ToolCall] = []
        for partial in self._partials.values():
            if not partial.name:
                continue
            args_json = "".join(partial.args) or "{}"
            out.append(ToolCall(partial.id, partial.name, args_json, parse_tool_args(args_json)))
        return out

    def usage(self) -> Usage:
        """用量，没收到就是 0。"""
        return self._usage


class _Partial:
    """一次工具调用的未完成状态：id 和 name 只在第一片里有，arguments 分散在任意多片里。"""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.args: list[str] = []
