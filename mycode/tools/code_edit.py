"""
代码编辑工具（对应 Java 版 CodeEditTool）—— 「代码自动填充与智能修改」能力最终落地的地方。

一次成功的编辑要走完 7 步，任何一步失败都回滚：
    1. 路径必须落在项目根目录内（防 ../ 逃逸）
    2. 读文件，记录编码 / 换行 / BOM
    3. 纯文本变换（锚点唯一性、缩进对齐在 code_editor.py 里做）
    4. 有改动才落盘，落盘前先备份快照
    5. .py 文件跑 ast 语法校验，其它文件跑括号平衡检查
    6. 校验失败 → 自动还原备份，把错误详情返回给模型让它重试
    7. 编辑成功后可选执行 verifyCommand（agent.toml 配置），报错喂回模型

为什么要把「校验 + 回滚」做进工具而不是交给模型：
模型不会主动验证自己写的东西能不能编译。把校验放在工具里，
改错就会被自动撤销，模型收到 ERROR 后自然会换个写法，
这比在提示词里反复强调可靠得多。
"""

from __future__ import annotations

from pathlib import Path

from mycode import config
from mycode.kernel.tool import Tool
from mycode.tools.backup import BackupStore
from mycode.tools.code_editor import Diff, Edit, apply as apply_edit
from mycode.tools.code_file import CodeFile
from mycode.tools.command import CommandTool
from mycode.tools.syntax_check import check

# 差异预览最多显示多少行
PREVIEW_LIMIT = 26

# 验证报告最多保留多少行。报错可能几十屏，模型只需要前段的错误定位
VERIFY_REPORT_LINES = 20


class CodeEditTool(Tool):
    """edit_code：按锚点精准修改代码文件。"""

    def __init__(self, root: Path) -> None:
        # 项目根目录（绝对路径、已归一化）。所有文件操作都不允许跳出这个范围
        self._root = Path(root).resolve()
        self._backups = BackupStore(self._root)
        # 命令执行器。验证命令复用它，危险特征检查（CommandPolicy）随之自动生效
        self._runner = CommandTool(self._root)
        # 编辑成功后自动执行的验证命令（例如 python -m compileall -q mycode）；未配置表示关闭
        self._verify_command = config.get("verifyCommand")

    def result_char_limit(self) -> int:
        """结果字符额度放大到 4000：edit_code 的观察含差异预览和验证报告，
        全局默认的 1500 字会把最关键的验证报错截掉，模型反而看不到失败原因。"""
        return 4000

    def name(self) -> str:
        return "edit_code"

    def description(self) -> str:
        return (
            "修改已有代码文件。支持按锚点插入、替换、删除，或按行号替换，以及追加到文件末尾。\n"
            "使用要求（不遵守会直接报错）：\n"
            "1. 必须先 read_code 读取原文，anchor 要从原文逐字复制（含缩进），并带足够上下文保证全文件唯一；\n"
            "2. 只改需要改的部分，禁止用一次调用重写整个文件；\n"
            "3. newCode 不用手动对齐缩进，工具会按目标位置自动重排；\n"
            "4. 一次调用只做一件事，多处修改就多次调用。\n"
            "操作类型 op：\n"
            "  insert_before / insert_after —— 在锚点前/后插入新代码（补全代码片段的场景）\n"
            "  replace      —— 用 newCode 替换锚点片段（重构、改逻辑）\n"
            "  delete       —— 删除锚点片段\n"
            "  replace_lines—— 用 startLine/endLine 指定行号替换（原文对不上时的兜底）\n"
            "  append       —— 追加到文件末尾"
        )

    def parameters(self) -> dict:
        """参数定义。每个字段都写了用途，模型据此生成调用参数。"""
        return Tool.schema({
            "path": Tool.str_param("相对项目根目录的文件路径，例如 mycode/agent.py"),
            "op": Tool.str_param("操作类型: insert_before / insert_after / replace / delete / replace_lines / append"),
            "anchor": Tool.str_param("锚点片段，从原文逐字复制，必须全文件唯一。append 时可省略"),
            "newCode": Tool.str_param("要插入或替换的新代码，不含最外层公共缩进"),
            "startLine": Tool.num_param("replace_lines 的起始行号（从 1 开始）"),
            "endLine": Tool.num_param("replace_lines 的结束行号，与 startLine 相同可省略"),
            "allowLarge": Tool.str_param("改动超过 150 行时必须显式传 true"),
        }, "path", "op")

    def run(self, args: dict) -> str:
        """执行一次代码修改。

        返回给模型的文本包含五段信息：改了哪个文件、改了啥、编码换行、校验结果、差异预览。
        差异预览很重要 —— 模型能据此确认自己改对了位置，用户也能一眼看出改动范围。
        """
        # 1. 解析并校验路径
        file = self._resolve(str(args.get("path") or ""))
        op = str(args.get("op") or "").strip()

        # 2. 读文件，拿到内容加隐形属性（编码、换行、BOM）
        code_file = CodeFile.read(file)
        edit = Edit(
            op=op,
            anchor=None if args.get("anchor") is None else str(args.get("anchor")),
            new_code=None if args.get("newCode") is None else str(args.get("newCode")),
            start_line=_as_int(args.get("startLine")),
            end_line=_as_int(args.get("endLine")),
            allow_large=str(args.get("allowLarge") or "false").lower() == "true",
        )

        # 3. 纯文本变换。锚点不唯一、找不到等情况会在这里抛异常，由 Registry 兜住
        result = apply_edit(code_file.text, edit)
        if result.new_text == code_file.text:
            # 没产生差异通常是模型给的新旧代码一样，让模型自己检查，而不是当作成功
            return "未产生任何改动（新代码与原文一致），请检查 anchor 是否选对、newCode 是否为空"

        # 4. 先存快照再落盘。快照是多版本的，所以连改多次也能一层层退回去
        snapshot = self._backups.save(file)
        code_file.write(result.new_text)

        # 5. 校验。.py 走 ast 解析，其它语言走括号平衡
        verdict = check(file)
        if not verdict.ok:
            # 6. 校验失败立即回滚。用 restore 而不是手工拷贝，
            #    是为了顺带把这个「编译不过的版本」从历史里消耗掉 ——
            #    否则用户按 undo 会先退到一个坏版本上，体验很糟
            self._backups.restore(file)
            return ("ERROR: 修改后校验失败，已自动回滚到修改前状态。原因：" + verdict.message
                    + "\n请修正后重新提交本次修改。")

        # 7. 生成差异预览（带两行上下文），过长就截断，别把上下文占满
        preview = Diff.preview(code_file.text, result.new_text, 2)
        if len(preview) > PREVIEW_LIMIT:
            preview = preview[:PREVIEW_LIMIT] + ["…（差异过长，仅显示前 26 行）"]

        rel = file.relative_to(self._root).as_posix()
        observation = (
            f"已修改 {rel}\n"
            f"操作: {op} | 影响起始行: {result.start_line} | {result.summary}"
            f" | 编码: {code_file.charset_name} | 换行: {'CRLF' if code_file.eol == '\r\n' else 'LF'}\n"
            f"校验: {verdict.message}\n"
            f"快照: {snapshot.relative_to(self._root).as_posix()}\n"
            + "差异:\n" + "\n".join(preview)
        )
        return self._verify(observation)

    def _verify(self, result_text: str) -> str:
        """编辑成功后自动跑一次验证命令（agent.toml 的 verifyCommand）。

        这是把「改完要验证」从提示词约定变成机制（Aider 的核心工作流）：
        校验失败会直接拼进本次观察，模型下一轮立刻看到报错去修，
        不用等它自己想起来调 run_command。

        命令只来自用户配置（agent.toml），模型无法指定或修改；
        且经由 CommandTool 执行，CommandPolicy 的危险特征检查自动生效。
        成功时只回一行，报错时带回输出 —— 撑爆观察没有意义。
        """
        if not self._verify_command:
            return result_text
        try:
            report = self._runner.run({"command": self._verify_command, "timeoutSeconds": 120})
            if "退出码: 0 " in report:
                return result_text + f"\n[verify] {self._verify_command} 通过"
            return result_text + "\n[verify] 失败，输出如下：\n" + _head(report, VERIFY_REPORT_LINES)
        except Exception as exc:  # noqa: BLE001 —— 验证命令失败不回滚编辑，如实上报
            return result_text + "\n[verify] 验证命令执行失败: " + str(exc)

    def _resolve(self, raw: str) -> Path:
        """把模型给的相对路径解析成绝对路径，并确保没有逃出项目目录。

        这一步不能省：模型完全可能给出 ../../Windows/System32 这种路径，
        一旦放行，一个「帮你改代码」的工具就变成了任意文件写入漏洞。
        """
        cleaned = raw.strip().replace("\\", "/")
        if not cleaned:
            raise ValueError("path 不能为空")
        target = (self._root / cleaned).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"拒绝访问项目目录之外的路径: {raw}")
        return target


def _as_int(value) -> int | None:
    """把行号参数转成 int。

    模型可能传数字、传小数、传字符串，统一兼容；解析不了就返回 None 交给上层判断。
    """
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _head(text: str, n: int) -> str:
    """取前 n 行。验证报错的定位信息几乎都在开头。"""
    lines = text.split("\n")
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + "\n…（后续省略）"
