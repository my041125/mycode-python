"""
持久化定时任务（二期）：任务写进 .mycode/schedules.json，跨会话存活。

触发形态：
    1. python -m mycode --cron   无头执行器：执行所有到期任务后退出；
       由 Windows 任务计划（--cron-register 注册，每分钟拉起）或 crontab 驱动；
    2. 交互会话内的 SessionScheduler 只管 scope=session 的任务，
       与持久化任务互不干扰。

报告落盘约定：.mycode/schedules/<任务id>/<时间戳>.md；
交互启动时若发现上次之后有新报告，提示 /reports 查看。绝不自动外发。

入口模块说明：本文件是 --cron 的实现，属于最外层入口，
允许 import shell；反向限制只约束 kernel / tools / extensions 各层。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from mycode.kernel.permission import PermissionHook, PermissionMode
from mycode.shell.shell import Shell

# next_run 用「分」精度（调度粒度就是分钟）；last_run 带秒
FMT = "%Y-%m-%d %H:%M"
FMT_S = "%Y-%m-%d %H:%M:%S"

# 任务名即报告目录名，只允许中英文、数字、下划线、短横线 —— 从源头堵死路径穿越
_TASK_ID_RE = re.compile(r"[\w-]{1,40}")

# Windows 计划任务名与运行脚本
_CRON_TASK_NAME = "mycode-cron"

_VALID_KINDS = ("once", "interval", "daily")


@dataclass
class CronTask:
    """一条持久化定时任务。字段即 JSON 结构（asdict 直接落盘）。"""

    id: str               # 任务名，同时是报告目录名（只允许中英文数字_-）
    name: str
    kind: str             # once / interval / daily
    prompt: str
    permission: str       # read-only / edit / full
    enabled: bool
    interval_minutes: float
    time: str             # daily: HH:MM；once: YYYY-MM-DD HH:MM
    next_run: str         # FMT 格式；空串表示未排程
    last_run: str         # FMT_S 格式
    last_status: str      # ok / ERROR 摘要
    run_count: int


def load_tasks(root: Path) -> list[CronTask]:
    """读任务清单；文件不存在或损坏返回空表（调度是可选能力，不阻断启动）。"""
    path = _store_path(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [CronTask(**item) for item in raw]
    except Exception:
        return []  # 清单损坏就当没有：绝不因为一个 JSON 阻断启动


def save_tasks(root: Path, tasks: list[CronTask]) -> None:
    """全量保存任务清单。"""
    path = _store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(t) for t in tasks], ensure_ascii=False, indent=2),
                    encoding="utf-8")


def add_task(root: Path, name: str, kind: str, prompt: str, permission: str = "read-only",
             interval_minutes: float = 0.0, time_str: str = "") -> CronTask:
    """创建一条持久化任务；参数不合法抛 ValueError。"""
    if kind not in _VALID_KINDS:
        raise ValueError("kind 必须是 once / interval / daily")
    if not prompt or not prompt.strip():
        raise ValueError("prompt 不能为空")
    mode = PermissionMode.parse(permission).cli
    interval = float(interval_minutes)
    if kind == "interval" and interval <= 0:
        raise ValueError("interval 任务必须传 interval_minutes（分钟数）")
    if kind == "daily":
        if not re.fullmatch(r"\d{1,2}:\d{2}", time_str.strip()):
            raise ValueError("daily 任务的 time 必须是 HH:MM，例如 09:30")
        hh, mm = time_str.strip().split(":")
        time_value = f"{int(hh):02d}:{int(mm):02d}"
    elif kind == "once":
        try:
            datetime.strptime(time_str.strip(), FMT)
        except ValueError as exc:
            raise ValueError("once 任务的 time 必须是 YYYY-MM-DD HH:MM，例如 2026-11-20 09:30") from None
        time_value = time_str.strip()
    else:
        time_value = ""

    task_id = _safe_id(name)
    tasks = load_tasks(root)
    if any(t.id == task_id for t in tasks):
        raise ValueError(f"任务名「{task_id}」已存在，换一个名字")
    task = CronTask(
        id=task_id, name=name.strip() or task_id, kind=kind, prompt=prompt.strip(),
        permission=mode, enabled=True, interval_minutes=interval,
        time=time_value,
        next_run=compute_next_run(kind, interval, time_value, datetime.now()),
        last_run="", last_status="", run_count=0,
    )
    tasks.append(task)
    save_tasks(root, tasks)
    return task


def delete_task(root: Path, task_id: str) -> bool:
    """按名字删除任务；存在返回 True。"""
    tasks = load_tasks(root)
    kept = [t for t in tasks if t.id != task_id.strip()]
    if len(kept) == len(tasks):
        return False
    save_tasks(root, kept)
    return True


def set_enabled(root: Path, task_id: str, enabled: bool) -> bool:
    """暂停 / 恢复任务。恢复时若 next_run 已经过期，从现在重新排程。"""
    tasks = load_tasks(root)
    for t in tasks:
        if t.id == task_id.strip():
            t.enabled = enabled
            if enabled and (not t.next_run or _in_past(t.next_run)):
                t.next_run = compute_next_run(t.kind, t.interval_minutes, t.time,
                                              datetime.now())
            save_tasks(root, tasks)
            return True
    return False


def run_due(args: list[str]) -> int:
    """--cron 入口：执行所有到期任务，报告落盘，更新清单。

    每个任务独立 try：一个失败写错误报告，不影响其它任务。
    """
    root = Path.cwd()
    tasks = load_tasks(root)
    now = datetime.now()
    due = [t for t in tasks if _is_due(t, now)]
    if not due:
        print("[cron] 没有到期的定时任务。")
        return 0

    shell = Shell(args)
    shell._build_runtime()  # noqa: SLF001 —— 入口模块复用装配（会打印模型摘要行）
    for task in due:
        started = datetime.now()
        # 非交互权限钩子：需要人工确认的命令一律拒绝 —— 无人值守的安全边界
        permission = PermissionHook.for_mode(PermissionMode.parse(task.permission), None)
        agent = shell.build_headless_agent(permission)
        try:
            answer = agent.run(task.prompt)
            status = "ok" if not answer.startswith("ERROR:") else answer[:160]
        except Exception as exc:  # noqa: BLE001 —— 单任务失败不影响其它任务
            answer = "ERROR: " + str(exc)
            status = ("ERROR: " + str(exc))[:160]
        report = _write_report(root, task, started, answer)

        task.last_run = started.strftime(FMT_S)
        task.last_status = status
        task.run_count += 1
        if task.kind == "once":
            task.enabled = False  # 一次性任务执行完即失效
        else:
            task.next_run = compute_next_run(task.kind, task.interval_minutes,
                                             task.time, datetime.now())
        # 每个任务跑完即保存：执行器中途被杀也不丢已完成任务的进度
        save_tasks(root, tasks)
        print(f"[cron] {task.id} 完成，报告：{_report_rel(root, report)}")
    return 0


def pending_reports(root: Path) -> list[Path]:
    """列出上次交互之后新产生的报告（按 .seen 时间戳判断），供启动提示。"""
    reports_root = _schedules_root(root)
    if not reports_root.is_dir():
        return []
    marker = reports_root / ".seen"
    seen = 0.0
    if marker.is_file():
        try:
            seen = float(marker.read_text(encoding="utf-8").strip())
        except Exception:
            seen = 0.0
    reports = [p for p in reports_root.glob("*/*.md") if p.is_file()]
    return [p for p in reports if p.stat().st_mtime > seen]


def mark_reports_seen(root: Path) -> None:
    """把当前时刻记为「报告已看过」，之后的启动提示只算新增部分。"""
    marker = _schedules_root(root) / ".seen"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()), encoding="utf-8")


def register(args: list[str]) -> int:
    """--cron-register：把无头执行器注册成系统级每分钟计划任务（用户手动执行）。"""
    root = Path.cwd()
    if os.name != "nt":
        print("非 Windows 系统：把下面一行加入 crontab（crontab -e）即可：")
        print(f"  * * * * * cd {root} && {sys.executable} -m mycode --cron >/dev/null 2>&1")
        return 0
    bat = root / ".mycode" / "cron-runner.bat"
    bat.parent.mkdir(parents=True, exist_ok=True)
    # 批处理按系统 ANSI 编码（中文系统即 GBK）解析，中文路径必须用 mbcs 写
    bat.write_text(f"@echo off\r\ncd /d \"{root}\"\r\n\"{sys.executable}\" -m mycode --cron\r\n",
                   encoding="mbcs")
    result = subprocess.run(
        ["schtasks", "/create", "/tn", _CRON_TASK_NAME, "/sc", "minute", "/mo", "1",
         "/tr", str(bat), "/f"],
        capture_output=True, text=True)
    if result.returncode == 0:
        print(f"已注册 Windows 计划任务 {_CRON_TASK_NAME}（每分钟拉起一次无头执行器）。")
        print("移除请执行：python -m mycode --cron-unregister")
        return 0
    print("注册失败：" + (result.stderr or result.stdout).strip())
    return 1


def unregister(args: list[str]) -> int:
    """--cron-unregister：移除系统级计划任务。"""
    if os.name != "nt":
        print("非 Windows 系统：请自行从 crontab 移除对应行。")
        return 0
    result = subprocess.run(["schtasks", "/delete", "/tn", _CRON_TASK_NAME, "/f"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        print(f"已移除计划任务 {_CRON_TASK_NAME}。")
        return 0
    print("移除失败：" + (result.stderr or result.stdout).strip())
    return 1


# ---------------- 内部工具 ----------------

def _store_path(root: Path) -> Path:
    return root / ".mycode" / "schedules.json"


def _schedules_root(root: Path) -> Path:
    return root / ".mycode" / "schedules"


def _safe_id(name: str) -> str:
    """校验任务名：它同时是报告目录名，只允许中英文、数字、下划线、短横线。"""
    task_id = (name or "").strip()
    if not task_id:
        raise ValueError("任务名不能为空")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("任务名只能包含中英文、数字、下划线、短横线（1~40 字），不能有空格和斜杠")
    return task_id


def _is_due(task: CronTask, now: datetime) -> bool:
    """判断任务是否到期；next_run 损坏按未到期处理（宁可不跑也不跑错）。"""
    if not task.enabled or not task.next_run:
        return False
    try:
        return datetime.strptime(task.next_run, FMT) <= now
    except ValueError:
        return False


def _in_past(next_run: str) -> bool:
    try:
        return datetime.strptime(next_run, FMT) <= datetime.now()
    except ValueError:
        return True


def compute_next_run(kind: str, interval_minutes: float, time_value: str,
                     now: datetime) -> str:
    """按任务类型计算下次运行时刻（FMT 格式）。"""
    if kind == "interval":
        return (now + timedelta(minutes=float(interval_minutes))).strftime(FMT)
    if kind == "daily" and time_value:
        hh, mm = time_value.split(":")
        candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.strftime(FMT)
    return time_value  # once：用户指定的绝对时刻


def _write_report(root: Path, task: CronTask, started: datetime, answer: str) -> Path:
    """把一次运行的结果写成 Markdown 报告。"""
    report_dir = _schedules_root(root) / task.id
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / f"{started.strftime('%Y%m%d-%H%M%S')}.md"
    header = (f"# 定时任务报告：{task.name}\n\n"
              f"- 运行时间：{started.strftime(FMT_S)}\n"
              f"- 权限档：{task.permission}\n"
              f"- 任务描述：{task.prompt}\n\n---\n\n")
    report.write_text(header + answer + "\n", encoding="utf-8")
    return report


def _report_rel(root: Path, report: Path) -> str:
    try:
        return report.relative_to(root).as_posix()
    except ValueError:
        return str(report)
