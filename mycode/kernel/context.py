"""
内核部件 2：上下文 / 记忆（对应 Java 版 AgentContext）。

内部直接维护 OpenAI 协议的消息数组（dict 列表，键序固定），好处是换模型厂商时
只需要换 LlmClient，这里一行不用改（AnthropicClient 会自己做协议转换）。

为什么用 dict 而不是自定义 Message 类：DeepSeek 等服务商的前缀缓存按请求报文的
「字节级前缀」匹配。dict 的插入顺序即序列化顺序，只要每次都按 role → content
的固定顺序构造，重启进程后序列化字节恒定，公共前缀仍能命中缓存。

消息顺序铁律：
    system → user → assistant(带 tool_calls) → tool(带 tool_call_id) → assistant → tool → ...

assistant 和随后的 tool 消息必须成对出现：少一条 tool，或 tool_call_id 对不上，
大部分厂商的接口会直接返回 400。这是接真模型时最高频的坑。
"""

from __future__ import annotations

import base64
from pathlib import Path

# system 里摘要段的拼接标记，restore 靠它把摘要从持久化的 system 里剥出来
_SUMMARY_MARKER = "\n\n【此前对话摘要】\n"
_SUMMARY_NOTE = "\n（以上是更早对话的压缩摘要，细节可能有丢失，需要时请重新查证。）"

# 消息里常用的键名（集中定义，避免散落的魔法字符串）
ROLE = "role"
CONTENT = "content"
TOOL_CALLS = "tool_calls"
TOOL_CALL_ID = "tool_call_id"


def _msg(role: str, content) -> dict:
    """构造一条字段顺序固定的消息（role 在前，content 在后）。

    必须用普通 dict 且按固定顺序插入键：理由见模块 docstring —— 保前缀缓存的
    字节级稳定。每次都是新 dict，调用方可以放心改写。
    """
    return {ROLE: role, CONTENT: content}


class AgentContext:
    """会话上下文：消息历史 + 可覆盖的滚动摘要。"""

    def __init__(self, system_base: str) -> None:
        self._system_base = system_base
        self._summary = ""
        self._messages: list[dict] = [self._system_message()]

    # ---------------- 构造入口 ----------------

    @classmethod
    def of(cls, system_prompt: str) -> "AgentContext":
        """创建上下文，第一条消息是 system。

        注意 system 只能是第一条，且只能有一条 —— 多转发给部分厂商会报错。
        """
        return cls(system_prompt)

    @classmethod
    def restore(cls, messages: list[dict]) -> "AgentContext":
        """从持久化的消息列表恢复上下文（--continue 用）。

        存档里的第一条 system 可能已经嵌着一段历史摘要（上次会话压缩过），
        必须把它剥出来：system_base 只装「原始 system」，摘要仍走 set_summary
        这个单一入口 —— 否则下次压缩会在旧摘要上再叠一段，越压越长。
        """
        if not messages or messages[0].get(ROLE) != "system":
            raise ValueError("会话存档缺少 system 消息，无法恢复")
        system_content = str(messages[0].get(CONTENT, ""))
        system_base, summary = system_content, ""
        at = system_content.find(_SUMMARY_MARKER)
        if at >= 0:
            system_base = system_content[:at]
            summary = system_content[at + len(_SUMMARY_MARKER):]
            if summary.endswith(_SUMMARY_NOTE):
                summary = summary[: -len(_SUMMARY_NOTE)]
        ctx = cls(system_base)
        if summary.strip():
            ctx.set_summary(summary.strip())
        for raw in messages[1:]:
            ctx._messages.append(dict(raw))  # 信任存档、不做协议校验（--continue 场景）
        return ctx

    # ---------------- 消息构造 ----------------

    def _system_message(self) -> dict:
        """按「原始 system + 当前摘要」生成 system 消息。"""
        text = self._system_base
        if self._summary:
            text = self._system_base + _SUMMARY_MARKER + self._summary + _SUMMARY_NOTE
        return _msg("system", text)

    def user(self, text: str) -> None:
        """追加一条用户消息。"""
        self._messages.append(_msg("user", text))

    def user_with_image(self, text: str, image: Path) -> None:
        """追加一条带图片的用户消息（--image 用，二期多模态演示保留此能力）。

        content 按 OpenAI 协议的块数组形态存：[文字块, 图片块(data URL)]。
        OpenAI 兼容客户端把消息原样序列化，直接透传；Anthropic 协议由
        AnthropicClient 转换成本它的 content blocks。两家都能吃。
        """
        image = Path(image)
        if not image.is_file():
            raise ValueError(f"图片文件不存在: {image}")
        data = image.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("图片超过 4MB，先压缩再试")
        media = _media_type(image.name.lower())
        if media is None:
            raise ValueError(f"不支持的图片格式（支持 png/jpg/jpeg/gif/webp）: {image.name}")
        data_url = f"data:{media};base64," + base64.b64encode(data).decode()
        blocks = [
            {"type": "text", "text": text or ""},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        self._messages.append({ROLE: "user", CONTENT: blocks})

    def assistant(self, text: str | None, calls: list | None) -> None:
        """追加一条 assistant 消息（即模型这一轮的回复）。

        三种形态都要支持：只有文字 / 只有工具调用（content 留空串）/
        文字 + 工具调用。内层结构是 OpenAI 的嵌套形态
        {id, type, function:{name, arguments}}，其中 arguments 必须是字符串。
        """
        msg = {ROLE: "assistant", CONTENT: text or ""}
        if calls:
            arr = []
            for c in calls:
                # arguments 必须原样用模型返回的字符串，不能重新序列化成对象
                arr.append({
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.args_json},
                })
            msg[TOOL_CALLS] = arr
        self._messages.append(msg)

    def tool_result(self, call_id: str, tool_name: str, content: str) -> None:
        """把工具执行结果作为「观察」回填。

        这是 ReAct 循环里最关键的一步，也是新手最容易漏的一步 ——
        漏了它，模型下一轮完全不知道工具干了什么，会反复调同一个工具。

        注意：不要额外塞 name 字段，部分厂商的兼容层会因此报 400。
        tool_name 仅用于日志，不写进报文。
        """
        self._messages.append({ROLE: "tool", TOOL_CALL_ID: call_id, CONTENT: content})

    # ---------------- 读取与摘要 ----------------

    @property
    def messages(self) -> list[dict]:
        """直接暴露消息列表，供 LlmClient 序列化成报文。"""
        return self._messages

    @property
    def summary(self) -> str:
        """当前滚动摘要，可能为空字符串。"""
        return self._summary

    def set_summary(self, new_summary: str | None) -> None:
        """覆盖历史摘要，并立刻重建 system 消息。

        为什么不是「再插一条摘要消息」：那样每压缩一次就多一条，
        上下文还是会膨胀。改成覆盖 system 里的固定段落，摘要永远只占一份。
        """
        self._summary = (new_summary or "").strip()
        self._messages[0] = self._system_message()

    def size(self) -> int:
        """消息条数（含 system），压缩器判断「还能不能继续摘」时会用。"""
        return len(self._messages)

    # ---------------- 裁剪与估算 ----------------

    def estimate_tokens(self) -> int:
        """粗略估算上下文长度。

        中文一个字约等于 1 token，英文约 4 字符 1 token，这里统一按字符数估，
        够用就行。精确计算需要引分词器，对教学项目不值当。

        注意必须把 tool_calls 里的参数也算进来：assistant 消息的 content 常常
        是空串，真正的体积在 arguments 里（一次 edit_code 可能带几千字符的代码）。
        早期版本只统计 content，导致上下文被严重低估、裁剪永不触发，最终超限 400。
        """
        total = 0
        for m in self._messages:
            total += _content_chars(m.get(CONTENT))
            calls = m.get(TOOL_CALLS)
            if calls is not None:
                # str() 已经包含函数名和参数字符串，长度足够做估算
                total += len(str(calls))
        return total

    def drain_oldest_round(self, keep_recent: int) -> list[dict]:
        """摘掉最老的一轮对话，返回被摘掉的消息（交给压缩器做摘要）。

        一轮 = 从一条 user 消息开始，到下一个 user 消息之前的所有消息，
        中间可能夹着 assistant 的 tool_calls 和若干条 tool 结果。
        必须整轮摘：只删其中一条会出现「assistant 说要调工具、tool 结果却没了」
        的协议违规，大部分厂商会直接返回 400。
        """
        size = len(self._messages)
        if size <= 1 + keep_recent:
            return []
        limit = size - keep_recent  # 允许摘到的最右位置（含）：保留其后 keep_recent 条

        # 情况一：后面还有新一轮 user —— 把从 index 1 到下一个 user 之前整段摘掉。
        # 从 2 开始扫：index 1 自己就是本轮起点，从 1 开始会立刻命中自己（踩过一次的 off-by-one）
        for i in range(2, limit + 1):
            if self._messages[i].get(ROLE) == "user":
                return self._remove_range(1, i)

        # 情况二：只有一轮对话（典型场景：一次提问、连续多次工具调用）。
        # 长任务里没有「下一个 user」当边界，但早期那几组 assistant(tool_calls)+tool
        # 结果同样可以摘掉 —— 只要保住用户指令和最末几条。
        # 起点从 index 1 扫：因为只匹配 assistant，不会误删 user 指令本身。
        start = -1
        for i in range(1, limit + 1):
            if self._messages[i].get(ROLE) == "assistant":
                start = i
                break
        if start < 0:
            return []
        # 整组摘：从这组 assistant 开始，直到下一个 assistant 或 user 之前
        for i in range(start + 1, limit + 1):
            role = str(self._messages[i].get(ROLE))
            if role in ("assistant", "user"):
                return self._remove_range(start, i)
        return []

    def trim(self, max_tokens: int) -> None:
        """超长就丢最老的对话（最小实现，够教学用；更好的做法见 compressor.py）。

        保留前 3 条（system + 首轮问答）是为了不丢掉任务最初的意图。
        """
        while self.estimate_tokens() > max_tokens and len(self._messages) > 3:
            # 最后一条 user 指令就是任务本身，绝不能删 ——
            # 删了模型就不知道自己在干什么，会出现「答非所问」这种最难排查的表现
            if self._messages[1].get(ROLE) == "user" and self._count_role("user") <= 1:
                break
            self._messages.pop(1)
            # 如果下一条是 tool 消息，它的发起方已经被删了，一并删掉避免孤儿消息导致 400
            while len(self._messages) > 3 and self._messages[1].get(ROLE) == "tool":
                self._messages.pop(1)

    # ---------------- 内部工具 ----------------

    def _remove_range(self, start: int, end: int) -> list[dict]:
        """摘掉 [start, end) 区间并返回，供压缩器做摘要。"""
        removed = self._messages[start:end]
        del self._messages[start:end]
        return removed

    def _count_role(self, role: str) -> int:
        """统计某个角色的消息条数，trim 的保护逻辑要用。"""
        return sum(1 for m in self._messages if m.get(ROLE) == role)


def _content_chars(content) -> int:
    """估算一段 content 的字数。

    content 通常是字符串；带图片的消息是块数组 —— 只数文字块：
    base64 图片块体积巨大但计费按图片 token 算，按字符估会虚高几百倍，
    会立刻触发错误的压缩与裁剪。
    """
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                total += len(str(block.get("text", "")))
        return total
    return 0 if content is None else len(str(content))


def _media_type(file_name: str) -> str | None:
    """扩展名 → MIME 类型；不认识的返回 None。"""
    if file_name.endswith(".png"):
        return "image/png"
    if file_name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if file_name.endswith(".gif"):
        return "image/gif"
    if file_name.endswith(".webp"):
        return "image/webp"
    return None
