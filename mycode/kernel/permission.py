"""
权限：三档模式 + 命令安全策略 + 审批钩子。

（对应 Java 版 PermissionMode / CommandPolicy / PermissionHook 三个文件。
CommandPolicy 并入本文件是有意为之：它是纯决策逻辑、被钩子直接调用，
放 tools 层会造成 kernel → tools 的反向依赖，违背分层铁律。）

这是加装 run_command 之后必须同步引入的东西 ——
一个能执行任意命令、又能改文件的 agent，如果没有任何权限边界，
就等于把终端交给了一个可能产生幻觉的程序。

三档模式：
    read-only  只能读，不能改文件、不能执行命令（读代码、问问题）
    edit       可以改文件；执行命令要逐条确认（默认档，日常够用）
    full       全部放行（自己玩的临时目录、或者完全信任的时候）
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from mycode.kernel.hooks import Hook


class PermissionMode(Enum):
    """权限模式：决定 agent 到底能做什么。"""

    READ_ONLY = ("read-only", "只读，不能改文件也不能执行命令")
    EDIT = ("edit", "可改文件；执行命令需要确认")
    FULL = ("full", "全部放行，不再询问")

    def __init__(self, cli: str, label: str) -> None:
        # cli 是命令行里用的名字（--permission=edit），label 是给用户看的说明
        self.cli = cli
        self.label = label

    @classmethod
    def parse(cls, value: str | None) -> "PermissionMode":
        """解析命令行参数里的权限模式，缺省用 EDIT。忽略大小写。"""
        if not value or not value.strip():
            return cls.EDIT
        v = value.strip().lower()
        for mode in cls:
            if mode.cli == v:
                return mode
        raise ValueError(f"不支持的权限模式: {value}。可选: read-only / edit / full")


@dataclass(frozen=True, slots=True)
class Decision:
    """命令判定结果。reason 会同时展示给用户和模型。"""

    allowed: bool        # 是否允许执行
    needs_approval: bool  # 是否需要人工确认
    reason: str          # 拒绝或需要确认的原因


class CommandPolicy:
    """命令安全策略：决定一条命令能不能跑、要不要人工确认。

    这不是「防君子」的装饰，而是 run_command 这个工具的安全阀 ——
    它让模型可以直接操作终端，同时也让一条幻觉出来的 `rm -rf /`
    有机会被拦住。

    三档判定：
        1. 命中危险片段        -> 直接拒绝，永不放行
        2. 只读命令（白名单）   -> 放行，不用问
        3. 其它命令            -> 放行但要人工确认

    为什么第三档不直接拒绝：命令不可能穷举（构建工具、脚本、自定义 CLI 太多了），
    一刀切会让工具变得没用。折中办法是「默认都要问一遍」。
    """

    # 危险命令特征。命中任意一条直接拒绝，不给确认的机会 ——
    # 因为用户很可能疲劳点击「确认」，而这种操作没有撤销余地。
    DANGEROUS = (
        "rm -rf /", "rm -rf ~", "rm -rf .", "rm -fr /",
        "del /f /s /q", "del /s /q", "format ", "rd /s /q", "rmdir /s /q",
        "shutdown", "reboot", "taskkill /f /im",
        "diskpart", "mkfs", "dd if=", "> /dev/sd", "chmod -r 777 /",
        "reg delete", "bcdedit",
        ":(){", "curl | bash", "curl|bash", "wget | sh",
        "git push --force", "git reset --hard", "git clean -fd",
        "git push -f",
    )

    # 只读命令白名单：跑起来不会有副作用，不用打断用户。匹配「命令的开头」。
    READ_ONLY = (
        "ls", "dir", "pwd", "cd", "cat", "type", "head", "tail", "wc",
        "find", "grep", "findstr", "where", "which", "echo", "date", "whoami",
        "mvn -v", "mvn -version", "mvn --version", "java -version", "javac -version",
        "node -v", "npm -v", "python --version", "git status", "git diff",
        "git log", "git branch", "git show", "git remote",
    )

    # 命中这些片段说明可能在读密钥文件，禁止让内容进入模型上下文
    SENSITIVE = ("agent.properties", ".env", "id_rsa", "credentials", ".npmrc")

    @classmethod
    def evaluate(cls, command: str | None) -> Decision:
        """评估一条命令，返回判定结果。"""
        if not command or not command.strip():
            return Decision(False, False, "命令为空")
        cmd = command.strip()
        lower = cmd.lower()

        # 1. 危险命令，永不执行
        for bad in cls.DANGEROUS:
            if bad in lower:
                return Decision(
                    False, False,
                    f"命中危险命令特征「{bad}」，已拒绝。这类操作不可撤销，如需执行请你自己在终端里做",
                )

        # 2. 读取密钥文件，拒绝：内容会进入模型上下文，等同于泄露
        read_prefixes = ("cat", "type", "more", "head", "tail")
        parts = lower.split()
        first = parts[0] if parts else ""
        for secret in cls.SENSITIVE:
            if secret in lower and first in read_prefixes:
                return Decision(
                    False, False,
                    f"检测到尝试读取敏感配置文件（{secret}），已拒绝，避免密钥进入对话上下文",
                )

        # 3. 只读命令直接放行（整词匹配开头，避免 "lsass" 被当成 "ls"）
        for safe in cls.READ_ONLY:
            if lower == safe or lower.startswith(safe + " "):
                return Decision(True, False, "只读命令，直接执行")

        # 4. 其余一律需要人工确认
        return Decision(True, True, "该命令可能产生副作用，需要你确认后才会执行")


class PermissionHook(Hook):
    """权限钩子：在每次工具执行前做一次「能不能做」的判断。

    放在 Hook 里而不是写在 Agent 循环里，是因为权限属于「策略」，
    而循环属于「机制」—— 同一套循环，换个钩子就能变成只读助手或全放行模式。

    判断顺序：
        0. 会话内已放行  -> 用户此前对该命令选过「始终允许」，直接放行
        1. 只读模式      -> 编辑类、非只读执行类工具一律拒绝
        2. run_command   -> 先过 CommandPolicy：危险命令拒绝，只读命令放行，其余问用户
        3. 其它工具      -> 放行
    """

    # 人工确认的三态结果。ALWAYS_ALLOW 是「本会话内同一命令不再询问」——
    # 比逐条确认省事，又比全放行保守；退出进程即失效，不做持久化，安全边界保持收紧。
    ALLOW = "allow"
    DENY = "deny"
    ALWAYS_ALLOW = "always"

    # 常驻关注的两类工具
    TOOL_EDIT = "edit_code"
    TOOL_COMMAND = "run_command"

    # 人工确认器：接收提示文本，返回三态决定。
    # 抽成 Callable 是为了能在非交互场景（管道、测试）里换成自动实现；
    # 终端版的交互式确认器由 shell 层提供（要用到界面模块，内核不依赖它）。
    Confirmer = Callable[[str], str]

    def __init__(self, mode: PermissionMode, confirmer: "Confirmer | None") -> None:
        self._mode = mode
        self._confirmer = confirmer
        # 本会话内用户选过「始终允许」的命令原文。加锁：并行工具执行时会被并发读写
        self._session_approved: set[str] = set()
        self._lock = threading.Lock()

    @classmethod
    def for_mode(cls, mode: PermissionMode, confirmer: "Confirmer | None" = None) -> "PermissionHook":
        """按模式创建钩子。

        confirmer 传 None 表示非交互场景（管道、测试）：此时「需要确认」的
        命令一律拒绝，只读命令照常执行。
        """
        if mode == PermissionMode.FULL:
            return cls(mode, lambda message: cls.ALLOW)
        if confirmer is None:
            return cls(mode, lambda message: cls.DENY)
        return cls(mode, confirmer)

    def before_tool(self, name: str, args: dict) -> bool:
        """执行工具前的闸门；返回 False 则模型收到「被拦截」的观察。"""
        command = str(args.get("command", "")) if args else ""

        # 0. 会话内已放行：同一命令不问第二遍。放行清单只会收录策略允许过的命令，
        #    危险命令根本到不了确认环节，所以这里不可能成为危险命令的后门
        if name == self.TOOL_COMMAND:
            with self._lock:
                if command in self._session_approved:
                    return True

        # 1. 只读模式：编辑一律拒绝；执行命令只放行「无副作用」的那些
        #    （只读模式应该允许读，而不是把 run_command 一刀切拒绝 ——
        #      否则连 ls、git status 都跑不了，属于逻辑错误）
        if self._mode == PermissionMode.READ_ONLY:
            if name == self.TOOL_EDIT:
                print("    ✗ 只读模式，已拒绝 " + name + "（需要改文件请用 --permission=edit）")
                return False
            if name == self.TOOL_COMMAND:
                verdict = CommandPolicy.evaluate(command)
                if not verdict.allowed:
                    print("    ✗ " + verdict.reason)
                    return False
                if verdict.needs_approval:
                    print("    ✗ 只读模式只允许无副作用的命令，已拒绝：" + command)
                    return False
                return True  # 只读命令放行
            return True

        # 2. 命令执行：交给策略判定
        if name == self.TOOL_COMMAND:
            verdict = CommandPolicy.evaluate(command)
            if not verdict.allowed:
                print("    ✗ " + verdict.reason)
                return False
            if not verdict.needs_approval:
                return True  # 只读命令，不打扰用户
            if self._confirmer is None:
                print("    ✗ 非交互环境下无法确认，已拒绝。需要放行请加 --permission=full")
                return False
            answer = self._confirmer("执行命令 " + command + " ?")
            if answer == self.ALLOW:
                return True
            if answer == self.ALWAYS_ALLOW:
                with self._lock:
                    self._session_approved.add(command)
                print("    ✓ 本会话内同一命令不再询问")
                return True
            print("    ✗ 已取消该命令")
            return False

        # 3. 其它工具（读文件、列目录、搜索）一律放行
        return True
