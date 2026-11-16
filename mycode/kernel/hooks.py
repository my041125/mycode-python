"""
内核部件 4：拦截钩子（对应 Java 版 Hook / ConsoleHook / LoggingHook）。

这是把「玩具」和「能上生产」区分开的东西：日志、限流、审计、人工审批、
危险工具拦截，全部挂在这里，不需要改主循环一行代码。
Spring AI 2.0 把这个思路叫 Advisor 链，本质就是拦截器。

六个切入点正好夹在循环的两个关键位置上：
    调用模型前 → 调模型后 → 执行工具前 → 执行工具后（外加压缩通知与重试通知）

分层说明：ConsoleHook 要画终端界面，但内核层禁止 import 外围（shell）模块，
所以界面渲染对象由外部注入（依赖倒置）—— shell 层把 Ui 传进来，演示场景
传 None 则退化为无颜色的纯文本输出。
"""

from __future__ import annotations

from typing import Any, Protocol


class Hook:
    """钩子接口。全部方法都有空实现，实现方按需覆盖即可。"""

    def before_llm(self, ctx) -> None:
        """调用模型之前。适合做：日志、上下文裁剪、注入临时提示、计费埋点。"""

    def after_llm(self, result) -> None:
        """调用模型之后。适合做：记录 token 消耗、统计工具调用意图、链路追踪。"""

    def before_tool(self, name: str, args: dict) -> bool:
        """执行工具之前。返回 False 则拒绝执行该工具。

        拒绝之后模型不会直接崩掉，而是收到一条「被拦截」的观察，
        它能自己决定换个方式或者告诉用户不具备权限 —— 这是人工审批卡点的实现方式。
        """
        return True

    def after_tool(self, name: str, result: str) -> None:
        """执行工具之后。适合做：审计日志、结果脱敏、耗时统计。"""

    def on_context_compressed(self, removed_messages: int, saved_chars: int, summary: str) -> None:
        """上下文被压缩之后。

        这是一次「记忆改写」，用户有权知道发生了什么，否则会奇怪
        「它怎么突然忘了我刚才说的」。
        """

    def on_model_retry(self, attempt: int, wait_millis: int, reason: str) -> None:
        """模型调用瞬时失败（限流、超时、断连），即将按退避等待后重试。

        不提示用户的话，等待的几秒看起来就像卡死了。
        """


class ConsolePresenter(Protocol):
    """ConsoleHook 需要的最小界面能力（结构化类型，shell 层的 Ui 天然满足）。"""

    def accent(self, text: str) -> str: ...
    def bold(self, text: str) -> str: ...
    def dim(self, text: str) -> str: ...
    def start_thinking(self) -> None: ...
    def stop_thinking(self) -> None: ...


class ConsoleHook(Hook):
    """面向命令行的钩子：把 agent 的内部动作渲染成终端里能看懂的过程。

    输出风格参考 Claude Code：

        ⏺ edit_code(path="samples/InvoiceService.py", op="replace")
          ⎿  已修改 samples/InvoiceService.py

    ⏺ 用强调色，⎿ 结果行用暗色 —— 主次分明，扫一眼就知道模型在干什么。

    与 LoggingHook 的区别：LoggingHook 是给开发调试看的（打印上下文长度、轮次），
    ConsoleHook 是给用户看的（只展示工具调用和结果），所以刻意去掉了噪音。
    """

    # 参数值 / 结果的最长显示字符数，太长会把终端刷乱
    ARG_MAX = 46
    RESULT_MAX = 88

    def __init__(self, depth: int = 0, presenter: ConsolePresenter | None = None) -> None:
        """depth：0 是主代理（缩进 2 格），子代理是 1（缩进 4 格）。

        子代理的活动多缩一层，扫一眼就能分清哪些是它干的、哪些是主代理干的。
        presenter：界面渲染对象（shell 层注入）；None 时输出无色纯文本。
        """
        self._depth = depth
        self._presenter = presenter

    # ---- 界面能力的兜底：没有 presenter 时用恒等函数与空操作 ----

    def _accent(self, text: str) -> str:
        return self._presenter.accent(text) if self._presenter else text

    def _bold(self, text: str) -> str:
        return self._presenter.bold(text) if self._presenter else text

    def _dim(self, text: str) -> str:
        return self._presenter.dim(text) if self._presenter else text

    def _start_thinking(self) -> None:
        if self._presenter:
            self._presenter.start_thinking()

    def _stop_thinking(self) -> None:
        if self._presenter:
            self._presenter.stop_thinking()

    def _indent(self) -> str:
        """调用行缩进：主代理 2 格，每深一层多 2 格。"""
        return "  " * (self._depth + 1)

    # ---- 六个拦截点 ----

    def before_llm(self, ctx) -> None:
        """调模型前启动动画转圈（非交互环境下 presenter 为 None，自动变空操作）。"""
        self._start_thinking()

    def after_llm(self, result) -> None:
        """模型回来后停掉转圈。纯工具调用的轮次（没有文字增量）靠这里收尾。"""
        self._stop_thinking()

    def before_tool(self, name: str, args: dict) -> bool:
        """工具调用：打印成 ⏺ 名字(参数=值) 的形式。

        名字加粗、参数保持原样 —— 主次分明，扫一眼就能找到工具名。
        """
        self._stop_thinking()
        print(self._indent() + self._accent("⏺ ") + self._bold(name)
              + "(" + self._render_args(args) + ")")
        return True

    def after_tool(self, name: str, result: str) -> None:
        """工具结果：接在调用下面，第一行即可，太长截断。"""
        print(self._indent() + "  " + self._dim("⎿ " + self._first_line(result)))

    def on_context_compressed(self, removed_messages: int, saved_chars: int, summary: str) -> None:
        """压缩发生时明确告诉用户：记忆被改写了，别让它悄悄发生。"""
        self._stop_thinking()
        print(self._indent() + self._dim(
            f"⇣ 上下文已压缩：移除 {removed_messages} 条消息，省下约 {saved_chars} 字"))

    def on_model_retry(self, attempt: int, wait_millis: int, reason: str) -> None:
        """重试前如实播报，不然等待退避的几秒看起来像卡死。"""
        self._stop_thinking()
        msg = (reason or "").replace("\n", " ").strip()
        if len(msg) > 60:
            msg = msg[:60] + "…"
        print(self._indent() + self._dim(
            f"↻ 模型调用失败，{wait_millis // 1000} 秒后重试（第 {attempt} 次）：{msg}"))

    # ---- 渲染辅助 ----

    def _render_args(self, args: dict | None) -> str:
        """把参数渲染成 键="值" 一行，超长就截断并加省略号。"""
        if not args:
            return ""
        return ", ".join(f'{k}="{self._shorten(v)}"' for k, v in args.items())

    @staticmethod
    def _shorten(value: Any) -> str:
        """参数值可能是一大段代码，压成一行并截断。"""
        text = str(value).replace("\r", "").replace("\n", "⏎").strip() if value is not None else ""
        return text if len(text) <= ConsoleHook.ARG_MAX else text[:ConsoleHook.ARG_MAX] + "…"

    @staticmethod
    def _first_line(result: str | None) -> str:
        """取结果的第一行有效内容；结果常是多行，全打出来会淹掉界面。"""
        if not result or not result.strip():
            return "(无输出)"
        for line in result.split("\n"):
            if line.strip():
                text = line.strip()
                return text if len(text) <= ConsoleHook.RESULT_MAX else text[:ConsoleHook.RESULT_MAX] + "…"
        return "(无输出)"


class LoggingHook(Hook):
    """日志钩子：把主循环的每一步都打印出来。

    调试 agent 时这是最值钱的一件东西 —— 出问题时你不需要猜，
    直接能看到：上下文多长了、模型想调哪个工具、参数是什么、工具返回了什么、
    哪一轮收敛。

    生产环境不要用它（每次都打控制台），应该换成写日志文件或上报链路追踪。
    """

    def __init__(self) -> None:
        # 当前轮次计数，每调一次模型加一，用来让日志能对上「第几轮」
        self._step = 0

    def before_llm(self, ctx) -> None:
        """调用模型前：打印轮次和上下文长度。上下文突然暴涨通常是工具结果没截断。"""
        self._step += 1
        print(f"\n  [第 {self._step} 轮] 上下文约 {ctx.estimate_tokens()} 字，发起模型调用")

    def after_llm(self, result) -> None:
        """调用模型后：把模型的意图翻译成人话，是「要调工具」还是「可以回答了」。"""
        if result.has_tool_calls:
            for c in result.tool_calls:
                print(f"    → 模型要求调用 {c.name}({c.args_json})")
        else:
            print("    → 模型给出最终文本，不再调用工具")

    def before_tool(self, name: str, args: dict) -> bool:
        """执行工具前：演示审批 / 风控钩子。

        真实系统在这里接人工确认、频率限制或危险工具黑名单，
        例如「支付、删除、发消息」这类不可逆操作必须先过审批。
        """
        dangerous = name.startswith("delete_") or name.startswith("pay_")
        if dangerous:
            print("    ✗ 拦截高风险工具: " + name)
        return not dangerous

    def after_tool(self, name: str, result: str) -> None:
        """执行工具后：打印观察结果。换行压成 ⏎ 并截断，避免刷屏。"""
        one = result.replace("\n", " ⏎ ")
        if len(one) > 90:
            one = one[:90] + "..."
        print("    ← 观察: " + one)
