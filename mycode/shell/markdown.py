"""
终端 Markdown 渲染器：把模型回答里的 Markdown 变成 Claude Code 风格的终端排版。

为什么需要它：模型按 Markdown 说话（标题、加粗、代码块、列表），
原样打印的话满屏都是 ** 和 ``` 记号，观感很差。
Claude Code 的做法是渲染后再上屏 —— 本模块用纯标准库复刻这件事。

支持的语法与渲染效果：
    # 标题          -> 橙色加粗（层级压平，终端里不需要六级区分）
    **加粗**        -> ANSI 加粗
    *斜体* / _斜体_  -> ANSI 斜体
    `代码`          -> 橙色行内代码
    ``` 围栏        -> 圆角框包裹的代码块（框线暗色，标题带语言名）
    - 列表          -> • 圆点列表
    1. 列表         -> 序号橙色
    > 引用          -> ▏ 暗色引用条
    --- 分隔线      -> 暗色横线

所有输出行都按终端显示宽度折行（CJK 按两列计、ANSI 转义零宽计），
保证流式重绘时的行号统计不会被终端自动折行打乱。
"""

from __future__ import annotations

import re

from mycode.shell import ui

# 块级语法
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)(\d+)([.、])\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_]\s*){3,}$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")

# 行内语法
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_UNDERSCORE_ITALIC_RE = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_CODE_RE = re.compile(r"`([^`]+)`")


def render(text: str, width: int | None = None) -> list[str]:
    """把一段 Markdown 文本渲染成终端行列表。

    width 缺省取当前终端宽度。返回的每一行都保证不超过该宽度，
    调用方（流式重绘）靠行数做原地光标回退。
    """
    if width is None or width <= 0:
        width = ui.term_width()
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    for raw in text.replace("\r\n", "\n").split("\n"):
        # 围栏开关：收集代码行，闭合时整块画框
        fence = _FENCE_RE.match(raw.strip())
        if fence is not None:
            if in_code:
                out.extend(_code_box(code_lines, code_lang, width))
                code_lines = []
                code_lang = ""
            else:
                code_lang = fence.group(1)
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(raw)
            continue

        out.extend(_render_block_line(raw, width))

    # 文本流中途被截断（正在生成）时，未闭合的代码块也要如实展示
    if in_code and code_lines:
        out.extend(_code_box(code_lines, code_lang or "代码", width))
    return out


def _render_block_line(raw: str, width: int) -> list[str]:
    """渲染一行普通（非代码块）Markdown。"""
    stripped = raw.strip()

    if not stripped:
        return [""]

    if _HR_RE.match(stripped) and not stripped.startswith("`"):
        return [ui.dim("─" * max(12, min(40, width - 8)))]

    heading = _HEADING_RE.match(stripped)
    if heading is not None:
        return [ui.accent(ui.bold(title)) for title in ui.wrap_ansi(heading.group(2).strip(), width)]

    quote = _QUOTE_RE.match(stripped)
    if quote is not None:
        body = ui.wrap_ansi(_inline(quote.group(1)), width - 2)
        return [ui.dim("▏ ") + line for line in body]

    bullet = _BULLET_RE.match(raw)
    if bullet is not None:
        indent = bullet.group(1)
        body = ui.wrap_ansi(_inline(bullet.group(2)), width - len(indent) - 2)
        first = indent + ui.accent("•") + " " + body[0]
        rest = [indent + "  " + line for line in body[1:]]
        return [first] + rest

    numbered = _NUMBERED_RE.match(raw)
    if numbered is not None:
        indent, num, dot, body_text = numbered.groups()
        body = ui.wrap_ansi(_inline(body_text), width - len(indent) - len(num) - 2)
        first = indent + ui.accent(num + dot) + " " + body[0]
        rest = [indent + " " * (len(num) + 2) + line for line in body[1:]]
        return [first] + rest

    return ui.wrap_ansi(_inline(raw), width)


def _code_box(code_lines: list[str], lang: str, width: int) -> list[str]:
    """把一个代码块渲染成圆角框：框线暗色，标题行带语言名。"""
    return ui.box_lines(code_lines, title=lang or "代码", title_style="dim")


def _inline(s: str) -> str:
    """行内语法着色：加粗 / 斜体 / 行内代码。"""
    s = _CODE_RE.sub(lambda m: ui.accent(m.group(1)), s)
    s = _BOLD_RE.sub(lambda m: ui.bold(m.group(1)), s)
    s = _ITALIC_RE.sub(lambda m: ui.italic(m.group(1)), s)
    s = _UNDERSCORE_ITALIC_RE.sub(lambda m: ui.italic(m.group(1)), s)
    return s
