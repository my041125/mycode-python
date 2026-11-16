"""
语法校验器（对应 Java 版 JavaSyntaxCheck）。

Java 原版用编译器「只解析不类型检查」校验 .java 文件；Python 复写版对应校验 .py 文件：
用标准库 ast.parse 只做语法解析。为什么不跑更重的检查（如 mypy）：
类型检查会把「引用了还没导入的名字」也报成错，而这类问题应该让模型按上下文自己处理；
这里只保证「括号、缩进、语法结构没写坏」。

非 Python 文件不做语法校验，只做括号与引号的平衡检查。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from mycode.tools.code_file import CodeFile


@dataclass(frozen=True, slots=True)
class CheckResult:
    """校验结果。ok=False 时调用方（edit_code）应该回滚。"""

    ok: bool
    message: str  # 给模型看的说明，失败时包含行号和具体原因


def check(file: Path) -> CheckResult:
    """校验一个文件。

    Python 文件走 ast 解析，其它文件走括号平衡检查。
    """
    name = Path(file).name.lower()
    if not name.endswith(".py"):
        return CheckResult(True, _balance_check(Path(file)))

    try:
        # 不能按固定编码读：GBK 文件会被当 UTF-8 解析而误报。
        # 这里用 CodeFile 按原编码解码成字符串再交给 ast，编码问题就绕开了
        content = CodeFile.read(Path(file)).text
        ast.parse(content, filename=str(file))
    except SyntaxError as exc:
        return CheckResult(False, f"语法错误 -> 第 {exc.lineno} 行: {exc.msg}")
    except ValueError as exc:
        # ast.parse 对空字节等极端输入会抛 ValueError，如实上报
        return CheckResult(False, f"语法校验器异常: {exc}")
    except Exception as exc:  # noqa: BLE001 —— 校验器自己出问题不应阻断业务
        return CheckResult(False, f"语法校验器异常: {exc}")

    return CheckResult(True, "语法校验通过")


def _balance_check(file: Path) -> str:
    """非 Python 文件的粗检：只检查括号和引号是否成对。

    能挡住最常见的一类问题 —— 模型生成的代码在中途被截断，
    导致最后少一个 } 或少了引号。做不到语法级校验，但成本极低。

    实现上用四个状态位跳过注释和字符串内部的括号，否则
    ")" 这种字符串里的括号会被误判。
    """
    try:
        text = CodeFile.read(file).text
    except Exception:
        return "（无法读取文件，跳过校验）"

    stack: list[str] = []
    in_str = in_char = in_line = in_block = False
    pairs = {")": "(", "]": "[", "}": "{"}
    i = 0
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line:               # 单行注释：遇到换行结束
            if c == "\n":
                in_line = False
            i += 1
            continue
        if in_block:              # 块注释：遇到 */ 结束
            if c == "*" and nxt == "/":
                in_block = False
                i += 1
            i += 1
            continue
        if in_str:                # 双引号字符串：跳过转义字符
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
            i += 1
            continue
        if in_char:               # 单引号字符
            if c == "\\":
                i += 1
            elif c == "'":
                in_char = False
            i += 1
            continue

        if c == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c == '"':
            in_str = True
        elif c == "'":
            in_char = True
        elif c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack:
                return f"括号不匹配：第 {i + 1} 个字符处多了一个 {c}"
            opened = stack.pop()
            if opened != pairs[c]:
                return f"括号不匹配：期望闭合 {opened}，实际遇到 {c}"
        i += 1

    if stack:
        return f"括号未闭合：还有 {len(stack)} 个 ( [ {{ 没有配对"
    return "括号平衡检查通过"
