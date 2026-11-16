"""
会话持久化（对应 Java 版 SessionStore）：把整场对话存成 JSONL 文件，--continue 时读回最近一份。

没有它，退出即失忆；跨天的工作只能从头再讲一遍背景。
Claude Code 的 --continue / --resume、Gemini 的会话分支都是这个能力。

文件格式（.mycode/sessions/session-<时间戳>.jsonl）：
    第 1 行  {"type":"meta","turns":..,"promptTokens":..,...}
    其后每行  {"type":"msg","message":{role, content, ...}}

上下文本来就是 OpenAI 协议的裸 dict，序列化零转换；恢复时原样塞回。
每轮问答后全量覆盖写，进程被杀最多丢当前这一轮。
持久化失败一律静默 —— 它是体验增强，绝不是关键路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mycode.kernel.context import AgentContext
from mycode.kernel.types import Usage


@dataclass(frozen=True, slots=True)
class Restored:
    """恢复出来的会话内容。"""

    ctx: AgentContext
    turns: int
    usage: Usage


class SessionStore:
    """会话存档。构造时定名本次的存档文件，之后每次覆盖写同一个文件。"""

    def __init__(self, project_root: Path) -> None:
        root = Path(project_root).resolve()
        self._dir = root / ".mycode" / "sessions"
        self._file = self._dir / f"session-{_stamp()}.jsonl"

    def save(self, ctx: AgentContext, turns: int, usage: Usage) -> None:
        """保存当前会话快照（全量覆盖写）。任何失败都静默：存不了就算了，对话继续。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "type": "meta",
                "turns": turns,
                "promptTokens": usage.prompt_tokens,
                "completionTokens": usage.completion_tokens,
                "cacheReadTokens": usage.cache_read_tokens,
                "savedAt": _stamp(),
            }
            lines = [json.dumps(meta, ensure_ascii=False)]
            for msg in ctx.messages:
                lines.append(json.dumps({"type": "msg", "message": msg}, ensure_ascii=False))
            self._file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass  # 持久化是体验增强，不是关键路径

    def load_latest(self) -> Restored | None:
        """找到最近的存档并恢复；没有任何存档或存档损坏时返回 None（调用方开新会话）。"""
        latest = self._find_latest()
        if latest is None:
            return None
        try:
            turns = 0
            usage = Usage.ZERO
            messages: list[dict] = []
            for line in latest.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("type") == "meta":
                    turns = int(rec.get("turns") or 0)
                    usage = Usage(
                        int(rec.get("promptTokens") or 0),
                        int(rec.get("completionTokens") or 0),
                        int(rec.get("cacheReadTokens") or 0),
                    )
                elif isinstance(rec.get("message"), dict):
                    messages.append(rec["message"])
            if not messages:
                return None
            return Restored(AgentContext.restore(messages), turns, usage)
        except Exception:
            # 存档损坏就当没有，开新会话比报错卡死好
            return None

    def _find_latest(self) -> Path | None:
        """目录里时间戳最新的存档文件；没有返回 None。"""
        if not self._dir.is_dir():
            return None
        candidates = [p for p in self._dir.iterdir()
                      if p.is_file() and p.name.startswith("session-") and p.name.endswith(".jsonl")]
        return max(candidates, key=lambda p: p.name) if candidates else None


def _stamp() -> str:
    """当前时间戳，用作文件名。秒级精度，按名字排序即按时间排序。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")
