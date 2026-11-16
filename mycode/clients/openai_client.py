"""
通用 OpenAI 兼容客户端（对应 Java 版 OpenAiCompatibleClient）。

全世界绝大多数模型服务都实现了 /v1/chat/completions 这套协议：
OpenAI、DeepSeek、Kimi、智谱、通义（DashScope 兼容模式）、本地 Ollama / vLLM。
所以只要把 endpoint 参数化，一份代码就能接完，这就是「兼容层」的价值。

报文结构要点：
    工具声明   tools -> [{type:"function", function:{name, description, parameters}}]
    工具调用   assistant 消息里的 tool_calls 数组，每项带 id
    结果回填   role:"tool" + tool_call_id

HTTP 用标准库 http.client 直连（见 net.py 的取舍说明）。
"""

from __future__ import annotations

import json

from mycode.kernel.context import AgentContext
from mycode.kernel.llm import LlmClient, StreamSink, as_sink
from mycode.kernel.tool import Tool
from mycode.kernel.types import ChatResult, ToolCall, Usage
from mycode.clients import net
from mycode.clients.sse import SseAccumulator, cached_tokens, parse_tool_args

# 单次请求超时：推理型模型 + 长上下文时，响应超过 60 秒很常见，
# 超时设太短会出现「请求其实成功了，但本地已经断开」的情况
_TIMEOUT_SECONDS = 120
_STREAM_TIMEOUT_SECONDS = 180


class OpenAiCompatibleClient(LlmClient):
    """一份代码接完所有 OpenAI 兼容服务。"""

    def __init__(self, endpoint: str, api_key: str, model: str) -> None:
        self.endpoint = endpoint        # 完整请求地址（不是 baseUrl）
        self.api_key = api_key          # 本地 Ollama 这类服务可以传空串
        self.model = model              # 模型名，例如 gpt-4.1、deepseek-chat、qwen3-max

    # ---------------- 报文构造 ----------------

    def build_request_json(self, ctx: AgentContext, tools: list[Tool]) -> str:
        """组装请求报文 JSON。

        单独抽成公开方法有两个好处：
          1. --dump 模式下可以只打印报文、不发请求，白嫖协议调试；
          2. 出问题时可以直接把这段 JSON 粘到 curl / Postman 里复现。
        """
        return json.dumps(self.build_request_body(ctx, tools), ensure_ascii=False)

    def build_request_body(self, ctx: AgentContext, tools: list[Tool]) -> dict:
        """组装请求体 dict。抽出来是为了流式和非流式共用同一套构造逻辑。"""
        body: dict = {
            "model": self.model,
            # 上下文消息本身就按 OpenAI 协议存的，直接序列化即可，不用转换
            "messages": ctx.messages,
        }
        if tools:
            wrappers = []
            for tool in tools:
                # 注意 OpenAI 的结构比 Anthropic 多包了一层 function
                wrappers.append({
                    "type": "function",
                    "function": {
                        "name": tool.name(),
                        "description": tool.description(),
                        # schema 要先 canonical 排序再序列化，保证跨重启字节恒定（见 Tool.canonical）
                        "parameters": Tool.canonical(tool.parameters()),
                    },
                })
            body["tools"] = wrappers
            # auto 表示由模型自己决定调不调工具；传 none 则禁用，传 required 则强制调
            body["tool_choice"] = "auto"
        # qwen3 系列默认开思考模式，配合工具调用会明显变慢，演示时先关掉
        if self.model.startswith("qwen3"):
            body["enable_thinking"] = False
        return body

    # ---------------- 一次性请求 ----------------

    def chat(self, ctx: AgentContext, tools: list[Tool]) -> ChatResult:
        """发起一轮对话。"""
        headers = {"Content-Type": "application/json"}
        # 本地服务（如 Ollama）不需要鉴权，空 key 就不发这个头
        if self.api_key and self.api_key.strip():
            headers["Authorization"] = "Bearer " + self.api_key
        payload = self._post_json(self.build_request_json(ctx, tools), headers, _TIMEOUT_SECONDS)
        return self.parse(payload)

    def _post_json(self, json_text: str, headers: dict, timeout: int) -> dict:
        """发 POST 并把响应解析成 dict；非 200 抛带状态码与响应体摘要的 RuntimeError。"""
        conn, path = net.open_connection(self.endpoint, timeout)
        try:
            conn.request("POST", path, body=json_text.encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                # 带上响应体前 300 字，401/400 时能直接看出是 key 错了还是参数不对
                raise RuntimeError(f"HTTP {resp.status} -> {net.read_body(resp)[:300]}")
            return json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    # ---------------- 流式请求 ----------------

    def chat_stream(self, ctx: AgentContext, tools: list[Tool], sink: StreamSink | None) -> ChatResult:
        """流式对话：边生成边把文本推给 sink。

        与一次性请求的三处差异：
          1. body 要带 stream=true；想拿用量还得加 stream_options.include_usage
             （个别代理不认这个字段，会返回 400，所以做成失败后自动去掉重试一次）；
          2. 响应是 SSE 文本流，要逐行读、剥掉 "data: " 前缀、遇到 [DONE] 结束；
          3. tool_calls 的 arguments 是分片下发的，交给 SseAccumulator 按 index 拼。
        """
        sink = as_sink(sink)  # 允许调用方直接传裸函数（对应 Java 的函数式接口）
        body = self.build_request_body(ctx, tools)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key and self.api_key.strip():
            headers["Authorization"] = "Bearer " + self.api_key

        try:
            conn, resp = self._open_stream(json.dumps(body, ensure_ascii=False), headers)
        except RuntimeError as exc:
            message = str(exc)
            if "HTTP 400" in message and "stream_options" in message:
                # 兼容层不支持 stream_options 时，去掉重试一次，宁可没有用量也别整轮失败
                body.pop("stream_options", None)
                conn, resp = self._open_stream(json.dumps(body, ensure_ascii=False), headers)
            else:
                raise

        acc = SseAccumulator(sink.on_text if sink is not None else None)
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue  # 空行与心跳注释
                if not line.startswith("data:"):
                    continue  # event: 之类的行忽略
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                acc.accept(data)
        finally:
            resp.close()
            conn.close()
        return ChatResult(acc.text(), acc.tool_calls(), acc.usage())

    def _open_stream(self, json_text: str, headers: dict):
        """发一个流式请求，返回 (连接, 响应)；非 200 抛 RuntimeError。

        返回连接本身是因为流式要逐行读很久，读完才能关；
        出错路径也必须关连接 —— stream_options 的重试路径每次都可能走到这里，
        不关就会一路泄漏 socket。
        """
        conn, path = net.open_connection(self.endpoint, _STREAM_TIMEOUT_SECONDS)
        conn.request("POST", path, body=json_text.encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            body = net.read_body(resp)
            conn.close()
            raise RuntimeError(f"HTTP {resp.status} -> {body[:300]}")
        return conn, resp

    # ---------------- 响应解析 ----------------

    def parse(self, root: dict) -> ChatResult:
        """解析响应报文。

        结构：choices[0].message.content 是文本，.tool_calls[] 是工具调用列表。
        """
        choices = root.get("choices") or [{}]
        message = (choices[0] or {}).get("message") or {}
        text = message.get("content")
        text = None if text is None else str(text)

        calls: list[ToolCall] = []
        for c in message.get("tool_calls") or []:
            fn = c.get("function") or {}
            # arguments 是「字符串形式的 JSON」，不是对象，这一点经常被搞错
            args_json = str(fn.get("arguments") or "{}")
            calls.append(ToolCall(
                str(c.get("id") or ""),
                str(fn.get("name") or ""),
                args_json,
                parse_tool_args(args_json),
            ))

        # 用量在顶层 usage 字段里，取不到就按 0 处理（部分代理会省略这个字段）
        usage_node = root.get("usage") or {}
        usage = Usage(
            int(usage_node.get("prompt_tokens") or 0),
            int(usage_node.get("completion_tokens") or 0),
            cached_tokens(usage_node),
        )
        return ChatResult(text, calls, usage)
