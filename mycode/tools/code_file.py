"""
一个代码文件的内存映像（对应 Java 版 CodeFile）。

只做一件事：把「文件里那些肉眼看不见、但改错了就会产生大量无意义 diff 的东西」原样保住：
    - 编码（UTF-8 / GBK，老项目里 GBK 的源码文件很常见）
    - 换行符（CRLF / LF，Windows 项目几乎都是 CRLF）
    - 是否带 BOM
    - 文件末尾有没有换行

内部统一用 \\n 处理，写回时再还原。少做一步，git diff 里就会出现整文件变更，
代码评审时根本看不出你实际改了什么。
"""

from __future__ import annotations

from pathlib import Path


class CodeFile:
    """读取时记录「隐形属性」，写回时自动还原。"""

    def __init__(self, path: Path, charset_name: str, eol: str,
                 bom: bool, trailing_newline: bool, text: str) -> None:
        self._path = path
        self._charset_name = charset_name  # 原文件编码名，写回时必须用它
        self._eol = eol                    # 换行符，"\\r\\n" 或 "\\n"
        self._bom = bom                    # 是否带 UTF-8 BOM，不能随便加也不能随便删
        self._trailing_newline = trailing_newline
        self._text = text                  # 内容，已统一成 \\n 换行

    @classmethod
    def read(cls, path: Path) -> "CodeFile":
        """读取文件，同时记录下它的编码、换行、BOM 等「隐形属性」。

        写回时会自动还原这些属性，见 write()。
        """
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"文件不存在: {path}")

        raw = path.read_bytes()
        # 判断 BOM：EF BB BF 三个字节
        bom = raw[:3] == b"\xef\xbb\xbf"
        if bom:
            raw = raw[3:]

        charset_name = _detect(raw)
        text = decode_text(raw)

        # 记录换行符，再把内容统一成 \n 方便后续文本运算
        eol = "\r\n" if "\r\n" in text else "\n"
        trailing = text.endswith("\n")
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        return cls(path, charset_name, eol, bom, trailing, text)

    @property
    def path(self) -> Path:
        """文件路径。"""
        return self._path

    @property
    def text(self) -> str:
        """文件内容（已统一成 \\n 换行）。"""
        return self._text

    @property
    def eol(self) -> str:
        """原文件的换行符，用于日志展示。"""
        return self._eol

    @property
    def charset_name(self) -> str:
        """编码名，用于在工具返回结果里提示「这个文件是 GBK」。"""
        return self._charset_name

    def write(self, new_text: str) -> None:
        """写回磁盘，自动还原编码 / 换行 / BOM / 末尾换行。

        处理顺序不能变：先统一成 \\n → 补末尾换行 → 再转成目标换行符 → 最后加 BOM。
        顺序错了就会产生 \\r\\r\\n 这种脏换行。
        """
        body = new_text.replace("\r\n", "\n")
        if self._trailing_newline and not body.endswith("\n"):
            body += "\n"
        # 原文件是 CRLF 就转回 CRLF，是 LF 就保持 LF
        out = body if self._eol == "\n" else body.replace("\n", self._eol)
        data = out.encode(self._charset_name)

        # 原文件有 BOM 就补回去，没有就不加
        if self._bom:
            data = b"\xef\xbb\xbf" + data
        self._path.write_bytes(data)  # 变量路径写盘一律走 pathlib（见 tools 层约定）


def _detect(raw: bytes) -> str:
    """探测编码名（写回时要按原编码编码）：先按严格模式试 UTF-8，非法即判 GBK。

    这个做法比引入 chardet 之类的探测库简单得多，
    对「中文 Windows 项目基本只有 UTF-8 和 GBK 两种编码」这个现实足够用。
    """
    try:
        raw.decode("utf-8", errors="strict")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


def decode_text(data: bytes) -> str:
    """字节 → 文本：先严格试 UTF-8，失败退回 GBK（errors="replace" 容错）。

    与 _detect 共用同一套判断；GBK 分支用 replace 而不是抛异常 ——
    与 Java 版 new String(bytes, GBK) 的「永不失败」行为一致，
    读到损坏或异编码的内容时显示替换符，而不是让调用方整个炸掉。
    命令输出的解码（command.py）与本文件的读取共用这一个入口。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")
