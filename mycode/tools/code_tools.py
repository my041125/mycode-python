"""
只读类代码工具（对应 Java 版 CodeTools）：read_code、list_files。

为什么必须给模型配 read_code：模型看不到磁盘，只能靠工具把原文喂进上下文。
没有它，agent 就会凭「记忆」编造锚点，然后一次都改不对。
这也是代码类 agent 和聊天类 agent 最本质的区别。
"""

from __future__ import annotations

import os
from pathlib import Path

from mycode.kernel.tool import SimpleTool, Tool
from mycode.tools.code_file import CodeFile

# 单次最多返回的行数，防止一个 5000 行的文件直接把上下文占满
MAX_READ_LINES = 400

# 目录遍历最多返回的文件数
MAX_LIST_FILES = 200

# 目录遍历的最大深度，防止在 node_modules 这类深目录里翻太久
MAX_DEPTH = 12

# 遍历时跳过的目录名与后缀：编译产物、版本库、编辑器配置、虚拟环境
_SKIP_DIRS = {"target", ".git", ".idea", ".venv", "__pycache__", "node_modules", ".mycode", ".mimosa", ".zcode"}
_SKIP_SUFFIX = (".pyc", ".bak", ".class")


def read_tool(root: Path) -> Tool:
    """构造 read_code 工具。

    root 是项目根目录，所有路径都必须落在它内部（防 ../ 逃逸）。
    """
    base = Path(root).resolve()

    def _read_code(args: dict) -> str:
        # 1. 路径归一化并校验：
        #    项目目录内随便读；技能目录（项目外）也放行 ——
        #    否则模型想读 SKILL.md 会被拒，只能退回 cat/sed，白白浪费好几步
        raw_path = str(args.get("path") or "").replace("\\", "/")
        target = base.joinpath(raw_path).resolve()
        if not (target == base or target.is_relative_to(base)) and not _is_skill_path(target):
            raise ValueError(f"拒绝访问项目目录之外的路径: {raw_path}")
        if not target.is_file():
            raise ValueError(f"文件不存在: {raw_path}")

        # 2. 计算要读的行范围，越界要明确报错而不是静默返回空。
        #    不能用固定 UTF-8 直接读：遇到 GBK 编码的老项目文件会直接炸，
        #    交给 CodeFile 做编码识别，和写文件时保持同一套判断
        lines = CodeFile.read(target).text.split("\n")
        start = max(1, Tool.to_int(args.get("startLine"), 1))
        end = (start + MAX_READ_LINES - 1 if args.get("endLine") is None
               else min(len(lines), Tool.to_int(args.get("endLine"), 1)))
        if start > len(lines):
            raise ValueError(f"起始行超出文件长度，文件共 {len(lines)} 行")
        end = min(end, len(lines))

        # 3. 带行号输出。行号很重要：模型按行号定位比按文本定位更稳
        out = [f"{target.name} 共 {len(lines)} 行，以下为第 {start}~{end} 行："]
        for i in range(start, end + 1):
            out.append(f"{i:4d}| {lines[i - 1]}")
        # 4. 没读完要告诉模型怎么继续，否则它会以为文件就这么长
        if end < len(lines):
            out.append(f"…（未读完，剩余 {len(lines) - end} 行，需要时用 startLine={end + 1} 继续）")
        return "\n".join(out)

    return SimpleTool(
        "read_code",
        "读取代码文件的指定行范围，返回带行号的原文。修改代码之前必须先调用它拿到准确原文。"
        "默认返回前 400 行，文件较长时用 startLine/endLine 分段读。",
        Tool.schema({
            "path": Tool.str_param("相对项目根目录的文件路径"),
            "startLine": Tool.num_param("起始行号，从 1 开始，默认 1"),
            "endLine": Tool.num_param("结束行号，默认 startLine+399"),
        }, "path"),
        _read_code,
    )


def _is_skill_path(target: Path) -> bool:
    """判断路径是否落在技能目录内。

    技能装在 ~/.claude/skills（项目外，二期接入），但读它是安全的：
    里面只有 SKILL.md 和配套脚本。放行可以避免模型为了看一眼技能正文
    而绕道 run_command 用 cat/sed —— 那既费步数，也绕过了读文件的保护逻辑。
    """
    roots = [Path.home() / ".claude" / "skills"]
    install = os.environ.get("MYCODE_HOME")
    if install:
        roots.append(Path(install) / "skills")
    return any(target.is_relative_to(r.resolve()) for r in roots)


def iter_project_files(root: Path, start: Path | None = None,
                       max_depth: int = MAX_DEPTH, limit: int = MAX_LIST_FILES) -> list[str]:
    """遍历项目文件，返回已排序的相对路径列表。

    list_files 工具与 shell 的 /files 命令共用这一套遍历与过滤规则，
    避免两处各写一份、日后改一处漏一处。

    root 决定相对路径的基准（工具返回的路径要能直接喂给 read_code）；
    start 决定从哪个目录开始遍历（默认 root 本身）。
    """
    base = Path(root).resolve()
    begin = base if start is None else Path(start).resolve()

    out: list[str] = []
    stack = [(begin, 0)]
    while stack and len(out) < limit:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name not in _SKIP_DIRS:
                    stack.append((child, depth + 1))
            elif child.is_file():
                if child.name.endswith(_SKIP_SUFFIX):
                    continue
                out.append(child.relative_to(base).as_posix())
                if len(out) >= limit:
                    break
    out.sort()
    return out


def list_tool(root: Path) -> Tool:
    """构造 list_files 工具，用于定位要改哪个文件。"""
    base = Path(root).resolve()

    def _list_files(args: dict) -> str:
        dir_arg = str(args.get("dir") or ".")
        d = base.joinpath(dir_arg).resolve()
        if not (d == base or d.is_relative_to(base)):
            raise ValueError("拒绝访问项目目录之外的路径")
        if not d.is_dir():
            raise ValueError(f"目录不存在: {dir_arg}")

        keyword = args.get("keyword")
        out = iter_project_files(base, start=d, max_depth=MAX_DEPTH, limit=MAX_LIST_FILES)
        if keyword:
            # 遍历有总量上限，关键词过滤放在遍历之后：宁可少列也不越过上限
            out = [f for f in out if keyword in Path(f).name]

        if not out:
            return f"目录 {dir_arg} 下没有匹配的代码文件"
        return f"共 {len(out)} 个文件：\n" + "\n".join(out)

    return SimpleTool(
        "list_files",
        "列出项目目录下的代码文件，用于定位要修改的文件。会跳过 target、.git、.idea 等目录。",
        Tool.schema({
            "dir": Tool.str_param("相对项目根目录的子目录，默认 ."),
            "keyword": Tool.str_param("可选，文件名包含的关键字过滤，例如 Invoice"),
        }, "dir"),
        _list_files,
    )
