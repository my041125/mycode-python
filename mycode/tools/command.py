"""
命令执行工具（对应 Java 版 CommandTool）—— 补上编码 agent 最关键的那块拼图。

没有它，agent 只能「读代码、改代码」，改完相当于闭着眼睛；
有了它才能形成真正的闭环：
    改代码 → 跑测试看报错 → 根据报错再改 → 再跑 → 通过

三道保险，缺一不可：
    1. CommandPolicy（kernel/permission.py）判定危险命令，直接拒绝
    2. PermissionHook 在 EDIT 模式下提供人工确认（本类只管执行，不管审批）
    3. 超时强杀 + 输出截断，防止一条卡死的命令把整个会话拖死
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from mycode.kernel.permission import CommandPolicy
from mycode.kernel.tool import Tool
from mycode.tools.code_file import decode_text

# 允许的最长执行时间，超过就强杀
MAX_TIMEOUT_SECONDS = 60

# 默认超时
DEFAULT_TIMEOUT_SECONDS = 30

# 输出上限，超出部分丢弃（模型看最后一段最有用，但保留开头才能看到错误起点）
MAX_OUTPUT_CHARS = 6000

# 后台任务 id 的合法形态（内部自增生成，这里做双重校验：纵深防御）
_TASK_ID_PATTERN = re.compile(r"b\d+")

# 后台任务登记表：task_output 工具（二期）查询时要用。加锁保护并行读写
_BACKGROUND: dict[str, "_BackgroundTask"] = {}
_BG_LOCK = threading.Lock()
_NEXT_BACKGROUND_ID = 0


class _BackgroundTask:
    """一个后台任务的登记信息。"""

    def __init__(self, task_id: str, process: subprocess.Popen, log_file: Path, started_at: float) -> None:
        self.id = task_id
        self.process = process
        self.log_file = log_file
        self.started_at = started_at


def background_task(task_id: str | None) -> "_BackgroundTask | None":
    """按任务 id 查后台任务，供 TaskOutputTool（二期）使用；不存在返回 None。"""
    if task_id is None:
        return None
    with _BG_LOCK:
        return _BACKGROUND.get(task_id.strip())


class CommandTool(Tool):
    """在项目目录下执行 shell 命令。"""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = Path(work_dir).resolve()

    def name(self) -> str:
        return "run_command"

    def description(self) -> str:
        return (
            "在项目目录下执行一条 shell 命令，用于编译、跑测试、查看版本等。\n"
            "使用要求：\n"
            "1. 改完代码后应该主动用它验证（例如 python -m compileall、pytest），不要假设改对了；\n"
            "2. 一次只跑一条命令，不要用 && 串一长串；\n"
            "3. 危险命令（删除、格式化、强制推送等）会被直接拒绝，不要尝试绕过；\n"
            "4. 命令默认 30 秒超时，安装依赖这类耗时操作请显式传 timeoutSeconds；\n"
            "5. 项目根目录如果有 .venv，python 会自动指向其中的解释器，\n"
            "   所以直接写 python xxx.py 就行，不用拼绝对路径；\n"
            "6. 编译、测试、起服务这类长命令传 background=true 放到后台跑，会立即返回任务 id，\n"
            "   输出写入 .mycode/runs/ 下的日志文件，之后用 run_command 执行 tail 查看进度，不要让会话干等。"
        )

    def parameters(self) -> dict:
        return Tool.schema({
            "command": Tool.str_param("要执行的命令，例如 python -m compileall -q mycode"),
            "timeoutSeconds": Tool.num_param("超时秒数，默认 30，最大 120（仅前台模式生效）"),
            "background": Tool.str_param("true 表示后台执行，立即返回任务 id；默认前台"),
        }, "command")

    def run(self, args: dict) -> str:
        command = str(args.get("command") or "").strip()

        # 第一道保险：危险命令在这里就挡住，不依赖调用方记得先问策略
        verdict = CommandPolicy.evaluate(command)
        if not verdict.allowed:
            raise ValueError(verdict.reason)

        timeout = max(1, min(MAX_TIMEOUT_SECONDS,
                             Tool.to_int(args.get("timeoutSeconds"), DEFAULT_TIMEOUT_SECONDS)))
        background = str(args.get("background") or "false").lower() == "true"

        windows = os.name == "nt"
        cmdline = ["cmd.exe", "/c", command] if windows else ["sh", "-c", command]

        env = os.environ.copy()
        # 把项目内的「自带工具」前置到 PATH：.venv 里的解释器（技能依赖装在这）、
        # tools/ 下的便携软件。整套依赖跟着项目走，不污染系统环境
        extra_path = _build_tool_path(self._work_dir, windows)
        if extra_path:
            env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', '')}"
        # 子进程 Python 默认按系统编码输出，Windows 下是 GBK，中文会乱码
        env["PYTHONIOENCODING"] = "utf-8"

        start = time.monotonic()
        if background:
            # 后台分支：命令已过策略检查、环境已就绪，只差「不等待、输出进日志」
            return self._launch_background(command, cmdline, env)

        # 用读线程收输出，避免「进程写满缓冲区阻塞」的经典死锁。
        # buffer 用 list + 锁：读线程往里写，主线程在超时路径上可能并发读
        buffer: list[bytes] = []
        lock = threading.Lock()

        proc = subprocess.Popen(
            cmdline, cwd=str(self._work_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        reader = threading.Thread(target=_drain, args=(proc, buffer, lock), daemon=True)
        reader.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            # 等读线程收尾，但设上限：进程被杀后管道可能迟迟不关，不能无限等
            reader.join(1.0)
            with lock:
                captured = _decode(b"".join(buffer))
            return ("命令超时（" + str(timeout) + " 秒）已被强制终止。\n"
                    "如果确实需要更长时间，请显式传 timeoutSeconds（最大 120）。\n"
                    "已捕获的输出：\n" + _tail(captured))
        reader.join(2.0)

        exit_code = proc.returncode
        cost_ms = int((time.monotonic() - start) * 1000)
        with lock:
            text = _decode(b"".join(buffer)).strip()

        lines = [
            "$ " + command,
            f"退出码: {exit_code} | 耗时: {cost_ms}ms",
        ]
        lines.append("(无输出)" if not text else "输出:\n" + _tail(text))
        if exit_code != 0:
            lines.append("\n提示：退出码非 0 表示执行失败，请看上面的错误信息。")
        return "\n".join(lines)

    def _launch_background(self, command: str, cmdline: list[str], env: dict) -> str:
        """后台启动：输出重定向到日志文件，登记任务表，立即返回任务 id。

        命令与环境由调用方准备好并过了策略检查，这里只做「重定向 + 登记 + 不等待」。
        任务随会话存在，进程退出时子进程一并结束。
        """
        global _NEXT_BACKGROUND_ID
        with _BG_LOCK:
            _NEXT_BACKGROUND_ID += 1
            task_id = "b" + str(_NEXT_BACKGROUND_ID)
        if not _TASK_ID_PATTERN.fullmatch(task_id):
            raise RuntimeError("后台任务 id 非法: " + task_id)  # 纵深防御：id 只能是内部生成的形态

        # 日志目录固定在项目内 .mycode/runs/；文件名只含内部生成的 b<数字>
        log_dir = self._work_dir / ".mycode" / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}.log"

        # 后台输出走「PIPE + 读线程」写进日志文件 —— 与前台路径同一套收流模式，
        # 不在父进程里长久持有日志文件句柄
        proc = subprocess.Popen(cmdline, cwd=str(self._work_dir), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

        def _to_log(pipe, path: Path) -> None:
            """读线程：把子进程输出边读边写进日志文件。"""
            try:
                with path.open("wb") as log:
                    while True:
                        chunk = pipe.read(4096)
                        if not chunk:
                            break
                        log.write(chunk)
            except Exception:
                pass  # 进程退出 / 管道关闭时自然结束

        threading.Thread(target=_to_log, args=(proc.stdout, log_file), daemon=True).start()

        with _BG_LOCK:
            _BACKGROUND[task_id] = _BackgroundTask(task_id, proc, log_file, time.monotonic())

        # 展示用的日志路径从真实 Path 对象派生，而不是手写一遍相对路径 ——
        # 少一处重复，也就少一处「两处写法不一致」的隐患
        log_rel = log_file.relative_to(self._work_dir).as_posix()
        return ("已在后台启动: " + command + "\n"
                + f"任务 id: {task_id} | 日志: {log_rel}\n"
                + f"查看输出：run_command 执行 tail {log_rel}（只读命令，不会要求确认）。")


def _drain(proc: subprocess.Popen, buffer: list[bytes], lock: threading.Lock) -> None:
    """读完整个输出流。命令输出可能很长，先全收再统一截断，避免边读边丢导致上下文断片。"""
    try:
        data = proc.stdout.read()
        with lock:
            buffer.append(data)
    except Exception:
        pass  # 流被关闭（进程被强杀）时忽略


def _decode(data: bytes) -> str:
    """解码输出：委托 code_file.decode_text —— 全项目共用同一套「UTF-8 优先、GBK 兜底」规则。"""
    return decode_text(data)


def _tail(text: str) -> str:
    """截断输出。保留开头（错误起点）和结尾（错误汇总）各一半，中间省略。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    head, tail = text[:half], text[-half:]
    return head + f"\n…（中间省略 {len(text) - MAX_OUTPUT_CHARS} 字符）…\n" + tail


def _build_tool_path(work_dir: Path, windows: bool) -> str:
    """组装要前置到 PATH 的目录，用系统分隔符连接。

    顺序有讲究：虚拟环境排最前，其次是便携软件 —— 同名命令以靠前的为准。
    没有可加的目录时返回空串。
    """
    dirs: list[Path] = []

    # 1. 项目虚拟环境
    venv_bin = work_dir / (".venv/Scripts" if windows else ".venv/bin")
    if venv_bin.is_dir():
        dirs.append(venv_bin)

    # 2. 便携软件。LibreOffice 这类安装包解压后是 tools/LibreOffice/program/soffice.exe，
    #    所以优先找 program 子目录；没有就直接用软件根目录
    tools_dir = work_dir / "tools"
    if tools_dir.is_dir():
        try:
            for sub in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
                program = sub / "program"
                dirs.append(program if program.is_dir() else sub)
        except OSError:
            pass  # 目录读不了就跳过，不能因为工具目录有问题就把命令执行整死

    return os.pathsep.join(str(d) for d in dirs)
