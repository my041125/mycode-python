"""
终端输出 → HTML 渲染工具：把 mycode 的真实运行输出（含 ANSI 转义序列）
转换成 Windows Terminal 风格的 HTML 页面，供截图生成 README 展示图。

用法：
    python -m mycode --demo > out.raw          # 先抓一份真实输出
    python scripts/render_terminal.py out.raw  # 生成 docs/terminal.html，再用浏览器截图

为什么自己写转换器而不是找现成工具：本项目零第三方依赖是硬约束，
而这里要处理的转义序列只有我们自己会发出的那一小撮
（SGR 颜色 / 光标上移 / 清行 / 回车 / 光标形状），
一个行缓冲模拟器就能精确还原，约 100 行代码。

模拟的终端行为（与 ui.py / markdown 渲染实际发出的序列一一对应）：
    ESC[<n>m   颜色与属性（0 复位 / 1 加粗 / 2 暗色 / 3 斜体 / 38;5;208 橙色 / 196 红色）
    ESC[<n>A   光标上移 n 行 —— 流式重绘的核心，等价于「丢弃末尾 n 行」
    ESC[2K     清除当前行（原地重绘总是整行重写，等价于「清空再写」）
    \\r        回车（列归零）
    \\n        换行
    ESC[1 q / ESC[0 q  光标形状（HTML 里无意义，直接丢弃）
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# ANSI 转义序列：CSI 以 ESC[ 开头，参数是数字/分号，结尾一个字母
_CSI_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")

# SGR 码 → CSS 类（与 ui.py 中定义的颜色一一对应）
_SGR_CLASS = {1: "bold", 2: "dim", 3: "italic", 196: "red", 208: "accent"}

# Windows Terminal 暗色主题的等价配色
_CSS = """
body { margin: 0; background: #1a1b1e; padding: 24px; }
.window { background: #0c0c0c; border-radius: 8px; overflow: hidden;
          box-shadow: 0 12px 40px rgba(0,0,0,.55); width: 900px; padding: 14px 0; }
.titlebar { background: #2b2b2b; padding: 8px 14px; display: flex; align-items: center; gap: 8px; }
.dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
.title { color: #9d9d9d; font-size: 13px; margin-left: 8px;
         font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
/* 行内边距只留横向：纵向一旦留白，每行 36px 会把整个页面撑出两倍高 */
pre { margin: 0; padding: 0 20px; color: #cccccc;
      font-family: Consolas, "Cascadia Mono", "Microsoft YaHei Mono", monospace;
      font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-break: break-all; }
.dim { color: #767676; }
.accent { color: #ff9f40; }
.bold { font-weight: 700; color: #ffffff; }
.italic { font-style: italic; }
.red { color: #ff5f5f; }
"""


class _Line:
    """一行终端内容：按「样式快照 + 文本」分段保存，渲染时逐段套 span。"""

    def __init__(self) -> None:
        self.segments: list[tuple[tuple[int, ...], str]] = []

    def write(self, sgr: tuple[int, ...], text: str) -> None:
        """追加文本；相邻同样式自动合并，避免逐字符产生的上千个小段。"""
        if not text:
            return
        if self.segments and self.segments[-1][0] == sgr:
            self.segments[-1] = (sgr, self.segments[-1][1] + text)
            return
        self.segments.append((sgr, text))

    def to_html(self) -> str:
        if not self.segments:
            return ""
        parts: list[str] = []
        for sgr, text in self.segments:
            classes = " ".join(_SGR_CLASS[c] for c in sgr if c in _SGR_CLASS)
            escaped = html.escape(text)
            parts.append(f'<span class="{classes}">{escaped}</span>' if classes else escaped)
        return "".join(parts)


def emulate(text: str) -> list[_Line]:
    """极简 VT100 行缓冲模拟器：逐字符吃进输出流，还原出最终的屏幕行。

    只模拟本程序会发出的序列；遇到的未知序列按「忽略」处理 ——
    这样转换器对未来新增的转义码也是安全的（宁缺毋错）。
    """
    lines: list[_Line] = [_Line()]
    sgr: tuple[int, ...] = ()
    i = 0
    while i < len(text):
        match = _CSI_RE.match(text, i)
        if match:
            params, cmd = match.group(1), match.group(2)
            # 注意：带 pos 参数时 match.end() 是「绝对索引」，
            # 必须赋值 i = match.end()，累加会变成「起点+终点」越跳越远
            i = match.end()
            if cmd == "m":
                codes = tuple(int(p) for p in params.split(";") if p) or (0,)
                if 0 in codes:
                    sgr = ()  # 复位：清空样式
                else:
                    sgr = sgr + codes  # 叠加样式（如 暗色+橙色同时生效）
            elif cmd == "A":
                # 光标上移 n 行 = 原地重绘回退：把末尾 n 行从缓冲里摘掉
                n = int(params) if params else 1
                del lines[len(lines) - n:]
            elif cmd == "K":
                lines[-1] = _Line()  # 清行：整行重写前先清空
            # 其它（q 光标形状等）：HTML 里无意义，忽略
            continue
        ch = text[i]
        i += 1
        if ch == "\r":
            lines[-1] = _Line()  # 回车：列归零（本程序总是整行写，等价清行）
        elif ch == "\n":
            lines.append(_Line())  # 换行：开新行
        else:
            lines[-1].write(sgr, ch)
    return lines


def render_html(text: str) -> str:
    """把还原出的屏幕行拼成完整 HTML 页面（含终端窗口装饰）。"""
    rows = "\n".join(f"<pre>{line.to_html()}</pre>" for line in emulate(text))
    return f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
  <div class="window">
    <div class="titlebar">
      <span class="dot" style="background:#ff5f57"></span>
      <span class="dot" style="background:#febc2e"></span>
      <span class="dot" style="background:#28c840"></span>
      <span class="title">mycode —— Python 复写版</span>
    </div>
    {rows}
  </div>
</body>
</html>"""


def normalize(data: bytes) -> str:
    """把抓下来的原始字节归一成模拟器友好文本。

    三个坑，全是 Windows 的「功劳」：
      1. stdout 是文本模式，程序里的 \\n 落盘成了 \\r\\n；
      2. 程序自己还会写裸 \\r（原地重绘的列归零），拼出 \\r\\r\\n；
         文本模式读回时 universal newlines 只归一 \\r\\n，残留的裸 \\r
         会让模拟器把刚渲染完的行当成「待清行」全部清掉（实测踩坑）；
      3. 光标形状码 ESC[1 q / ESC[0 q 对 HTML 无意义，留着还会被
         逐字符降级写进正文。
    所以这里统一：按字节读 → 归一换行 → 剔除光标形状码。
    """
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "")
    return re.sub(r"\x1b\[\d+ q", "", text)


def main() -> int:
    """命令行入口：argv[1] 原始输出文件；argv[2] 可选，指定 HTML 输出路径。"""
    if len(sys.argv) < 2:
        print("用法: python scripts/render_terminal.py <原始输出文件> [输出HTML路径]")
        return 1
    raw = normalize(Path(sys.argv[1]).read_bytes())
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sys.argv[1]).with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(raw), encoding="utf-8")
    print(f"已生成 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
