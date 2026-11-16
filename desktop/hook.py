"""GUI 钩子与 presenter 等价物：把 agent 的内部动作转成事件，投递给界面线程。

对照终端版的三个角色：
    ConsoleHook     → GuiHook：六类钩子事件转成格式化好的文本行投递出去
    ui 模块         → PlainPresenter：TodoTool 需要的 5 方法界面能力（着色全用原文）
    _console_confirmer → 由 bridge.confirm 承担（见 bridge.py，需要跨线程等待）

工具行的格式刻意复刻 ConsoleHook（⏺ 名字(参数) / ⎿ 结果首行，超长截断）——
两个前端长得一样，用户从终端切到桌面不用重新认符号。
"""

from __future__ import annotations

from typing import Any, Callable

from mycode.kernel.hooks import Hook

# 参数值 / 结果的最长显示字符数（与 ConsoleHook.ARG_MAX / RESULT_MAX 一致）
_ARG_MAX = 46
_RESULT_MAX = 88

# 事件投递函数的类型：bridge.post(kind, payload)，线程安全
PostFn = Callable[[str, object], None]


def format_tool_call(name: str, args: dict | None) -> str:
    """工具调用行：⏺ 名字(键="值")，参数压成一行并截断。"""
    if not args:
        return "  ⏺ " + name + "()"
    rendered = ", ".join(f'{k}="{_shorten(v)}"' for k, v in args.items())
    return "  ⏺ " + name + "(" + rendered + ")"


def format_tool_result(result: str | None) -> str:
    """工具结果行：⎿ 结果第一行有效内容，太长截断。"""
    if not result or not result.strip():
        return "    ⎿ (无输出)"
    for line in result.split("\n"):
        if line.strip():
            text = line.strip()
            return "    ⎿ " + (text if len(text) <= _RESULT_MAX else text[:_RESULT_MAX] + "…")
    return "    ⎿ (无输出)"


def _shorten(value: Any) -> str:
    """参数值可能是一大段代码，压成一行并截断（复刻 ConsoleHook._shorten）。"""
    text = str(value).replace("\r", "").replace("\n", "⏎").strip() if value is not None else ""
    return text if len(text) <= _ARG_MAX else text[:_ARG_MAX] + "…"


class GuiHook(Hook):
    """GUI 钩子：所有事件都转成文本行投递出去，绝不 print（GUI 没有可依赖的控制台）。

    事件清单（kind → payload）：
        tool_start  → 工具调用行文本
        tool_end    → 工具结果行文本
        compressed  → 上下文压缩提示行
        retry       → 模型重试提示行
        llm_start   → None（每轮发起模型调用，界面可显示「思考中」）
    """

    def __init__(self, post: PostFn) -> None:
        self._post = post

    def before_llm(self, ctx) -> None:
        """发起模型调用：投一个空事件，界面显示等待状态。"""
        self._post("llm_start", None)

    def before_tool(self, name: str, args: dict) -> bool:
        """工具调用前：投递调用行。只展示不拦截（拦截是 PermissionHook 的职责）。"""
        self._post("tool_start", format_tool_call(name, args))
        return True

    def after_tool(self, name: str, result: str) -> None:
        """工具结束后：投递结果行。"""
        self._post("tool_end", format_tool_result(result))

    def on_context_compressed(self, removed_messages: int, saved_chars: int, summary: str) -> None:
        """压缩发生时明确告诉用户：记忆被改写了，别让它悄悄发生。"""
        self._post("compressed",
                   f"  ⇣ 上下文已压缩：移除 {removed_messages} 条消息，省下约 {saved_chars} 字")

    def on_model_retry(self, attempt: int, wait_millis: int, reason: str) -> None:
        """重试前如实播报，不然等待退避的几秒看起来像卡死。"""
        msg = (reason or "").replace("\n", " ").strip()
        if len(msg) > 60:
            msg = msg[:60] + "…"
        self._post("retry",
                   f"  ↻ 模型调用失败，{wait_millis // 1000} 秒后重试（第 {attempt} 次）：{msg}")


class PlainPresenter:
    """ConsolePresenter 的 GUI 等价物：给 TodoTool 这类要「着色文本」的工具用。

    GUI 的着色发生在渲染层（tag），工具拿到的字符串保持原文即可；
    转圈动画在 GUI 里由状态栏承担，这里都是空操作。
    结构化类型满足 kernel.hooks.ConsolePresenter 协议，与终端版传 ui 模块等价。
    """

    def accent(self, text: str) -> str:
        return text

    def bold(self, text: str) -> str:
        return text

    def dim(self, text: str) -> str:
        return text

    def start_thinking(self) -> None:
        pass

    def stop_thinking(self) -> None:
        pass
