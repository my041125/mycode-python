"""
上下文压缩器（对应 Java 版 ContextCompressor）：
把「最老的一轮对话」换成一段摘要，而不是直接删掉。

为什么需要它：
    早期实现是 trim() —— 超过上限就从最老的开始删。问题是任务最初的意图、
    已经确认过的约束（比如「不要动注释风格」）往往就在最老那几条里，
    删掉之后 agent 会开始跑偏，而且用户完全看不出为什么。

现在的做法：整轮摘出来，交给模型压成几百字摘要，塞回 system 提示词里。
这样上下文长度可控，任务意图也还在。

三个设计取舍：
    1. 必须整轮摘（user → assistant/tool → …），否则会出现
       「assistant 说要调工具、tool 结果却被删了」的协议违规；
    2. 摘要覆盖写进 system 的固定段落，而不是再追加一条消息，避免越压越长；
    3. 摘要调用失败（离线假模型、网络问题）时退回机械摘要，保证流程不中断。
"""

from __future__ import annotations

from dataclasses import dataclass

from mycode.kernel.context import AgentContext
from mycode.kernel.llm import LlmClient
from mycode.kernel.types import ChatResult, Usage

# 一次最多喂给摘要器多少字符，太多会让摘要调用本身变贵
MAX_FEED_CHARS = 12000

# 给摘要本身预留的位置，避免「刚压完立刻又超」
RESERVED_FOR_SUMMARY = 1200

# 机械摘要的字数上限
MECHANICAL_LIMIT = 900

# 摘要器的系统提示词。要求写得很硬：只保留会影响后续决策的信息
SUMMARIZER_PROMPT = """你是一个对话摘要器。把下面这段「较早的对话」压缩成简短摘要，供后续继续同一任务使用。

只保留这些内容：
- 用户的目标与已确认的约束（尤其是否定性的要求，例如「不要改注释」）
- 用户在对话中给过的纠偏与明确偏好（例如回答语言、代码风格、工具选择）
- 已经查证过的事实与结论（例如某文件某方法的行为）
- 已做过的修改及其位置
- 尚未完成的事项

丢弃：寒暄、重复的中间过程、大段代码原文、已经作废的尝试。

要求：用中文，纯文本，不超过 300 字，不要加标题，不要写「以下是摘要」这类废话。"""


@dataclass(frozen=True, slots=True)
class Stats:
    """一次压缩的统计。"""

    removed_messages: int  # 移除了多少条消息
    removed_rounds: int    # 移除了多少轮对话
    saved_chars: int       # 净省下多少字（已扣掉摘要本身）
    usage: Usage           # 摘要调用消耗的 token，调用方要累加进成本
    used_model: bool       # 摘要是否真的由模型生成（False 表示走的兜底）


# 没有可压内容时返回的空统计
NONE = Stats(0, 0, 0, Usage.ZERO, False)


def compress(ctx: AgentContext, llm: LlmClient | None, target_chars: int, keep_recent: int) -> Stats:
    """执行压缩（就地修改 ctx）。

    llm 传 None 则直接走机械摘要；target_chars 是希望压缩后的上下文长度（字）。
    """
    before = ctx.estimate_tokens()
    # 留出摘要的位置：目标是 target_chars，但摘到 budget 就停
    budget = max(400, target_chars - RESERVED_FOR_SUMMARY)

    removed_messages = 0
    removed_rounds = 0
    dropped_parts: list[str] = []

    while ctx.estimate_tokens() > budget and ctx.size() > 1 + keep_recent:
        round_msgs = ctx.drain_oldest_round(keep_recent)
        if not round_msgs:
            break  # 只剩当前一轮，不能再摘
        removed_rounds += 1
        removed_messages += len(round_msgs)
        dropped_parts.append(_render(round_msgs))
        if sum(len(p) for p in dropped_parts) >= MAX_FEED_CHARS:
            break
    if removed_rounds == 0:
        return NONE

    dropped = "".join(dropped_parts)
    text, usage, from_model = _summarize(llm, ctx.summary, dropped)
    ctx.set_summary(text)

    return Stats(removed_messages, removed_rounds,
                 max(0, before - ctx.estimate_tokens()), usage, from_model)


def _summarize(llm: LlmClient | None, previous: str, dropped: str) -> tuple[str, Usage, bool]:
    """生成摘要，返回 (文本, 用量, 是否来自模型)。

    优先用模型，任何异常都退回机械摘要。
    压缩是「清理动作」，它自己绝不允许把主流程搞挂。
    """
    if llm is not None:
        try:
            sc = AgentContext.of(SUMMARIZER_PROMPT)
            sc.user(_build_prompt(previous, dropped))
            result: ChatResult = llm.chat(sc, [])
            if result is not None and result.text is not None and result.text.strip():
                return result.text.strip(), result.usage, True
        except Exception:
            pass  # 网络失败、鉴权失败都在这里兜住，下面走机械摘要
    return _mechanical(previous, dropped), Usage.ZERO, False


def _build_prompt(previous: str, dropped: str) -> str:
    """拼摘要请求的用户消息：旧摘要 + 新摘掉的对话。"""
    prev = previous if previous and previous.strip() else "（无）"
    return ("已有的摘要：\n" + prev + "\n\n需要并入摘要的新对话：\n" + dropped
            + "\n请输出合并后的新摘要（覆盖旧摘要，不要重复罗列）。")


def _mechanical(previous: str, dropped: str) -> str:
    """机械摘要：不经过模型，只做结构化裁剪。

    效果远不如模型摘要，但胜在永远不会失败，且完全可预期（方便测试）。
    """
    parts: list[str] = []
    if previous and previous.strip():
        parts.append(previous.strip())
    body = dropped.strip()
    if len(body) > MECHANICAL_LIMIT:
        body = body[:MECHANICAL_LIMIT] + "…"
    parts.append("（以下为自动摘要，模型摘要不可用时的兜底）\n" + body)
    out = "\n".join(parts)
    if len(out) > MECHANICAL_LIMIT + 400:
        out = out[-(MECHANICAL_LIMIT + 400):]
    return out


def _render(messages: list[dict]) -> str:
    """把消息渲染成摘要器能读的文本。

    单条内容截断到 400 字：摘要只需要知道「聊了什么」，不需要原样复现大段代码。
    带图片的消息是块数组：只取文字块 —— base64 图片块体积巨大且摘要器读不懂，
    原样塞进去等于把一次摘要请求撑爆，用「[图片]」占位即可。
    """
    lines: list[str] = []
    for m in messages:
        role = str(m.get("role"))
        content = m.get("content")
        if isinstance(content, list):
            text_bits = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_bits.append(str(part.get("text") or ""))
            text = " ".join(text_bits) if text_bits else "[图片]"
        else:
            text = "" if content is None else str(content)
        text = text.replace("\n", " ").strip()
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"- {_label(role)}: {text}")

        calls = m.get("tool_calls")
        if calls is not None:
            c = str(calls).replace("\n", " ")
            if len(c) > 200:
                c = c[:200] + "…"
            lines.append(f"  （工具调用: {c}）")
    return "\n".join(lines) + "\n"


def _label(role: str) -> str:
    """把协议里的 role 换成中文，方便摘要器理解。"""
    return {"user": "用户", "assistant": "助手", "tool": "工具结果", "system": "系统"}.get(role, role)
