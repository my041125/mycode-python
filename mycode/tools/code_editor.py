"""
纯函数式代码编辑器（对应 Java 版 CodeEditor）：不碰磁盘，只做文本变换，方便验证。

支持的 6 种操作，覆盖「自动补全」和「智能修改」两个场景：
    insert_before / insert_after   —— 按锚点插入（补全代码片段）
    replace                        —— 按锚点整段替换（重构）
    delete                         —— 按锚点删除
    replace_lines                  —— 按行号替换（模型拿不准原文时用）
    append                         —— 追加到文件末尾

一条铁律：锚点必须在文件里唯一。锚点不唯一说明模型给的上下文不够，
这时宁可报错让它重新读文件，也不要猜一个位置改下去 —— 猜错就是灾难。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Edit:
    """一次编辑指令。"""

    op: str                        # 操作类型，见模块 docstring 里列出的 6 种
    anchor: str | None = None      # 锚点原文，必须全文件唯一；append 时可省略
    new_code: str | None = None    # 新代码，不需要带最外层公共缩进
    start_line: int | None = None  # replace_lines 的起始行号（1 起）
    end_line: int | None = None    # replace_lines 的结束行号，None 表示与 start_line 相同
    allow_large: bool = False      # 是否允许超过 150 行的大改动


@dataclass(frozen=True, slots=True)
class Result:
    """编辑结果。"""

    new_text: str    # 修改后的完整文本
    start_line: int  # 受影响的起始行号
    end_line: int    # 受影响的结束行号
    summary: str     # 增删行数摘要，例如 "+3 / -2"


@dataclass(frozen=True, slots=True)
class Diff:
    """差异统计：新增多少行、删除多少行。"""

    added: int
    removed: int

    @property
    def changed_lines(self) -> int:
        """新增行数 + 删除行数，用来判断改动规模是否异常。"""
        return self.added + self.removed

    @classmethod
    def of(cls, a: str, b: str) -> "Diff":
        """用最长公共子序列（LCS）算「新增多少行、删除多少行」。

        为什么不用简单比对行数：改一行文字，行数不变，但实际是 +1/-1。
        LCS 能给出准确的增删行数，也顺便用来判断「这次算不算大改动」。
        """
        x, y = a.split("\n"), b.split("\n")
        # LCS 的 dp 表是 O(n×m) 的二维数组，大文件代价高。
        # 阈值定在 100 万格是刻意的：超大文件只做行数差估算，
        # 反正那种规模下「精确到行」的统计对用户也没有意义
        if len(x) * len(y) > 1_000_000:
            delta = len(y) - len(x)
            return cls(max(0, delta), max(0, -delta))
        # dp[i][j] = x[i:] 与 y[j:] 的最长公共子序列长度
        dp = [[0] * (len(y) + 1) for _ in range(len(x) + 1)]
        for i in range(len(x) - 1, -1, -1):
            row, below = dp[i], dp[i + 1]
            for j in range(len(y) - 1, -1, -1):
                row[j] = below[j + 1] + 1 if x[i] == y[j] else max(below[j], row[j + 1])
        lcs = dp[0][0]
        return cls(len(y) - lcs, len(x) - lcs)

    @staticmethod
    def preview(a: str, b: str, context: int = 2) -> list[str]:
        """生成可读的差异片段，直接给模型和用户看。"""
        x, y = a.split("\n"), b.split("\n")
        # 头尾找相同的部分，只展示中间变化区域加前后 context 行
        pre = 0
        while pre < len(x) and pre < len(y) and x[pre] == y[pre]:
            pre += 1
        suf = 0
        while (suf < len(x) - pre and suf < len(y) - pre
               and x[len(x) - 1 - suf] == y[len(y) - 1 - suf]):
            suf += 1

        out = [f"@@ 第 {max(1, pre - context + 1)} 行起 @@"]
        for i in range(max(0, pre - context), pre):
            out.append("  " + x[i])
        for i in range(pre, len(x) - suf):
            out.append("- " + x[i])
        for i in range(pre, len(y) - suf):
            out.append("+ " + y[i])
        end = min(len(x), len(x) - suf + context)
        for i in range(len(x) - suf, end):
            out.append("  " + x[i])
        return out


def apply(original: str, e: Edit) -> Result:
    """执行编辑：解析操作类型 → 定位锚点 → 对齐缩进 → 生成新文本 → 安全检查。

    锚点找不到 / 不唯一 / 改动过大时抛 ValueError，由 Registry 转成 ERROR 文本。
    """
    src = original.replace("\r\n", "\n")
    total_lines = len(src.split("\n"))

    op = (e.op or "").strip().lower()
    if op in ("insert_before", "insert_after", "replace", "delete"):
        anchor = e.anchor
        if not anchor:
            raise ValueError("op=" + op + " 必须提供 anchor（从原文逐字复制的片段）")
        if "\n" not in anchor and len(anchor.strip()) < 3:
            raise ValueError("anchor 太短容易误伤。请把上一行（或下一行）一起带上，让它能唯一定位")
        anchor = anchor.replace("\r\n", "\n")
        idx = _unique_index(src, anchor)
        from_line = _line_of(src, idx)
        to_line = _line_of(src, idx + len(anchor))

        indent = _base_indent(src, from_line, anchor)
        block = "" if e.new_code is None else reindent(e.new_code, indent)

        if op == "insert_before":
            head, tail = src[:idx], src[idx:]
            prefix = block
            # 插入块后面紧跟内容时补换行，否则会和锚点行粘成一行
            if prefix and not prefix.endswith("\n") and not tail.startswith("\n"):
                prefix += "\n"
            updated = head + prefix + tail
        elif op == "insert_after":
            end = idx + len(anchor)
            head, tail = src[:end], src[end:]
            prefix = block
            # 锚点后面不是换行（例如插在行尾），必须先补一个换行，否则会和下一行粘在一起
            if prefix and not head.endswith("\n") and not prefix.startswith("\n"):
                prefix = "\n" + prefix
            # 锚点后面紧接内容，补一个换行避免粘连
            if prefix and not prefix.endswith("\n") and tail and not tail.startswith("\n"):
                prefix += "\n"
            updated = head + prefix + tail
        elif op == "replace":
            updated = src[:idx] + block + src[idx + len(anchor):]
        else:  # delete
            updated = _clean_delete(src, idx, idx + len(anchor))
        touched_from = from_line
        touched_to = from_line if op == "delete" else _line_of(updated, idx)

    elif op == "replace_lines":
        s = e.start_line
        t = e.end_line if e.end_line is not None else e.start_line
        if s is None or s < 1 or t is None or t < s or t > total_lines:
            raise ValueError(f"行号越界：文件共 {total_lines} 行，收到 {s}~{t}")
        lines = src.split("\n")
        indent = _indent_of(src, s)
        block = "" if e.new_code is None else reindent(e.new_code, indent)
        replacement = [] if not block else block.split("\n")
        # 末尾空行属于块外，去掉避免多出空行
        if replacement and replacement[-1] == "" and block.endswith("\n"):
            replacement.pop()
        lines[s - 1:t] = replacement
        updated = "\n".join(lines)
        touched_from = s
        touched_to = s + max(0, len(replacement) - 1)

    elif op == "append":
        block = e.new_code or ""
        updated = src + block if (src.endswith("\n") or not src) else src + "\n" + block
        touched_from = total_lines
        touched_to = total_lines + len(block.split("\n"))

    else:
        raise ValueError("不支持的操作: " + op
                         + "。可用: insert_before / insert_after / replace / delete / replace_lines / append")

    diff = Diff.of(src, updated)
    if not e.allow_large and diff.changed_lines > 150:
        raise ValueError(f"本次改动 {diff.changed_lines} 行，超过安全阈值 150 行，已拒绝。"
                         "若确实要整段重构，请加 allowLarge=true")
    return Result(updated, touched_from, touched_to,
                  f"改动行数 +{diff.added} / -{diff.removed}")


# ---------------- 锚点相关 ----------------

def _unique_index(src: str, anchor: str) -> int:
    """在 src 里定位锚点，并要求全文件唯一。

    找不到时给出可执行的建议（重新读文件），而不是一句「没找到」；
    找到多处时把两个行号都报出来，模型据此补足上下文就能定位唯一位置。
    """
    first = src.find(anchor)
    if first < 0:
        raise ValueError("锚点未找到。可能原因：缩进不一致、多/少空格、"
                         "或文件已被修改。请用 read_code 重新读取原文后再试")
    second = src.find(anchor, first + 1)
    if second >= 0:
        raise ValueError(f"锚点不唯一，出现在第 {_line_of(src, first)} 行和第 {_line_of(src, second)} 行。"
                         "请多复制几行上下文让它唯一")
    return first


def _line_of(src: str, index: int) -> int:
    """根据字符下标算出它在第几行（从 1 开始），用于报错和差异预览。"""
    return src.count("\n", 0, min(index, len(src))) + 1


def _indent_of(src: str, line: int) -> str:
    """取某一行的前导空白（缩进），用于把新代码对齐到该层级。"""
    lines = src.split("\n")
    if line - 1 >= len(lines):
        return ""
    target = lines[line - 1]
    return target[:len(target) - len(target.lstrip())]


def _base_indent(src: str, line: int, anchor: str) -> str:
    """推断插入代码该用几级缩进 —— 只按锚点行自身的缩进会错。

    反例：锚点是收尾的 "}"，它自己缩进是 0，但插进去的代码应该在方法体内（4 空格）。
    所以按锚点的「首字符」和「尾字符」判断上下文层级：
        锚点以 } ) ] 开头  -> 插在收尾括号前，层级要 +1
        锚点以 { 或 ( 结尾 -> 插在开括号后，层级要 +1
        其余情况           -> 与锚点同层
    """
    indent = _indent_of(src, line)
    anchor_lines = anchor.split("\n")
    first = anchor_lines[0].strip()
    last = anchor_lines[-1].strip()

    before_closing = first.startswith(("}", ")", "]"))
    after_opening = last.endswith(("{", "("))
    return indent + _indent_unit(src) if (before_closing or after_opening) else indent


def _indent_unit(src: str) -> str:
    """探测文件用的是 Tab 还是几个空格，默认 4 空格。"""
    tab = False
    min_width = -1
    prev = 0
    for line in src.split("\n"):
        if not line.strip():
            continue
        stripped = line.lstrip()
        width = len(line) - len(stripped)
        if "\t" in line[:width]:
            tab = True
        if width > prev and (min_width < 0 or width - prev < min_width):
            min_width = width - prev
        prev = width
    if tab:
        return "\t"
    if min_width <= 0 or min_width > 8:
        return "    "
    return " " * min_width


def reindent(code: str, target_indent: str) -> str:
    """关键一步：把模型给的代码块重新对齐到目标位置的缩进。

    做法是先算块内最小公共缩进，整体剥离，再统一加上目标缩进 ——
    这样模型多给或少给缩进都不会破坏文件风格。
    """
    lines = code.replace("\r\n", "\n").split("\n")
    min_indent = -1
    for line in lines:
        if not line.strip():
            continue
        width = len(line) - len(line.lstrip())
        min_indent = width if min_indent < 0 else min(min_indent, width)
    if min_indent < 0:
        min_indent = 0

    out: list[str] = []
    for line in lines:
        if line.strip():
            out.append(target_indent + line[min_indent:])
        else:
            out.append("")
    return "\n".join(out)


def _clean_delete(src: str, start: int, end: int) -> str:
    """删除后如果留下空行，把整行一起吃掉，避免产生无意义的空行 diff。"""
    line_start = src.rfind("\n", 0, start) + 1
    line_end = src.find("\n", end)
    only_spaces_before = not src[line_start:start].strip()
    rest_is_spaces = line_end < 0 or not src[end:line_end].strip()
    if only_spaces_before and rest_is_spaces:
        cut = len(src) if line_end < 0 else line_end + 1
        return src[:line_start] + src[cut:]
    return src[:start] + src[end:]
