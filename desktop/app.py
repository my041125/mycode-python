"""主窗口：对话区 / 输入区 / 状态栏 + 事件分发主循环（桌面版的 Shell 等价物）。

职责对照终端版 shell.Shell：装配 runtime、维护会话、处理输入、
把 worker 线程的事件渲染到界面上。渲染分两类：
    普通行（工具行 / 统计行 / 系统提示）→ 一次性插入；
    流式回答块 → 攒增量、60ms 节流、整块重渲染（对照终端 _LiveBlock 的思路，
    但 Tk 里「原地重绘」只是 delete 标记区间再重新插入，比 ANSI 光标回退简单）。
"""

from __future__ import annotations

import contextlib
import time
import tkinter as tk
from pathlib import Path

from mycode import __version__
from mycode.clients import factory as llm_factory
from mycode.clients.factory import Resolved
from mycode.extensions.scheduler import ScheduledTask, SessionScheduler
from mycode.kernel.permission import PermissionHook, PermissionMode
from mycode.shell import costs
from mycode.shell.language import Language
from mycode.shell.session_store import SessionStore

import desktop.theme as theme
from desktop.bridge import ChatBridge, _friendly_error
from desktop.commands import Commander
from desktop.hook import GuiHook
from desktop.render import insert_markdown, insert_plain
from desktop.runtime import (Runtime, build_agent, build_runtime, build_scheduled_agent,
                             open_session, save_api_key)

# 流式重渲染的最小间隔（秒）：模型增量可能一次一两个字，逐字重渲染整块太浪费
_FLUSH_INTERVAL = 0.06


class DesktopApp:
    """桌面版主窗口。一个实例对应一次交互进程（与终端版 Shell 同构）。"""

    def __init__(self, root: tk.Tk, args: list[str]) -> None:
        self._root = root
        self._args = list(args)
        self._lang = Language.from_args(self._args)
        self._root_dir = Path.cwd().resolve()
        self._store = SessionStore(self._root_dir)
        self._bridge = ChatBridge()
        # 会话内定时任务：调度器随窗口启动，随关窗停止（对照终端版 Shell.__init__）
        self._scheduler = SessionScheduler(self._run_scheduled)
        self._runtime: Runtime | None = None
        self._commands = Commander(self)
        self._fonts: theme.Fonts | None = None

        # 流式块状态：攒增量 + 起始标记（见 _flush_live）
        self._live_buf: list[str] = []
        self._live_mark = False          # 标记是否已放置（标记名固定 "live_start"）
        self._live_had_text = False      # 本轮是否真的收到过文字增量
        self._last_flush = 0.0
        self._busy_ui = False
        self._busy_text = ""             # 状态条左侧的进行中文案（思考中 / 正在回答…）

        # 先解析配置（纯函数，不发请求不建客户端）再建窗：头部信息栏要显示模型；
        # 配置错误（比如 provider=custom 缺 baseUrl）也要能起窗口给人看错误
        self._config_error = ""
        resolved: Resolved | None = None
        try:
            resolved = llm_factory.resolve(self._args)
        except Exception as exc:  # noqa: BLE001
            self._config_error = _friendly_error(exc)
        info = ("配置有误" if resolved is None else
                f"模型 {resolved.model} · 语言 {self._lang.label}"
                + (" · 离线演示" if "--demo" in self._args else ""))
        self._build_ui(info)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self._root.after(50, self._poll)

        if resolved is None:
            self.insert_system("启动失败：" + self._config_error, "error")
            return

        # 没配密钥就先弹窗收集。--demo 用离线假模型不需要密钥；
        # ollama / custom 这类本地服务通常也不需要，都不弹。
        # 弹窗是异步的：用户回答（确定 / 跳过）之后 _finish_startup 才装配并开会话。
        if (resolved.api_key == "" and "--demo" not in self._args
                and resolved.provider not in ("ollama", "custom")):
            self._ask_api_key(resolved)
            return
        self._finish_startup()

    def _finish_startup(self) -> None:
        """装配 runtime 并开会话 —— 启动路径的终点，密钥弹窗回答后也走这里。"""
        self._runtime = build_runtime(self._args, self._root_dir, self._bridge.confirm,
                                      self._scheduler, self._create_persistent_task)
        self._start_session(self._lang, "--continue" in self._args)
        self._refresh_status()

    # ---------------- 界面搭建 ----------------

    def _build_ui(self, info: str) -> None:
        """三段式布局：头部信息栏 / 对话区 / 输入区 + 底部状态条。"""
        root = self._root
        root.title("mycode 桌面版")
        root.geometry("960x680")
        root.minsize(640, 460)
        root.configure(bg=theme.BAR_BG)
        fonts = theme.load_fonts()
        self._fonts = fonts

        # 头部：Logo + 信息栏（对照终端版的扁平头部：左 Logo 右信息）
        header = tk.Frame(root, bg=theme.BAR_BG)
        header.pack(fill="x")
        tk.Label(header, text="🦀 mycode 桌面版", bg=theme.BAR_BG, fg=theme.ACCENT,
                 font=(fonts.body.actual("family"), 12, "bold")).pack(side="left", padx=14, pady=8)
        self._header_info = tk.Label(header, text=info, bg=theme.BAR_BG, fg=theme.BAR_FG,
                                     font=fonts.small)
        self._header_info.pack(side="right", padx=14)

        # 对话区：只读 Text，所有内容经渲染器写入
        chat_frame = tk.Frame(root, bg=theme.CHAT_BG)
        chat_frame.pack(fill="both", expand=True)
        self._chat = tk.Text(chat_frame, bg=theme.CHAT_BG, fg=theme.CHAT_FG,
                             font=fonts.body, state="disabled", wrap="word",
                             borderwidth=0, highlightthickness=0,
                             padx=14, pady=10, insertbackground=theme.CHAT_FG)
        scroll = tk.Scrollbar(chat_frame, command=self._chat.yview, troughcolor=theme.CHAT_BG)
        self._chat.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._chat.pack(side="left", fill="both", expand=True)
        theme.apply_tags(self._chat, fonts)

        # 输入区：多行输入框 + 发送按钮；回车发送、Shift+回车换行
        sep = tk.Frame(root, bg="#33333A", height=1)
        sep.pack(fill="x")
        input_frame = tk.Frame(root, bg=theme.INPUT_BG)
        input_frame.pack(fill="x")
        self._input = tk.Text(input_frame, bg=theme.INPUT_BG, fg=theme.CHAT_FG,
                              font=fonts.body, height=3, wrap="word",
                              borderwidth=0, highlightthickness=0,
                              padx=12, pady=8, insertbackground=theme.CHAT_FG)
        self._input.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=6)
        self._input.bind("<Return>", self._on_enter)
        self._send_btn = tk.Button(input_frame, text="发送 ⏎", command=self._on_send,
                                   bg=theme.ACCENT, fg=theme.CHAT_BG,
                                   activebackground="#F09B55", activeforeground=theme.CHAT_BG,
                                   relief="flat", font=fonts.body, padx=14,
                                   cursor="hand2", takefocus=0)
        self._send_btn.pack(side="right", padx=(4, 10), pady=8, ipady=4)

        # 状态条（对照终端版输入行下方的橙色状态条：权限提示 + 会话统计）
        self._status = tk.Label(root, anchor="w", bg=theme.BAR_BG, fg=theme.BAR_FG,
                                font=fonts.small, padx=10, pady=3)
        self._status.pack(fill="x", side="bottom")
        self._refresh_status()

    def _start_session(self, lang: Language, cont: bool) -> None:
        """建 agent + 开会话并挂上桥。启动时调一次；/lang 切换时再调（全新会话）。"""
        assert self._runtime is not None
        agent = build_agent(self._runtime, self._root_dir, lang, [GuiHook(self._bridge.post)])
        session, notice = open_session(agent, self._store, cont)
        self._bridge.attach(session, self._store)
        self.insert_system(
            f"mycode 桌面版 v{__version__} — 直接输入问题，回车发送；/help 查看全部命令。")
        # 启动提示：上次交互之后是否有新的定时任务报告（对照终端版头部 notices）
        from mycode import cron  # noqa: PLC0415 —— 延迟导入，与终端版同一模式
        pending = cron.pending_reports(self._root_dir)
        if pending:
            self.insert_system(f"有 {len(pending)} 份定时任务报告，/reports 查看", "ok")
        if notice:
            self.insert_system(notice, "ok")

    # ---------------- 事件分发主循环 ----------------

    def _poll(self) -> None:
        """主循环：取走积压事件逐个分发，然后按节流节奏刷新流式块。"""
        try:
            for kind, payload in self._bridge.drain():
                self._dispatch(kind, payload)
            self._flush_live(force=False)
            self._poll_job = self._root.after(50, self._poll)
        except tk.TclError:
            return  # 窗口已销毁：停止轮询（/exit 或关窗后的残余回调）

    def _dispatch(self, kind: str, payload: object) -> None:
        """把桥上的一条事件翻译成界面动作。事件清单见 bridge.py 与 hook.py 的 docstring。"""
        if kind == "text":
            # 流式增量：只攒不发，由 _flush_live 统一节流重渲染
            self._live_buf.append(str(payload))
            if not self._live_had_text:
                self._live_had_text = True
                self._set_status_busy("正在回答…")
        elif kind == "llm_start":
            self._set_status_busy("思考中…")
        elif kind == "tool_start":
            self._seal_live()
            self._append_line(str(payload), "tool")
        elif kind == "tool_end":
            self._append_line(str(payload), "tool_result")
        elif kind == "stdout":
            # worker 期间捕获到的裸 print（权限提示等），原样进聊天区
            self._append_line("    " + str(payload), "dim")
        elif kind in ("compressed", "retry"):
            self._seal_live()
            self._append_line(str(payload), "dim")
        elif kind == "confirm_request":
            rid, message = payload  # type: ignore[misc]
            self._ask_permission(int(rid), str(message))
        elif kind == "task_result":
            self._seal_live()
            self.insert_system(str(payload))
        elif kind == "done":
            self._on_done(payload)  # type: ignore[arg-type]
        elif kind == "error":
            self._seal_live()
            self._append_line("✗ " + str(payload), "error")
        elif kind == "scheduled_start":
            # 定时任务开始（调度线程发起）：可能与正在流式的回答交错，
            # 先收尾当前流式块再插行，避免工具行被下一帧整块重渲染吞掉
            self._seal_live()
            self._append_line(str(payload), "tool")
        elif kind == "scheduled_done":
            task_id, answer = payload  # type: ignore[misc]
            self._seal_live()
            self._append_report(str(task_id), str(answer))
        elif kind == "busy_off":
            self._busy_text = ""
            self._set_busy_ui(False)
            self._refresh_status()

    # ---------------- 输入与发送 ----------------

    def _on_enter(self, event: tk.Event) -> str | None:
        """回车发送；按住 Shift 回车插入换行。"""
        if event.state & 0x1:  # Shift 按下：不拦截，让 Text 正常换行
            return None
        self._on_send()
        return "break"  # 吞掉默认行为：不往输入框里插换行

    def _on_send(self) -> None:
        """发送输入框内容：斜杠命令本地处理，普通文字交给 worker 线程。"""
        text = self._input.get("1.0", "end").strip()
        if not text:
            return
        if self._bridge.busy:
            self.insert_system("正在处理上一条，等它跑完再发。")
            return
        self._input.delete("1.0", "end")
        if text.startswith("/"):
            self._commands.handle(text)  # 返回 False 表示要退出，窗口已在 handle 里销毁
            return
        self._append_user(text)
        if self._bridge.send(text):
            self._set_busy_ui(True)
            self._set_status_busy("思考中…")

    def _append_user(self, text: str) -> None:
        """用户消息：橙色 ❯ 提示符 + 原文 + 空行分隔。"""
        chat = self._chat
        chat.configure(state="normal")
        chat.insert("end", "❯ ", ("user_mark",))
        chat.insert("end", text + "\n\n")
        chat.configure(state="disabled")
        chat.see("end")

    # ---------------- 流式块（对照终端版 _LiveBlock） ----------------

    def _flush_live(self, force: bool) -> None:
        """把攒到的增量渲染出来：整块删除重插（Tk 的「原地重绘」）。

        节流：非强制时 60ms 内不重复渲染。首次增量时在当前位置放下标记，
        之后每次都从标记处删到末尾再重画整块 —— 块内的 Markdown 结构
        （代码围栏等）只有整块重渲染才能保证正确。
        """
        if not self._live_buf:
            return
        now = time.monotonic()
        if not force and now - self._last_flush < _FLUSH_INTERVAL:
            return
        self._last_flush = now
        chat = self._chat
        at_bottom = chat.yview()[1] >= 0.999
        chat.configure(state="normal")
        if not self._live_mark:
            chat.mark_set("live_start", "end-1c")
            chat.mark_gravity("live_start", "left")
            self._live_mark = True
        chat.delete("live_start", "end-1c")
        insert_markdown(chat, "".join(self._live_buf))
        chat.configure(state="disabled")
        if at_bottom:
            chat.see("end")

    def _seal_live(self) -> None:
        """收尾当前流式块（对照 _LiveBlock.seal）：强制刷最后一帧，清空缓冲。

        工具行 / 统计行都要等正文块结束才出现，否则会叠进正在生成的回答里。
        """
        if self._live_buf:
            self._flush_live(force=True)
            self._live_buf = []
            self._append_line("", None)  # 正文与后续行之间留一个空行
        self._live_mark = False

    def _on_done(self, payload: tuple[str, float]) -> None:
        """一轮问答结束：补渲染 / 统计行 / 落盘 / 解除忙碌。"""
        answer, elapsed = payload
        self._seal_live()
        session = self._bridge.session
        if session is None:
            return
        # 没走过流式（非流式客户端或模型没吐字）时整段补渲染
        if answer and not self._live_had_text:
            self._chat.configure(state="normal")
            insert_markdown(self._chat, answer)
            self._chat.configure(state="disabled")
            self._chat.see("end")
        self._append_line(
            f"  {elapsed:.1f}s · {costs.describe(session.total_usage)}"
            f" · 上下文约 {session.context_chars()} 字 · 第 {session.turns} 轮", "dim")
        # 每轮问答后落盘：进程被杀也最多丢当前这一轮（存档内部吞异常）
        session.persist(self._store)
        self._live_had_text = False
        self._refresh_status()

    # ---------------- 定时任务（调度线程的 runner） ----------------

    def _run_scheduled(self, task: ScheduledTask) -> None:
        """到点执行一个会话内定时任务。运行在调度线程上，禁止直接碰控件。

        与终端版 _run_scheduled 同构：独立上下文 + 非交互权限钩子（需确认的
        命令自动拒绝并如实写进结果）；差异只在输出通道 —— 事件全部走桥的
        队列回主线程渲染。工具行可能与正在流式的用户回答交错，界面侧会先
        收尾流式块再插行（见 _dispatch）。
        """
        rt = self._runtime
        assert rt is not None
        permission = PermissionHook.for_mode(PermissionMode.parse(task.permission), None)
        agent = build_scheduled_agent(rt, self._root_dir, self._lang,
                                      [GuiHook(self._bridge.post)], permission)
        self._bridge.post("scheduled_start",
                          f"  ⏰ 定时任务 {task.task_id} 开始执行（独立上下文）：{task.prompt[:36]}")
        try:
            # 权限拒绝提示等散落 print 兜进桥的捕获器（捕获器内部有锁，线程安全）
            with contextlib.redirect_stdout(self._bridge.trap):
                answer = agent.run(task.prompt)
        except Exception as exc:  # noqa: BLE001 —— 定时任务失败如实展示，不拖垮会话
            answer = "ERROR: " + _friendly_error(exc)
        self._bridge.post("scheduled_done", (task.task_id, answer))

    def _create_persistent_task(self, spec: dict) -> str:
        """schedule 工具的 persistent 分支：写持久化任务文件（跨会话存活）。

        延迟导入 cron —— cron 反向 import shell，顶层导入会成环（终端版同款处理）。
        返回给模型的一段确认文本。
        """
        from mycode import cron  # noqa: PLC0415 —— 见上，避免循环导入
        try:
            task = cron.add_task(self._root_dir, **spec)
        except ValueError as exc:
            return "ERROR: " + str(exc)
        return (f"OK: 持久化定时任务「{task.id}」已写入 .mycode/schedules.json，"
                f"下次运行 {task.next_run}（{task.kind}）。"
                "需要系统级自动触发请执行：python -m mycode --cron-register；"
                "/schedules 查看与管理。")

    def _append_report(self, task_id: str, answer: str) -> None:
        """定时任务结果：⏰ 头行（强调色）+ Markdown 渲染正文。"""
        chat = self._chat
        chat.configure(state="normal")
        insert_plain(chat, f"⏰ 定时任务 {task_id}（独立上下文）执行完成：", "tool")
        insert_markdown(chat, answer)
        chat.configure(state="disabled")
        chat.see("end")

    # ---------------- 权限确认对话框 ----------------

    def _ask_permission(self, rid: int, message: str) -> None:
        """模态三选一：允许一次 / 本会话始终允许 / 取消（对齐终端 pick_option）。"""
        fonts = self._fonts
        assert fonts is not None
        dialog = tk.Toplevel(self._root)
        dialog.title("权限确认")
        dialog.configure(bg=theme.INPUT_BG)
        dialog.resizable(False, False)
        dialog.transient(self._root)

        tk.Label(dialog, text="⚠ " + message, bg=theme.INPUT_BG, fg=theme.CHAT_FG,
                 font=fonts.body, wraplength=440, justify="left",
                 padx=20, pady=16).pack()

        def choose(answer: str, line: str, tag: str) -> None:
            """回填三态结果给 worker，并把决定写进聊天区（对照终端确认后的提示行）。"""
            if not dialog.winfo_exists():
                return
            self._bridge.resolve_confirm(rid, answer)
            self._append_line("    " + line, tag)
            dialog.destroy()

        btns = tk.Frame(dialog, bg=theme.INPUT_BG)
        btns.pack(padx=20, pady=(0, 16), fill="x")
        tk.Button(btns, text="允许一次", relief="flat", cursor="hand2", padx=12, pady=4,
                  bg=theme.ACCENT, fg=theme.CHAT_BG, font=fonts.body,
                  command=lambda: choose(PermissionHook.ALLOW, "✓ 允许这一次", "ok"),
                  takefocus=0).pack(side="left")
        tk.Button(btns, text="本会话始终允许", relief="flat", cursor="hand2", padx=12, pady=4,
                  bg="#3A3A40", fg=theme.CHAT_FG, font=fonts.body,
                  command=lambda: choose(PermissionHook.ALWAYS_ALLOW,
                                         "✓ 本会话内同一命令不再询问", "ok"),
                  takefocus=0).pack(side="left", padx=(8, 0))
        tk.Button(btns, text="取消", relief="flat", cursor="hand2", padx=12, pady=4,
                  bg="#3A3A40", fg=theme.CHAT_FG, font=fonts.body,
                  command=lambda: choose(PermissionHook.DENY, "✗ 已取消该命令", "error"),
                  takefocus=0).pack(side="right")

        # Esc = 取消；点窗口关闭按钮也按取消处理，worker 才不会永远等下去
        dialog.bind("<Escape>", lambda _e: choose(PermissionHook.DENY, "✗ 已取消该命令", "error"))
        dialog.protocol("WM_DELETE_WINDOW",
                        lambda: choose(PermissionHook.DENY, "✗ 已取消该命令", "error"))
        dialog.grab_set()
        self._place_centered(dialog)

    # ---------------- 密钥收集弹窗 ----------------

    def _ask_api_key(self, resolved: Resolved) -> None:
        """首启弹窗：没配 apiKey 时收集密钥，回答后继续启动。

        密钥通过启动参数传给装配层（本会话立即生效，参数优先级最高）；
        勾选「保存」则写入 agent.toml —— 下次启动 config 直接读到，不再弹窗。
        跳过也能继续用窗口（离线演示 / 只看界面），只是真实提问会失败。
        """
        fonts = self._fonts
        assert fonts is not None
        dialog = tk.Toplevel(self._root)
        dialog.title("配置模型密钥")
        dialog.configure(bg=theme.INPUT_BG)
        dialog.resizable(False, False)
        dialog.transient(self._root)

        tk.Label(dialog, text=("还没有配置 apiKey，先填一个才能向真实模型提问。\n"
                               f"当前将使用：{resolved.provider} · {resolved.model}"
                               "（密钥请到对应服务商控制台获取）"),
                 bg=theme.INPUT_BG, fg=theme.CHAT_FG, font=fonts.body,
                 wraplength=440, justify="left", padx=20).pack(pady=(16, 8))
        entry = tk.Entry(dialog, show="•", width=46, font=fonts.mono,
                         bg="#2E2E34", fg=theme.CHAT_FG, relief="flat",
                         insertbackground=theme.CHAT_FG)
        entry.pack(padx=20, ipady=4, fill="x")
        entry.focus_set()
        hint = tk.Label(dialog, text="", bg=theme.INPUT_BG, fg=theme.ERROR, font=fonts.small)
        hint.pack(padx=20, pady=(4, 0), anchor="w")
        save_var = tk.BooleanVar(value=True)
        tk.Checkbutton(dialog, text="保存到 agent.toml，下次启动不再询问",
                       variable=save_var, bg=theme.INPUT_BG, fg=theme.BAR_FG,
                       activebackground=theme.INPUT_BG, activeforeground=theme.CHAT_FG,
                       selectcolor="#3A3A40", font=fonts.small,
                       relief="flat", highlightthickness=0).pack(padx=20, pady=(2, 8), anchor="w")

        def confirm() -> None:
            """确定：密钥进启动参数（本会话生效），勾选了就顺手写进 agent.toml。"""
            key = entry.get().strip()
            if not key:
                hint.configure(text="密钥为空：填好再点「确定」，或点「跳过」稍后再配。")
                return
            dialog.destroy()
            self._args.append("--api-key=" + key)
            self._finish_startup()
            self.insert_system("✓ 密钥已生效（" + resolved.provider + " · " + resolved.model + "）", "ok")
            self.insert_system(save_api_key(self._root_dir, key) if save_var.get()
                               else "密钥仅本次会话生效；要长期保存请勾选「保存到 agent.toml」。")

        def skip() -> None:
            """跳过：照常启动（密钥为空，真实提问会在模型调用时报错）。"""
            dialog.destroy()
            self._finish_startup()
            self.insert_system("未配置 apiKey：真实模型提问会失败。可重启填入、手改 agent.toml，"
                               "或改用 python -m desktop --demo 离线演示。")

        btns = tk.Frame(dialog, bg=theme.INPUT_BG)
        btns.pack(padx=20, pady=(0, 16), fill="x")
        tk.Button(btns, text="跳过", relief="flat", cursor="hand2", padx=14, pady=4,
                  bg="#3A3A40", fg=theme.CHAT_FG, font=fonts.body,
                  command=skip, takefocus=0).pack(side="right")
        tk.Button(btns, text="确定", relief="flat", cursor="hand2", padx=14, pady=4,
                  bg=theme.ACCENT, fg=theme.CHAT_BG, font=fonts.body,
                  command=confirm, takefocus=0).pack(side="right", padx=(0, 8))

        # 回车 = 确定；Esc / 关闭按钮 = 跳过（不会卡住启动，也不会卡住 worker）
        dialog.bind("<Return>", lambda _e: confirm())
        dialog.bind("<Escape>", lambda _e: skip())
        dialog.protocol("WM_DELETE_WINDOW", skip)
        dialog.grab_set()
        self._place_centered(dialog)

    def _place_centered(self, dialog: tk.Toplevel) -> None:
        """把对话框弹在主窗口中央（几何计算要等控件定型，先 update 一次）。"""
        dialog.update_idletasks()
        x = self._root.winfo_x() + (self._root.winfo_width() - dialog.winfo_width()) // 2
        y = self._root.winfo_y() + (self._root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ---------------- 输出与状态 ----------------

    def insert_system(self, text: str, tag: str | None = "dim") -> None:
        """系统输出（命令结果、提示、错误）：纯文本插入聊天区，保留内部换行。"""
        self._append_line(text, tag)

    def _append_line(self, text: str, tag: str | None) -> None:
        """往对话区插一行纯文本；内容贴着窗口底部时自动跟随滚动。"""
        chat = self._chat
        at_bottom = chat.yview()[1] >= 0.999
        chat.configure(state="normal")
        insert_plain(chat, text, tag)  # type: ignore[arg-type]
        chat.configure(state="disabled")
        if at_bottom:
            chat.see("end")

    def _set_busy_ui(self, busy: bool) -> None:
        """忙碌状态切换：发送按钮禁用 / 恢复。输入框始终可编辑，方便先打好下一条。"""
        self._busy_ui = busy
        self._send_btn.configure(state="disabled" if busy else "normal",
                                 text="思考中…" if busy else "发送 ⏎")

    def _set_status_busy(self, text: str) -> None:
        """状态条左半：忙时显示进行中的动作。"""
        self._busy_text = text
        self._refresh_status()

    def _refresh_status(self) -> None:
        """状态条：左边权限与快捷提示（对照终端的橙色状态条），右边会话统计。"""
        rt = self._runtime
        if rt is None:
            text = ("配置有误 · 请检查 agent.toml 或启动参数" if self._config_error
                    else "等待密钥配置…")
            self._status.configure(text=text)
            return
        left = self._busy_text
        prefix = left + "  ·  " if left else ""
        base = (prefix + f"⏩⏩ 权限 {rt.mode.cli}（/permission 查看）"
                f" · /help 全部命令 · /exit 退出")
        session = self._bridge.session
        stats = ""
        if session is not None and session.turns > 0:
            usage = session.total_usage
            money = costs.money(usage)
            stats = (f"    |    第 {session.turns} 轮 · {usage.total()} tokens"
                     + (f" · {money}" if money else ""))
        self._status.configure(text=base + stats)

    # ---------------- 窗口生命周期 ----------------

    def switch_language(self, nxt: Language) -> None:
        """/lang 切换：重建 agent、开全新会话（语言约束写在 system 提示词里）。"""
        self._lang = nxt
        assert self._runtime is not None
        rt = self._runtime
        self._header_info.configure(
            text=f"模型 {rt.resolved.model} · 权限 {rt.mode.cli} · 语言 {nxt.label}"
                 + (" · 离线演示" if "--demo" in self._args else ""))
        self._start_session(nxt, cont=False)

    def exit_app(self) -> None:
        """/exit：关窗（worker 都是 daemon 线程，进程随主窗口退出）。"""
        self._on_close()

    def _on_close(self) -> None:
        # 停调度线程（join ≤1 秒）：未触发的会话内任务随退出失效（终端版同款语义）
        self._scheduler.stop()
        # 取消排队的轮询回调再销毁，避免窗口销毁后残余回调报 TclError
        try:
            self._root.after_cancel(self._poll_job)
        except Exception:  # noqa: BLE001 —— 回调已触发或已失效都无所谓
            pass
        self._root.destroy()

    # ---------------- Commander / 命令需要只读访问的属性 ----------------

    @property
    def runtime(self) -> Runtime:
        """运行期依赖（commands.py 用；启动失败时窗口只有错误提示，不会调到这里）。"""
        assert self._runtime is not None
        return self._runtime

    @property
    def bridge(self) -> ChatBridge:
        return self._bridge

    @property
    def lang(self) -> Language:
        return self._lang

    @property
    def args(self) -> list[str]:
        return self._args

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def scheduler(self) -> SessionScheduler:
        """会话内调度器（commands.py 的 /schedules 管理子命令用）。"""
        return self._scheduler

    def insert_report(self, title: str, content: str) -> None:
        """报告展示：暗灰路径行 + Markdown 正文（报告是 md 文件，按 Markdown 渲染）。"""
        self._seal_live()
        chat = self._chat
        chat.configure(state="normal")
        insert_plain(chat, "—— " + title + " ——", "dim")
        insert_markdown(chat, content)
        chat.configure(state="disabled")
        chat.see("end")
