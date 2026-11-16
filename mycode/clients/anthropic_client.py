"""
Anthropic（Claude）原生 Messages API 客户端（对应 Java 版 AnthropicClient）。

它跟 OpenAI 协议有 6 处硬性差异，一处处理错就直接 400，全部在这里转换掉：

    | 项目        | OpenAI 兼容                    | Anthropic                                  |
    |------------|-------------------------------|---------------------------------------------|
    | 端点        | /v1/chat/completions          | /v1/messages                                |
    | 鉴权        | Authorization: Bearer sk-xxx  | x-api-key: sk-ant-xxx + anthropic-version   |
    | system     | 一条 role=system 的消息        | 顶层 system 字段，不在 messages 里           |
    | 工具声明    | {type:function, function:{}}  | {name, description, input_schema}           |
    | 工具回填    | role:"tool" + tool_call_id    | role:"user" + content[{type:tool_result}]   |
    | max_tokens | 可选                          | 必填                                        |

另有一条隐形规则最坑：messages 必须 user/assistant 交替，
模型一次调 3 个工具会产生 3 条结果，必须合并进同一条 user 消息，否则报错。

当前实现只覆盖一次性请求：流式走 LlmClient.chat_stream 的默认退化实现
（整段推送）。Claude 的 SSE 是事件驱动（content_block_delta 等多种事件），
留到后续版本再加。
"""

from __future__ import annotations

import json

from mycode.kernel.context import AgentContext
from mycode.kernel.llm import LlmClient
from mycode.kernel.tool import Tool
from mycode.kernel.types import ChatResult, ToolCall, Usage
from mycode.clients import net

# 协议版本，写死官方当前版本；不写这个头 Anthropic 直接 400
_API_VERSION = "2023-06-01"
_TIMEOUT_SECONDS = 120


class AnthropicClient(LlmClient):
    """Claude 原生协议客户端，核心价值在 _to_anthropic_messages 的协议转换。"""

    def __init__(self, api_key: str, model: str,
                 endpoint: str = "https://api.anthropic.com/v1/messages",
                 max_tokens: int = 4096) -> None:
        self.api_key = api_key        # Claude 的密钥（sk-ant- 开头），走 x-api-key 头
        self.model = model            # 模型名，例如 claude-sonnet-4-5
        self.endpoint = endpoint      # 完整请求地址（/v1/messages），不是 baseUrl
        self.max_tokens = max_tokens  # Anthropic 强制要求，缺了直接 400

    # ---------------- 报文构造 ----------------

    def build_request_json(self, ctx: AgentContext, tools: list[Tool]) -> str:
        """组装请求报文。单独暴露，方便用 --dump 和 OpenAI 报文逐字段对比。"""
        system, messages = self._to_anthropic_messages(ctx.messages)

        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,          # 必填，少写就是 400
        }
        if system:
            body["system"] = system                  # 顶层字段，不进 messages
        body["messages"] = messages

        if tools:
            declarations = []
            for tool in tools:
                declarations.append({
                    "name": tool.name(),
                    "description": tool.description(),
                    # schema 排序保证跨重启字节恒定，与 OpenAiCompatibleClient 同一套规则
                    # 注意：字段名是 input_schema，不是 parameters
                    "input_schema": Tool.canonical(tool.parameters()),
                })
            body["tools"] = declarations
            body["tool_choice"] = {"type": "auto"}   # 注意：是对象不是字符串
        return json.dumps(body, ensure_ascii=False)

    def chat(self, ctx: AgentContext, tools: list[Tool]) -> ChatResult:
        """发起一轮对话。

        鉴权不用 Bearer，而是两个专用头：x-api-key（密钥）+
        anthropic-version（协议版本，不写会 400）。
        """
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
        }
        conn, path = net.open_connection(self.endpoint, _TIMEOUT_SECONDS)
        try:
            conn.request("POST", path, body=self.build_request_json(ctx, tools).encode("utf-8"),
                         headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"Anthropic HTTP {resp.status} -> {net.read_body(resp)[:400]}")
            root = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        return self.parse(root)

    # ---------------- 响应解析 ----------------

    def parse(self, root: dict) -> ChatResult:
        """解析响应报文。

        结构：content 是一个块数组，可能混着 text 块和 tool_use 块，
        所以不能用「取第一个字段」的思路，必须逐个块遍历。
        """
        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for block in root.get("content") or []:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block_type == "tool_use":
                # tool_use.input 必须是 JSON 对象；args_json 是它的字符串形态，
                # 回填 assistant 消息时要用（与 OpenAI 的 arguments 字符串语义对齐）
                input_obj = block.get("input") or {}
                args_json = json.dumps(input_obj, ensure_ascii=False)
                calls.append(ToolCall(
                    str(block.get("id") or ""),
                    str(block.get("name") or ""),
                    args_json,
                    parse_tool_args(args_json),
                ))

        # stop_reason = tool_use 表示要执行工具，end_turn 表示说完了。
        # 用量字段名和 OpenAI 不同：input_tokens / output_tokens / cache_read_input_tokens
        usage_node = root.get("usage") or {}
        usage = Usage(
            int(usage_node.get("input_tokens") or 0),
            int(usage_node.get("output_tokens") or 0),
            int(usage_node.get("cache_read_input_tokens") or 0),
        )
        text = "".join(text_parts)
        return ChatResult(text if text else None, calls, usage)

    # ---------------- 协议转换核心 ----------------

    def _to_anthropic_messages(self, source: list[dict]) -> tuple[str, list[dict]]:
        """把 OpenAI 形态的消息数组转换成 Anthropic 形态。

        返回 (system 文本, messages 数组)。转换规则：
          system    -> 不进 messages，拼成顶层 system 字符串
          assistant -> content 文本 + tool_calls 都转成块
          tool      -> 转成 user 消息里的 tool_result 块
          user      -> 纯文本或块数组（--image 存的 OpenAI 形态）都转成块
        相邻同角色的消息必须合并（尤其并行工具结果），否则 Anthropic 报 400。
        """
        system_parts: list[str] = []
        out: list[dict] = []

        for m in source:
            role = str(m.get("role"))

            if role == "system":
                content = m.get("content")
                if content is not None and str(content).strip():
                    system_parts.append(str(content))
                continue  # system 不进 messages

            out_role: str
            blocks: list[dict] = []

            if role == "assistant":
                out_role = "assistant"
                txt = "" if m.get("content") is None else str(m.get("content"))
                if txt.strip():
                    blocks.append(_text_block(txt))
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    blocks.append({
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": fn.get("name"),
                        "input": _parse_input(str(fn.get("arguments") or "{}")),
                    })
            elif role == "tool":
                # 工具结果在 Anthropic 里是 user 消息里的 tool_result 块
                out_role = "user"
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id"),
                    "content": "" if m.get("content") is None else str(m.get("content")),
                })
            else:
                out_role = "user"
                # content 可能是纯文本，也可能是带图片的块数组（--image 存的 OpenAI 形态）
                _append_user_blocks(m.get("content"), blocks)

            if not blocks:
                continue  # Anthropic 不接受空 content

            # 相邻同角色必须合并，尤其是并行工具结果
            if out and out_role == out[-1]["role"]:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": out_role, "content": blocks})

        return "\n\n".join(system_parts), out


def _text_block(text: str) -> dict:
    """构造一个 text 文本块。Anthropic 的 content 是块数组，纯文本也必须包成块。"""
    return {"type": "text", "text": text}


def _append_user_blocks(content, blocks: list[dict]) -> None:
    """user 消息的 content 转成 Anthropic 块。

    纯文本 → 一个 text 块；块数组（OpenAI 形态）逐个转换：
    text → text 块，image_url 的 data URL → base64 图片块。
    """
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                blocks.append(_text_block(str(part.get("text") or "")))
            elif part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
                _add_image_block(str(part["image_url"].get("url") or ""), blocks)
        return
    blocks.append(_text_block("" if content is None else str(content)))


def _add_image_block(data_url: str, blocks: list[dict]) -> None:
    """把 data:image/png;base64,xxxx 形态的 URL 转成 Anthropic 的图片块；非 base64 的 URL 跳过。"""
    prefix, marker = "data:", ";base64,"
    if not data_url.startswith(prefix) or marker not in data_url:
        return
    comma = data_url.index(marker)
    blocks.append({
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": data_url[len(prefix):comma],
            "data": data_url[comma + len(marker):],
        },
    })


def _parse_input(args_json: str) -> dict:
    """tool_use.input 必须是 JSON 对象，不能是字符串；解析失败给空对象兜底。"""
    try:
        node = json.loads(args_json)
        return node if isinstance(node, dict) else {}
    except Exception:
        return {}
