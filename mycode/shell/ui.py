"""
终端界面小工具（对应 Java 版 Ui）：统一负责 Claude Code 风格的外观 ——
圆角框、转圈等待、暗色提示、橙色强调，以及让这一切能对齐的
「中文按两列计宽」和「终端宽度探测」。

为什么要单独一个模块：Shell、ConsoleHook、PermissionHook 都要往终端画东西，
「一个汉字占几列」「ANSI 颜色能不能用」「窗口有多宽」这些规则必须全项目只有一份，
各画各的必然对不齐、颜色时有时无。

与 Java 版的取舍差异：JDK 拿不到控制台宽度，Java 版为此实现了一整套
「DSR 光标询问 + stdin 输入泵」；Python 标准库 shutil.get_terminal_size()
一步就能拿到操作系统给的权威答案（窗口拉伸后下一次画框自动跟上），
所以那套机器在这里不需要 —— 这是复写版的合理简化，不是功能缺失。

颜色策略：默认按终端环境自动判断（TERM / WT_SESSION / ConEmuANSI / ANSICON
任一存在就启用），也可在 agent.toml 里用 ansi = "on" / "off" 强制。
框线和符号本身是 UTF-8 字符，不依赖 ANSI。

本模块的函数集合天然满足 kernel.hooks.ConsolePresenter 协议，
可以直接作为 presenter 注入 ConsoleHook / TodoTool / PermissionHook 的确认器。
"""

from __future__ import annotations

import os
import random
import re
import shutil
import sys
import threading
import time

from mycode import config

# 兜底宽度：什么都探测不到时按最经典的 80 列画
DEFAULT_WIDTH = 80

# ANSI 转义序列（颜色码等）：计宽、折行时必须当零宽处理
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(s: str) -> str:
    """去掉字符串里的 ANSI 转义序列，用于显示宽度计算。"""
    return _ANSI_RE.sub("", s)


def ansi_enabled() -> bool:
    """当前是否输出 ANSI 转义码（供流式渲染等调用方决定渲染策略）。"""
    return _ANSI


# ---------------- 颜色 ----------------

def _ansi_enabled() -> bool:
    """按配置和环境判断要不要输出 ANSI 转义码。"""
    pref = config.get("ansi")
    if pref:
        v = pref.strip().lower()
        if v in ("on", "true"):
            return True
        if v in ("off", "false"):
            return False
    # TERM：Linux/macOS/Git Bash；WT_SESSION：Windows Terminal；
    # ConEmuANSI / ANSICON：老牌 Windows 增强终端
    return (os.environ.get("TERM") is not None
            or os.environ.get("WT_SESSION") is not None
            or os.environ.get("ConEmuANSI", "").lower() == "on"
            or os.environ.get("ANSICON") is not None)


_ANSI = _ansi_enabled()


def dim(s: str) -> str:
    """暗色：次要信息、统计行。"""
    return _wrap("\x1b[2m", s)


def accent(s: str) -> str:
    """强调色：Claude Code 的橙色。"""
    return _wrap("\x1b[38;5;208m", s)


def bold(s: str) -> str:
    """加粗。"""
    return _wrap("\x1b[1m", s)


def italic(s: str) -> str:
    """斜体（部分终端字体不支持斜体，只影响观感不影响功能）。"""
    return _wrap("\x1b[3m", s)


def error(s: str) -> str:
    """红色：错误信息。"""
    return _wrap("\x1b[196m", s)


def _wrap(code: str, s: str) -> str:
    return code + s + "\x1b[0m" if _ANSI else s


# ---------------- 宽度与对齐 ----------------

# CJK 宽字符区间（部首、汉字、朝鲜文、全角形式、扩展区）按 2 列计
_WIDE_RANGES = ((0x2E80, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF),
                (0xFF00, 0xFF60), (0x20000, 0x3FFFD))


def _cp_width(cp: int) -> int:
    """一个码点占几列。框线╭│╯、符号⏺⎿✻ 都是 1 列。"""
    return 2 if any(lo <= cp <= hi for lo, hi in _WIDE_RANGES) else 1


def width(s: str) -> int:
    """终端显示宽度：CJK 字符按 2 列，ANSI 转义零列，其余按 1 列。框线对齐全靠它。"""
    return sum(_cp_width(ord(ch)) for ch in strip_ansi(s))


def pad(s: str, total: int) -> str:
    """右侧补空格到指定显示宽度。"""
    gap = total - width(s)
    return s + " " * gap if gap > 0 else s


def fit(s: str, total: int) -> str:
    """超宽就截断加省略号，保证框不被撑破。"""
    if width(s) <= total:
        return s
    out: list[str] = []
    w = 0
    i = 0
    while i < len(s):
        if s[i] == "\x1b":  # 转义序列原样保留、零宽
            m = _ANSI_RE.match(s, i)
            seq = m.group(0) if m else s[i]
            out.append(seq)
            i += len(seq)
            continue
        ch = s[i]
        cw = _cp_width(ord(ch))
        if w + cw > total - 1:
            break
        out.append(ch)
        w += cw
        i += 1
    return "".join(out) + "…"


def wrap_ansi(s: str, total: int) -> list[str]:
    """按显示宽度把一段文本折成多行（词优先断行，CJK 长词硬断）。

    ANSI 转义序列按零宽拷贝，折行不会破坏着色；宽度不足时对
    超长的单个单词做硬断行，保证每行都不会超过终端宽度。
    """
    if total <= 0:
        return [s]
    lines: list[str] = []
    for para in s.split("\n"):
        cur: list[str] = []
        cur_w = 0
        i = 0
        while i < len(para):
            if para[i] == "\x1b":
                m = _ANSI_RE.match(para, i)
                seq = m.group(0) if m else para[i]
                cur.append(seq)
                i += len(seq)
                continue
            ch = para[i]
            cw = _cp_width(ord(ch))
            if cur_w + cw > total and cur_w > 0:
                # 在最近的空格处回退断行（词边界优先）；找不到空格就硬断
                joined = "".join(cur)
                cut = joined.rfind(" ")
                if cut > 0 and cut + 1 < len(joined):
                    lines.append(joined[:cut])
                    rest = joined[cut + 1:] + ch
                else:
                    lines.append(joined)
                    rest = ch
                cur = [rest] if rest else []
                cur_w = width("".join(cur))
                i += 1
                continue
            cur.append(ch)
            cur_w += cw
            i += 1
        lines.append("".join(cur))
    return lines


# ---------------- 终端宽度 ----------------

def _forced_width() -> int:
    """termWidth 配置（0 表示未配置）。配了就完全不听终端的，适合怪终端。"""
    return config.get_int("termWidth", 0)


def term_width() -> int:
    """当前终端宽度（列）。每次画框前调用，窗口拉伸后自动跟上。"""
    forced = _forced_width()
    if forced >= 40:
        return forced
    try:
        # shutil 会先看 COLUMNS 环境变量，再向操作系统查询真实窗口宽度
        cols = shutil.get_terminal_size(fallback=(DEFAULT_WIDTH, 24)).columns
    except Exception:
        cols = DEFAULT_WIDTH
    # 检测到真实宽度（≥40 列）就如实使用，只设上限防异常巨宽；
    # 探测不到（<40）才退回 80 —— 强行放大到 80 会让窄终端的折行与重绘全部错位
    if cols >= 40:
        return min(cols, 300)
    return DEFAULT_WIDTH


def term_height() -> int:
    """当前终端高度（行）。流式渲染用它在「超过一屏」时放弃原地重绘。"""
    try:
        return shutil.get_terminal_size(fallback=(DEFAULT_WIDTH, 24)).lines
    except Exception:
        return 24


def _inner_width() -> int:
    """内容区宽度 = 终端宽度 - 左右边框和空格的 4 列，夹在合理区间。"""
    return max(30, min(term_width() - 4, 220))


# ---------------- 圆角框 ----------------

def box_lines(lines: list[str], title: str | None = None,
              title_style: str = "accent") -> list[str]:
    """构造一个圆角信息框的行列表（不打印，供流式重绘复用）。

    title 会在框内顶部单独占一行（如代码块的语言名）；
    title_style="accent" 时首行按标题橙色加粗（启动横幅），
    "dim" 时标题行用暗色（代码块标题）。
    内容先 fit 截断再 pad 对齐，长路径不会把框撑破。
    """
    inner = _inner_width()
    out = [dim("╭" + "─" * (inner + 2) + "╮")]
    if title:
        content = pad(fit(title, inner), inner)
        styled = dim(content) if title_style == "dim" else accent(bold(content))
        out.append("│ " + styled + " │")
    for i, line in enumerate(lines):
        content = pad(fit(line, inner), inner)
        if title is None and i == 0 and title_style == "accent":
            content = accent(bold(content))
        out.append("│ " + content + " │")
    out.append(dim("╰" + "─" * (inner + 2) + "╯"))
    return out


def box(lines: list[str]) -> None:
    """画一个圆角信息框（启动横幅用）。第一行按标题处理：橙色加粗。"""
    print()  # 探测前先换行，保证光标在第 1 列
    for line in box_lines(lines):
        print(line)


# ---------------- 输入区 ----------------

def prompt_open(indicator: str | None = None) -> None:
    """画输入区：右对齐模式指示 + 全宽分隔线 + `> ` 提示符。

    对齐 Claude Code 的极简输入样式（截图样式）：没有边框，
    只有一条横贯全宽的分隔线，`> ` 后面就是输入位置。
    indicator 是右对齐的一行状态（如「● edit · /permission」），传 None 跳过。
    """
    print()
    if indicator:
        pad_left = term_width() - width(indicator)
        if pad_left > 0:
            print(" " * pad_left + indicator)
    print(dim("─" * term_width()))
    if _ANSI:
        # 把光标切成闪烁方块，贴近 Claude Code 的输入光标
        # （DECSCUSR；不支持该序列的老终端通常会静默忽略）
        sys.stdout.write("\x1b[1 q")
        sys.stdout.flush()
    print("> ", end="", flush=True)


def prompt_close(typed: str | None) -> None:
    """用户回车后恢复默认光标形状。typed 为 None（EOF）时同样只做恢复。

    极简样式下输入行本身就是最终形态（用户输入的内容留在 `> ` 后面），
    不需要再补任何框线。
    """
    if _ANSI:
        sys.stdout.write("\x1b[0 q")  # 恢复终端默认光标
        sys.stdout.flush()


# ---------------- 转圈等待 ----------------

class _Spinner:
    """动画转圈：Claude Code 风格的 ✻ 系符号 + 随机动词（琢磨中…/盘算中…）。

    单独开守护线程按 \\r 原地重画；主线程阻塞在模型调用上，互不抢输出。
    stop() 先置事件再短暂 join，保证擦线动作一定发生在动画最后一帧之后。
    """

    FRAMES = ("✻", "✽", "✳", "✶", "✷")
    VERBS = ("思考", "琢磨", "盘算", "推敲", "翻阅", "梳理", "捣鼓", "校对", "搜罗", "合计")

    LINE = 20  # 擦线宽度：盖住最长的一帧即可

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """开始转圈。非交互（管道/重定向）环境下自动变空操作。"""
        if not sys.stdout.isatty() or self._thread is not None:
            return
        self._stop_event.clear()
        verb = random.choice(self.VERBS)  # 只是挑个展示用动词，普通随机足够

        def loop() -> None:
            i = 0
            while not self._stop_event.is_set():
                print("\r  " + accent(self.FRAMES[i % len(self.FRAMES)]) + " " + verb + "中…   ",
                      end="", flush=True)
                i += 1
                if self._stop_event.wait(0.13):
                    break

        self._thread = threading.Thread(target=loop, name="mycode-spinner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止转圈并擦掉那一行。幂等，重复调用无副作用。"""
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(0.3)
        print("\r" + " " * self.LINE + "\r", end="", flush=True)
        self._thread = None


# 全局唯一的「思考中」转圈。ConsoleHook 启停，流式输出打第一个字前也要停掉它
_think = _Spinner()


def start_thinking() -> None:
    """开始转圈。非交互环境下自动变空操作。"""
    _think.start()


def stop_thinking() -> None:
    """停止转圈并擦掉那一行。幂等，重复调用无副作用。"""
    _think.stop()


# ---------------- Claude Code 式选项确认 ----------------

def pick_option(options: list[str], cancel_index: int) -> int:
    """选项选择器：↑/↓ 移动高亮、回车确认、数字 1~n 秒选、Esc 取消。

        ❯ 1. 允许一次
          2. 本会话始终允许
          3. 取消

    行缓冲终端收不到方向键也没关系：「数字 + 回车」同样能选，两种形态都覆盖。
    非交互（管道/重定向）直接返回 cancel_index —— PermissionHook 靠它走「一律拒绝」。

    返回选中的选项下标；Esc / Ctrl+D / 输入流关闭返回 cancel_index。
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return cancel_index
    if os.name == "nt":
        return _pick_windows(options, cancel_index)
    return _pick_posix(options, cancel_index)


def _render_options(options: list[str], selected: int, redraw: bool) -> None:
    """画出选项列表。redraw 时先把光标移回选项首行逐行重写。"""
    parts: list[str] = []
    if redraw and _ANSI and len(options) > 1:
        parts.append(f"\x1b[{len(options) - 1}A")  # 光标上移到选项首行
    for i, option in enumerate(options):
        if i:
            parts.append("\n")
        # 回行首并清到行尾：重画时抹掉旧内容，也顺带抹掉数字秒选时终端的回显
        parts.append("\r\x1b[K" if _ANSI else "\r")
        parts.append(accent("❯ ") if i == selected else "  ")
        parts.append(f"{i + 1}. {option}")
    print("".join(parts), end="", flush=True)


def _pick_windows(options: list[str], cancel_index: int) -> int:
    """Windows 实现读单键：msvcrt 直接给按键，不需要终端设置。"""
    import msvcrt
    selected = 0
    _render_options(options, selected, False)
    while True:
        ch = msvcrt.getwche()
        if ch in ("\x00", "\xe0"):          # 功能键前缀，下一个字符才是方向键
            ch = msvcrt.getwche()
            if ch == "H":                    # ↑
                selected = (selected - 1) % len(options)
                _render_options(options, selected, True)
            elif ch == "P":                  # ↓
                selected = (selected + 1) % len(options)
                _render_options(options, selected, True)
            continue
        if ch in ("\r", "\n"):
            print()
            return selected
        if ch == "\x1b":                     # Esc 取消
            print()
            return cancel_index
        if "1" <= ch <= str(len(options)):
            selected = int(ch) - 1
            _render_options(options, selected, True)
            print()
            return selected
        # 其它按键忽略


def _pick_posix(options: list[str], cancel_index: int) -> int:
    """POSIX 实现：进 cbreak 模式逐字节读，用 select 做半截转义序列的超时判断。"""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    selected = 0
    try:
        tty.setcbreak(fd)
        _render_options(options, selected, False)

        def read_byte(timeout: float | None = 0.12) -> int:
            """读一个字节；timeout=None 表示无限阻塞等用户按键，没等到数据返回 -1。"""
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return -1
            data = sys.stdin.read(1)
            return ord(data) if data else -1

        while True:
            b = read_byte(None)  # 首键无限等待：权限确认想考虑多久都行（对应 Java 版阻塞读）
            if b == -1:
                return cancel_index  # 输入流结束（Ctrl+D）
            if b == 0x1B:
                nxt = read_byte()
                if nxt == -1:
                    return cancel_index          # 单独的 Esc
                if nxt == ord("["):
                    cmd = read_byte()
                    if cmd in (ord("A"), ord("B")):
                        n = len(options)
                        selected = ((selected + n - 1) % n if cmd == ord("A")
                                    else (selected + 1) % n)
                        _render_options(options, selected, True)
                    continue
                continue                          # 其它序列跳过
            if b in (0x0D, 0x0A):
                print()
                return selected
            if ord("1") <= b <= ord("0") + len(options):
                selected = b - ord("1")
                _render_options(options, selected, True)
                print()
                return selected
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
