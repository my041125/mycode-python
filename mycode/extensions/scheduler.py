"""
会话内定时任务调度器（一期）。

三个角色：
    ScheduledTask     一个定时任务的数据（时间基准是进程内相对时钟）
    SessionScheduler  守护线程：到期检查、顺序执行、间隔任务顺延
    ScheduleTool      schedule 工具：让模型创建定时任务

执行语义（刻意为之的安全边界）：
    到点执行走「独立上下文 + 非交互权限钩子」—— 不带主对话的记忆，
    需要人工确认的命令一律拒绝。所以默认 read-only 就够提醒、检查类任务；
    要 edit/full 必须在创建时显式声明（工具会打印无人值守警告）。

为什么顺序执行而不是并发：所有任务共享同一个终端，并发打印会把
输入区打乱；串行的代价只是后一个任务晚几分钟，对提醒类任务无感。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from mycode.kernel.permission import PermissionMode
from mycode.kernel.tool import Tool

# 最短延迟：3 秒（测试与演示用；再短就没有「定时」的意义了）
_MIN_DELAY_MINUTES = 0.05

# 会话内任务上限：防止模型一口气刷出一堆任务
_MAX_TASKS = 20


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """一个会话内定时任务。"""

    task_id: str           # 会话内自增，形如 t1、t2
    kind: str              # "once"（执行一次）| "interval"（按间隔重复）
    prompt: str            # 到点执行的任务描述（作为独立提问交给 agent）
    permission: str        # 执行时的权限档：read-only / edit / full
    interval_minutes: float  # interval 的间隔分钟数；once 恒为 0
    next_fire: float       # 下次触发时刻（time.monotonic() 秒）


class SessionScheduler:
    """会话内调度器：单守护线程，按到期时间顺序执行任务。

    到点的任务交给构造时注入的 runner（shell 侧提供：构建独立上下文执行并打印），
    调度器本身只管「什么时候该跑谁」—— 执行与展示的职责分离。
    """

    def __init__(self, runner: Callable[[ScheduledTask], None]) -> None:
        self._runner = runner
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()
        self._wakeup = threading.Event()   # 新任务 / 取消 / 停止 时唤醒睡眠中的循环
        self._stop_event = threading.Event()
        self._next_id = 0
        self._thread = threading.Thread(target=self._loop, name="mycode-scheduler", daemon=True)
        self._thread.start()

    def add(self, kind: str, prompt: str, permission: str = "read-only",
            delay_minutes: float = 0.0, interval_minutes: float = 0.0) -> ScheduledTask:
        """登记一个任务并唤醒调度线程。参数不合法抛 ValueError。"""
        if kind not in ("once", "interval"):
            raise ValueError("kind 必须是 once 或 interval")
        if not prompt or not prompt.strip():
            raise ValueError("prompt 不能为空")
        with self._lock:
            if len(self._tasks) >= _MAX_TASKS:
                raise ValueError(f"会话内定时任务已达上限 {_MAX_TASKS} 个，先取消一些再试")
            self._next_id += 1
            task_id = f"t{self._next_id}"

        now = time.monotonic()
        minutes = max(_MIN_DELAY_MINUTES,
                      float(delay_minutes if kind == "once" else interval_minutes))
        task = ScheduledTask(
            task_id=task_id,
            kind=kind,
            prompt=prompt.strip(),
            permission=permission,
            interval_minutes=interval_minutes if kind == "interval" else 0.0,
            next_fire=now + minutes * 60,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._wakeup.set()
        return task

    def cancel(self, task_id: str) -> bool:
        """取消一个任务；存在返回 True。"""
        with self._lock:
            removed = self._tasks.pop(task_id.strip(), None) is not None
        if removed:
            self._wakeup.set()
        return removed

    def all(self) -> list[ScheduledTask]:
        """当前任务的快照。"""
        with self._lock:
            return list(self._tasks.values())

    def stop(self) -> int:
        """停止调度线程，返回随退出失效的任务数。"""
        self._stop_event.set()
        self._wakeup.set()
        if self._thread.is_alive():
            self._thread.join(1.0)
        return len(self._tasks)

    def _loop(self) -> None:
        """调度主循环：摘到期任务 → 顺序执行 → 算下一个触发点 → 睡到那一刻。"""
        while not self._stop_event.is_set():
            now = time.monotonic()
            due: list[ScheduledTask] = []
            with self._lock:
                for task in list(self._tasks.values()):
                    if task.next_fire <= now:
                        due.append(task)
                        if task.kind == "once":
                            self._tasks.pop(task.task_id, None)
                        else:
                            # 间隔任务从现在顺延：落后太多也不补跑，避免雪崩式连发
                            self._tasks[task.task_id] = replace(
                                task, next_fire=now + task.interval_minutes * 60)
            for task in due:
                if self._stop_event.is_set():
                    break
                try:
                    self._runner(task)
                except Exception:
                    pass  # 定时任务失败不拖垮会话；错误信息由 runner 自己打印

            with self._lock:
                fires = [t.next_fire for t in self._tasks.values()]
            # 没有任务时无限等待（被 add/cancel/stop 唤醒）；有任务时睡到最近的触发点
            wait = None if not fires else max(0.05, min(fires) - time.monotonic())
            self._wakeup.wait(wait)
            self._wakeup.clear()


class ScheduleTool(Tool):
    """schedule 工具：让模型创建定时任务。

    scope=session（默认）走会话内调度器，退出即失效；
    scope=persistent 写入持久化任务文件（跨会话），需要系统级计划任务
    （--cron-register）才会被自动拉起 —— 持久化的创建回调由 shell 注入，
    避免扩展层反向依赖入口模块。
    """

    def __init__(self, scheduler: SessionScheduler,
                 persistent_creator: Callable[[dict], str] | None = None) -> None:
        self._scheduler = scheduler
        self._persistent_creator = persistent_creator

    def name(self) -> str:
        return "schedule"

    def description(self) -> str:
        return (
            "创建定时任务。scope=session（默认）在会话内执行，退出即失效："
            "kind=once（delay_minutes 分钟后执行一次）或 kind=interval（每 interval_minutes 分钟执行一次）。"
            "scope=persistent 写入持久化任务跨会话生效，支持 kind=daily（time=HH:MM 每天定点）。"
            "任务到点会在后台以独立上下文执行（prompt 作为独立提问），结果直接打印到终端；"
            "需要人工确认的命令在定时执行中会被拒绝，所以默认权限 read-only 就够提醒、检查类任务；"
            "要改文件请显式传 permission=edit。"
        )

    def parameters(self) -> dict:
        return Tool.schema({
            "kind": Tool.str_param("once / interval / daily（daily 仅 persistent 支持）"),
            "prompt": Tool.str_param("到点执行的任务描述，会作为独立提问交给 agent"),
            "scope": Tool.str_param("session（默认，退出失效）/ persistent（跨会话）"),
            "delay_minutes": Tool.num_param("session+once 用：多少分钟后执行，可传小数"),
            "interval_minutes": Tool.num_param("interval 用：每多少分钟执行一次"),
            "time": Tool.str_param("persistent 用：daily 传 HH:MM，once 传 YYYY-MM-DD HH:MM"),
            "permission": Tool.str_param("执行权限档：read-only（默认）/ edit / full"),
        }, "kind", "prompt")

    def run(self, args: dict) -> str:
        kind = str(args.get("kind") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        # PermissionMode.parse 校验非法档位并抛 ValueError（registry 转成参数错误）
        mode = PermissionMode.parse(str(args.get("permission") or "read-only")).cli
        scope = str(args.get("scope") or "session").strip() or "session"

        note = ""
        if mode != "read-only":
            note = f"⚠ 该任务将以 {mode} 权限无人值守执行，请确认任务内容安全。\n"

        if scope == "persistent":
            if self._persistent_creator is None:
                raise ValueError("当前环境不支持持久化定时任务")
            if kind not in ("once", "interval", "daily"):
                raise ValueError("persistent 任务 kind 必须是 once / interval / daily")
            name = str(args.get("name") or "").strip() or prompt[:12]
            return note + self._persistent_creator({
                "name": name,
                "kind": kind,
                "prompt": prompt,
                "permission": mode,
                "interval_minutes": Tool.to_int(args.get("interval_minutes"), 0),
                "time_str": str(args.get("time") or "").strip(),
            })

        if kind not in ("once", "interval"):
            raise ValueError("session 任务 kind 必须是 once 或 interval")
        task = self._scheduler.add(
            kind, prompt, mode,
            delay_minutes=_minutes(args.get("delay_minutes")),
            interval_minutes=_minutes(args.get("interval_minutes")),
        )
        describe = (f"{task.interval_minutes:g} 分钟间隔重复执行" if task.kind == "interval"
                    else f"{(task.next_fire - time.monotonic()) / 60:.1f} 分钟后执行")
        return (note + f"OK: 会话内定时任务 {task.task_id} 已创建（{describe}）。"
                + "会话退出即失效；/schedules 查看，/schedules cancel "
                + task.task_id + " 取消。")


def _minutes(value) -> float:
    """把模型给的间隔参数容错转成分钟数（可能传数字或字符串小数）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
