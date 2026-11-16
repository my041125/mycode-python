"""桌面版的视觉常量：配色、字体、Tk tag 名。

对照关系（终端 → GUI）：
    ANSI 208 橙（Claude Code 强调色）→ ACCENT
    ANSI 灰暗色（统计行 / 结果行）  → DIM
    ANSI 196 红（错误行）          → ERROR
    无                             → CODE_BG（代码块底色，Tk tag 没有边框，用底色区分）

整个界面走深色主题：对话区贴近终端观感，与终端版的行为对齐（它本来就是
照着 Claude Code 的深色终端画的）。
"""

from __future__ import annotations

import tkinter.font as tkfont
from dataclasses import dataclass

# ---------------- 配色 ----------------

CHAT_BG = "#1B1B1E"     # 对话区底色（近黑）
CHAT_FG = "#E4E4E7"     # 正文前景
ACCENT = "#E8863A"      # 强调橙：工具名、标题、序号、用户提示符（ANSI 208 的柔化版）
DIM = "#8B8B94"         # 暗灰：统计行、工具结果行、引用
ERROR = "#E5534B"       # 错误红
OK = "#57AB5A"          # 成功绿：权限放行、压缩完成这类「✓」行
CODE_BG = "#26262C"     # 代码块底色（Tk tag 无边框，用底色代替圆角框）
CODE_FG = "#D7DAE0"     # 代码文字
INPUT_BG = "#232327"    # 输入区底色
BAR_BG = "#141416"      # 头部与状态条底色
BAR_FG = "#B9B9C0"      # 状态条文字

# ---------------- 字体 ----------------

@dataclass(frozen=True, slots=True)
class Fonts:
    """解析出的三套字体。探测失败时逐级回退，保证任何机器都能起窗口。"""

    body: tkfont.Font     # 正文（对话区）
    mono: tkfont.Font     # 代码块与行内代码
    small: tkfont.Font    # 状态条等小字


def load_fonts() -> Fonts:
    """选字体：中文正文优先微软雅黑，等宽优先 Cascadia / Consolas。

    不写死字体名 —— Linux 上没有雅黑，探测失败就落回 Tk 默认，功能不受影响。
    """
    families = set(tkfont.families())
    body_name = next((n for n in ("Microsoft YaHei UI", "Microsoft YaHei",
                                  "PingFang SC", "Noto Sans CJK SC", "Segoe UI")
                      if n in families), "TkDefaultFont")
    mono_name = next((n for n in ("Cascadia Mono", "Consolas",
                                  "JetBrains Mono", "Menlo", "Courier New")
                      if n in families), "TkFixedFont")
    body = tkfont.Font(family=body_name, size=11)
    mono = tkfont.Font(family=mono_name, size=10)
    small = tkfont.Font(family=body_name, size=9)
    return Fonts(body, mono, small)


# ---------------- Tk tag 配置 ----------------

def apply_tags(widget, fonts: Fonts) -> None:
    """把全部文本 tag 注册到对话区 Text 控件上（render.py 按名字引用）。

    全部视觉定义集中在这里，render.py 只管「往哪段文字上贴哪个 tag」，
    与终端版 ui.py（颜色函数）+ markdown.py（贴码）的分工一致。
    """
    w = widget
    # 用户消息：橙色 ❯ 提示符 + 正文
    w.tag_config("user_mark", foreground=ACCENT, font=fonts.body)
    # 标题：橙色加粗，层级用字号区分（Tk 没有终端那种「全部压平」的必要）
    w.tag_config("h1", foreground=ACCENT, font=(fonts.body.actual("family"), 15, "bold"),
                 spacing1=6, spacing3=2)
    w.tag_config("h2", foreground=ACCENT, font=(fonts.body.actual("family"), 13, "bold"),
                 spacing1=4, spacing3=2)
    w.tag_config("h3", foreground=ACCENT, font=(fonts.body.actual("family"), 11, "bold"),
                 spacing1=2)
    # 行内样式
    w.tag_config("bold", font=(fonts.body.actual("family"), 11, "bold"))
    w.tag_config("italic", font=(fonts.body.actual("family"), 11, "italic"))
    w.tag_config("code_inline", foreground=ACCENT, font=fonts.mono,
                 background="#2E2E34")
    # 代码块：底色 + 等宽；语言名一行用暗灰
    w.tag_config("code_block", background=CODE_BG, foreground=CODE_FG,
                 font=fonts.mono, lmargin1=12, lmargin2=12)
    w.tag_config("code_lang", foreground=DIM, font=fonts.small)
    # 列表：圆点与序号用强调色，正文按行内样式走
    w.tag_config("bullet", foreground=ACCENT)
    # 引用：暗灰 + 左侧竖条字符（终端版同款 ▏）
    w.tag_config("quote", foreground=DIM)
    # 分隔线 / 系统行
    w.tag_config("hr", foreground="#3A3A40")
    w.tag_config("dim", foreground=DIM, font=fonts.small)
    w.tag_config("error", foreground=ERROR)
    w.tag_config("ok", foreground=OK)
    # 工具行：⏺ 调用行用强调色，⎿ 结果行用暗灰（对齐终端 ConsoleHook 的主次关系）
    w.tag_config("tool", foreground=ACCENT)
    w.tag_config("tool_result", foreground=DIM, font=fonts.small)
