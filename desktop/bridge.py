"""线程桥：worker 线程跑会话、事件队列连回界面主线程、权限确认的跨线程等待。

为什么需要它：内核是纯同步阻塞代码（ChatSession.ask 会一直跑到本轮结束），
而 Tk 只允许主线程碰控件。解法是经典的两级结构：

    worker 线程  ──ask() 的流式增量、GuiHook 钩子事件、裸 print──▶  事件队列
    主线程 after(50ms) 轮询队列 ──分发──▶ 控件更新 / 模态对话框

权限确认是唯一的反向通道：confirmer 在 worker 线程被 PermissionHook 同步调用，
这里把它包装成「登记请求 → 阻塞等 Event → 返回三态」，
弹框与点击都发生在主线程（confirm_request 事件 + resolve_confirm 回填）。

worker 期间还用 redirect_stdout 把 kernel/tools 里散落的裸 print
（权限拒绝提示、TodoTool 清单等）逐行捕获成 stdout 事件 ——
一处兜住所有「输出通道不走界面」的漏点。
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
import io
from typing import Callable

from mycode.kernel.session import ChatSession
from mycode.shell.session_store import SessionStore

# 事件类型：(kind, payload)。kind 是字符串标签，payload 由 app.py 的分发器解释
Event = tuple[str, object]


def _friendly_error(exc: Exception) -> str:
    """把英文异常翻成人话（与终端版 shell._friendly_error 同一套规则）。

    底层异常信息基本都是英文（HTTP 401、Connection refused…），直接抛给用户等于没提示。
    按常见故障给一句中文解释，再附上原始信息便于排查。
    """
    raw = str(exc) or type(exc).__name__
    lower = raw.lower()
    if "401" in raw or "unauthorized" in lower or "invalid api key" in lower:
        return "模型服务拒绝了请求（401），请检查 agent.toml 里的 apiKey 是否填对。原始信息：" + raw
    if "404" in raw or "not found" in lower:
        return "接口地址不存在（404），请检查 endpoint 是否正确。原始信息：" + raw
    if "429" in raw or "rate limit" in lower:
        return "请求太频繁或额度用尽（429），稍等一会儿再试。原始信息：" + raw
    if "403" in raw or "forbidden" in lower:
        return "没有访问权限（403），确认这个 key 是否开通了该模型。原始信息：" + raw
    if "timed out" in lower or "timeout" in lower:
        return "连接超时，检查网络或代理设置。原始信息：" + raw
    if "unknownhost" in lower or "connection refused" in lower or "getaddrinfo failed" in lower:
        return "连不上服务器，检查网络或 baseUrl。原始信息：" + raw
    return raw


class _StdoutTrap(io.TextIOBase):
    """redirect_stdout 的目标：把散落的裸 print 逐行捕获成 stdout 事件。

    print 可能来自多个线程（并行工具池里跑的工具也可能输出），缓冲区读写加锁；
    只有非空行才投递 —— print() 空行分隔符对 GUI 没有意义。
    """

    def __init__(self, post: Callable[[str, object], None]) -> None:
        self._post = post
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._post("stdout", line)
        return len(s)

    def flush(self) -> None:
        pass  # 无缓冲，无事可做


class _PendingConfirm:
    """一次待确认的权限请求：Event 等 worker 回来，answer 存用户的三态选择。"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.answer = ""


class ChatBridge:
    """会话与界面之间的桥。一个实例对应一个主窗口。"""

    def __init__(self) -> None:
        self._q: queue.Queue[Event] = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._seq = 0
        self._pending: dict[int, _PendingConfirm] = {}
        self._session: ChatSession | None = None
        self._store: SessionStore | None = None
        self._trap = _StdoutTrap(self.post)

    # ---------------- 装配 ----------------

    def attach(self, session: ChatSession, store: SessionStore) -> None:
        """把建好的会话挂上桥（runtime 装配完成后由 app 调用；/lang 换会话时重新挂）。"""
        self._session = session
        self._store = store

    @property
    def session(self) -> ChatSession | None:
        """当前会话（斜杠命令在主线程读它的状态；ask 期间不读，无并发问题）。"""
        return self._session

    @property
    def busy(self) -> bool:
        """是否有一轮问答或任务正在进行。"""
        return self._busy

    @property
    def trap(self) -> "io.TextIOBase":
        """stdout 捕获器。定时任务线程也用它兜住散落的裸 print（内部有锁，线程安全）。"""
        return self._trap

    # ---------------- 事件通道 ----------------

    def post(self, kind: str, payload: object) -> None:
        """线程安全的事件投递入口（worker 线程、钩子、stdout 捕获都走这里）。"""
        self._q.put((kind, payload))

    def drain(self) -> list[Event]:
        """主线程一次取走当前积压的全部事件（没有就返回空表）。"""
        events: list[Event] = []
        while True:
            try:
                events.append(self._q.get_nowait())
            except queue.Empty:
                return events

    # ---------------- 提交任务 ----------------

    def send(self, text: str) -> bool:
        """提交一轮提问。忙碌时返回 False（界面据此拒绝连发）。"""
        if self._busy or self._session is None:
            return False
        self._busy = True
        threading.Thread(target=self._run_round, args=(text,),
                         name="mycode-gui-round", daemon=True).start()
        return True

    def run_text_task(self, fn: Callable[[], str]) -> bool:
        """提交一个「跑完给一段文字」的本地任务（/compress 这类会调模型的命令）。

        与 send 互斥：压缩期间不能再提问，提问期间也不能压缩 ——
        两者都动上下文，并行没有意义还容易乱。
        """
        if self._busy or self._session is None:
            return False
        self._busy = True
        threading.Thread(target=self._run_task, args=(fn,),
                         name="mycode-gui-task", daemon=True).start()
        return True

    def _run_round(self, text: str) -> None:
        """一轮完整问答：ask 阻塞到结束，结果与统计走 done / error 事件。"""
        start = time.monotonic()
        try:
            with contextlib.redirect_stdout(self._trap):
                answer = self._session.ask(text, sink=self.post_text)
        except Exception as exc:  # noqa: BLE001 —— 单次失败不退出，用户可以直接重试
            self.post("error", _friendly_error(exc))
        else:
            self.post("done", (answer, time.monotonic() - start))
        finally:
            self._busy = False
            self.post("busy_off", None)

    def _run_task(self, fn: Callable[[], str]) -> None:
        """跑一个本地任务，结果以 task_result 事件带回（文字已格式化好）。"""
        try:
            with contextlib.redirect_stdout(self._trap):
                result = fn()
        except Exception as exc:  # noqa: BLE001
            self.post("error", _friendly_error(exc))
        else:
            self.post("task_result", result)
        finally:
            self._busy = False
            self.post("busy_off", None)

    def post_text(self, delta: str) -> None:
        """流式回调（StreamSink 的裸函数形态）：文本增量直接进队列。"""
        self.post("text", delta)

    # ---------------- 权限确认（反向通道） ----------------

    def confirm(self, message: str) -> str:
        """权限确认器（PermissionHook.Confirmer）：worker 线程同步调用，等用户点框。

        等待无超时 —— 权限确认想考虑多久都行，与终端版 pick_option 的行为一致。
        返回值是 PermissionHook 的三态常量（allow / always / deny）。
        """
        pending = _PendingConfirm()
        with self._lock:
            self._seq += 1
            rid = self._seq
            self._pending[rid] = pending
        self.post("confirm_request", (rid, message))
        pending.event.wait()
        return pending.answer

    def resolve_confirm(self, rid: int, answer: str) -> None:
        """主线程回填用户的选择（对话框按钮回调里调用），唤醒 worker。"""
        with self._lock:
            pending = self._pending.pop(rid, None)
        if pending is not None:
            pending.answer = answer
            pending.event.set()
