"""Tk 版 Markdown 渲染：把 Markdown 文本插进对话区 Text 控件，样式用 tag 表达。

与 shell/markdown.py 是同一套语法覆盖（标题 / 粗斜体 / 行内代码 / 围栏代码块 /
列表 / 引用 / 分隔线，行内正则原样照搬），但渲染目标根本不同：
终端版输出的是带 ANSI 码的字符串列表，本模块输出的是「控件插入 + tag 区间」。
两者刻意不共享代码 —— 硬凑一层抽象只会让两边都别扭。

块级解析与终端版一样按行处理（一行一块），流式期间未闭合的代码围栏也如实渲染：
模型经常生成到一半，界面必须跟着长，不能等收尾才显示。
"""

from __future__ import annotations

import re
import tkinter as tk

# ---------------- 行内正则（与 shell/markdown.py 保持一致） ----------------

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_UNDERSCORE_ITALIC_RE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_CODE_RE = re.compile(r"`([^`]+)`")

# 行内样式表：正则 → tag 名。扫描时找「最早出现」的匹配，越靠前优先级越高，
# 同一位置命中多个时按列表顺序（code 先于 bold，避免 `**` 之类被误拆）。
_INLINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_CODE_RE, "code_inline"),
    (_BOLD_RE, "bold"),
    (_ITALIC_RE, "italic"),
    (_UNDERSCORE_ITALIC_RE, "italic"),
]

# ---------------- 块级正则（与 shell/markdown.py 保持一致） ----------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)(\d+)([.、])\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_]\s*){3,}$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")

# 分隔线的长度：与终端版一致取固定宽度，跟着对话区宽度逐行画没必要
_HR_WIDTH = 46


def insert_markdown(widget: tk.Text, text: str) -> None:
    """把一段 Markdown 文本渲染后追加到控件末尾（调用方保证插入点在 "end"）。"""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence is not None:
            i = _insert_code_block(widget, lines, i + 1, fence.group(1))
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            tag = "h1" if len(heading.group(1)) == 1 else ("h2" if len(heading.group(1)) == 2 else "h3")
            _insert_inline(widget, heading.group(2), [tag])
            widget.insert("end", "\n")
            i += 1
            continue
        if _HR_RE.match(line) is not None:
            widget.insert("end", "─" * _HR_WIDTH + "\n", ("hr",))
            i += 1
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            widget.insert("end", "  " * _indent_of(bullet.group(1)) + "• ", ("bullet",))
            _insert_inline(widget, bullet.group(2), [])
            widget.insert("end", "\n")
            i += 1
            continue
        numbered = _NUMBERED_RE.match(line)
        if numbered is not None:
            widget.insert("end", "  " * _indent_of(numbered.group(1)),
                          ())
            widget.insert("end", numbered.group(2) + numbered.group(3) + " ", ("bullet",))
            _insert_inline(widget, numbered.group(4), [])
            widget.insert("end", "\n")
            i += 1
            continue
        quote = _QUOTE_RE.match(line)
        if quote is not None:
            widget.insert("end", "▏ ", ("quote",))
            _insert_inline(widget, quote.group(1), ["quote"])
            widget.insert("end", "\n")
            i += 1
            continue
        _insert_inline(widget, line, [])
        widget.insert("end", "\n")
        i += 1


def insert_plain(widget: tk.Text, text: str, tag: str | None) -> None:
    """按纯文本插入（斜杠命令的输出、统计行）：保持换行原样，不解析 Markdown。

    tag 为 None 时按正文默认样式插入。
    """
    widget.insert("end", text + "\n", (tag,) if tag else ())


def _indent_of(space: str) -> int:
    """前导空格数 → 缩进层数（两格一层，与终端版列表缩进一致）。"""
    return len(space) // 2


def _insert_code_block(widget: tk.Text, lines: list[str], start: int, lang: str) -> int:
    """渲染围栏代码块，返回围栏结束后的行号。

    语言名做一行暗灰小字标题；正文整段用 code_block tag（底色 + 等宽）。
    流式期间可能没有结束围栏：i 走到末尾就结束，下一帧整块重渲染会补上。
    """
    if lang:
        widget.insert("end", "  " + lang + "\n", ("code_lang",))
    i = start
    while i < len(lines) and _FENCE_RE.match(lines[i]) is None:
        widget.insert("end", lines[i] + "\n", ("code_block",))
        i += 1
    return i + 1  # 跳过结束围栏那一行（流式时跳到 len(lines) 也无害）


def _insert_inline(widget: tk.Text, text: str, base_tags: tuple[str, ...] | list[str]) -> None:
    """渲染一行内的粗体 / 斜体 / 行内代码：从左往右找最早的行内标记。

    base_tags 是整行自带的 tag（比如引用行整体是暗灰），
    行内标记命中的片段会同时带上 base_tags 与样式 tag（Tk 支持多 tag 叠加）。
    """
    pos = 0
    while pos < len(text):
        chosen: tuple[re.Match[str], str] | None = None
        for pattern, tag in _INLINE_PATTERNS:
            found = pattern.search(text, pos)
            if found is None:
                continue
            # 取出现位置最早的标记；并列时用先声道的（code 优先于 bold）
            if chosen is None or found.start() < chosen[0].start():
                chosen = (found, tag)
        if chosen is None:
            widget.insert("end", text[pos:], tuple(base_tags))
            return
        found, tag = chosen
        if found.start() > pos:
            widget.insert("end", text[pos:found.start()], tuple(base_tags))
        widget.insert("end", found.group(1), (*base_tags, tag))
        pos = found.end()
