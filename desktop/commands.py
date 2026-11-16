"""斜杠命令：本地操作不进模型，清单与实现和终端版逐条对齐。

与终端版的差异只有输出通道：shell 用 print 打到终端，这里全部落到聊天区
（app.insert_system）。需要调用模型的 /compress 走 bridge.run_text_task
（线程桥与提问互斥），其余都是本地快速操作，直接在主线程执行。
"""

from __future__ import annotations

import contextlib
import difflib
import io
import time
from pathlib import Path

from mycode.clients import factory as llm_factory
from mycode.extensions.skills import SkillRegistry
from mycode.kernel.registry import ToolRegistry
from mycode.shell import costs
from mycode.shell.language import Language
from mycode.tools.backup import BackupStore, Snapshot
from mycode.tools.code_tools import iter_project_files
from mycode.tools.todo import TodoTool

from desktop.bridge import _friendly_error

# 全部已实现的斜杠命令，供「未知命令」做最接近建议
KNOWN_COMMANDS = ("/help", "/clear", "/tools", "/files", "/todo", "/skills", "/model",
                  "/permission", "/lang", "/cost", "/compress", "/undo", "/history",
                  "/schedules", "/reports", "/exit", "/quit", "/q")

# 二期扩展的斜杠命令：先占位提示，不静默吞掉
_PHASE2_COMMANDS = {"/agents", "/memory", "/task"}

_HELP_TEXT = """用法：直接输入问题，回车发送（Shift+回车换行）。需要它动手时会自动调用工具。

斜杠命令（本地执行，不消耗 token）：
  /help        显示本帮助
  /clear       清空上下文，开始新会话
  /tools       查看已加载的工具
  /todo        查看当前任务清单
  /files       列出当前项目的代码文件
  /skills      查看已加载的技能（Agent Skills，兼容 Claude Code）
  /model       查看当前模型配置
  /permission  查看当前权限模式
  /lang        查看或切换回答语言（/lang zh | /lang en）
  /cost        查看 token 用量与耗时
  /compress    手动压缩一次上下文（自动压缩默认已开启）
  /undo        撤销最近一次文件修改（/undo 路径 指定文件）
  /history     查看最近几次修改前的快照
  /schedules   查看定时任务（cancel/pause/resume/delete <id> 管理）
  /reports     查看定时任务的最新报告
  /exit        退出（同窗口关闭）
  /agents /memory  二期扩展（子代理 / 项目记忆）

权限模式（启动参数 --permission=）：
  read-only    只读，不能改文件也不能执行命令
  edit         可改文件；执行命令需要你在对话框里确认（默认）
  full         全部放行，不再询问

会话与记忆：python -m desktop --continue 恢复最近一次会话（每轮自动存档到 .mycode/sessions/）。
快照说明：每次改文件前都会在 .mycode/backups/ 下存一份，每个文件保留最近 20 个版本。

定时任务（会话内 + 持久化）：
  会话内：对模型说「10 分钟后提醒我看看测试结果」，它会调 schedule 工具（退出即失效）
  持久化：schedule 工具 scope=persistent 写入 .mycode/schedules.json（跨会话）
  系统级：python -m mycode --cron-register 注册 Windows 计划任务后自动拉起 --cron
  报告：持久化任务每次运行结果落盘 .mycode/schedules/，/reports 查看
"""

# 技能扫描目录的说明文案（与终端版一致）
_SKILLS_HINT = ("把技能放到下列任一目录即可（每个技能一个子目录，内含 SKILL.md）：\n"
                "  项目：<当前目录>/.mycode/skills/\n"
                "  共用：<当前目录>/.claude/skills/\n"
                "  个人：~/.claude/skills/   ← Claude Code 的技能默认在这里\n"
                "  内置：<项目根>/skills/   ← 本仓库自带一个 hello-mycode 演示")


class Commander:
    """斜杠命令分发器。持有主窗口引用，输出全部走 app.insert_system。"""

    def __init__(self, app) -> None:
        self._app = app

    def handle(self, text: str) -> bool:
        """处理一条命令。返回 False 表示要退出（对齐终端版 _handle_command 的约定）。"""
        cmd = text.split()[0].lower()
        app = self._app
        rt = app.runtime

        if cmd in ("/exit", "/quit", "/q"):
            session = app.bridge.session
            if session is not None:
                app.insert_system(f"再见。本次共 {session.turns} 轮，{session.elapsed_seconds()} 秒。")
            app.exit_app()
            return False
        if cmd == "/help":
            app.insert_system(_HELP_TEXT)
        elif cmd == "/clear":
            session = app.bridge.session
            if session is not None:
                session.reset()
            app.insert_system("已清空上下文，开始新会话。", "ok")
        elif cmd == "/lang":
            self._switch_language(text)
        elif cmd == "/tools":
            self._list_tools(rt.tools)
        elif cmd == "/files":
            self._list_files(app.root_dir)
        elif cmd == "/skills":
            self._list_skills(rt.skills)
        elif cmd == "/todo":
            self._print_todo(rt.tools)
        elif cmd == "/model":
            self._print_model()
        elif cmd == "/permission":
            app.insert_system(f"当前权限模式：{rt.mode.cli}（{rt.mode.label}）")
        elif cmd == "/cost":
            self._print_cost()
        elif cmd == "/compress":
            self._compress()
        elif cmd == "/undo":
            self._undo(text)
        elif cmd == "/history":
            self._history()
        elif cmd == "/schedules":
            self._list_schedules(text)
        elif cmd == "/reports":
            self._show_reports(text)
        elif cmd in _PHASE2_COMMANDS:
            app.insert_system(f"{cmd} 属于二期扩展（MCP / 技能 / 子代理），当前版本尚未接入。")
        else:
            message = f"未知命令: {cmd}"
            suggestion = self._suggest(cmd)
            if suggestion:
                message += f"，你是想输入 {suggestion} 吗？"
            app.insert_system(message + "（/help 查看全部命令）")
        return True

    # ---------------- 各命令实现（对照终端版同名方法） ----------------

    def _switch_language(self, text: str) -> None:
        """/lang：查看或切换回答语言。切换要重建 agent 与会话（语言约束写在 system 里）。"""
        arg = text[len("/lang"):].strip()
        if not arg:
            lang = self._app.lang
            self._app.insert_system(f"当前语言：{lang.code}（{lang.label}）")
            self._app.insert_system("用法：/lang zh 切换中文，/lang en 切换英文")
            return
        nxt = Language.parse(arg)
        if nxt == self._app.lang:
            self._app.insert_system(f"已经是 {nxt.label} 了。")
            return
        self._app.switch_language(nxt)
        self._app.insert_system(
            f"已切换到 {nxt.label}。因为语言约束写在系统提示词里，上下文已清空、重新开始。", "ok")

    def _list_tools(self, tools: ToolRegistry) -> None:
        """/tools：列出已加载工具，描述只取第一句，保持列表清爽。"""
        lines = [f"已加载 {len(tools.all())} 个工具："]
        for tool in tools.all():
            desc = tool.description().split("。")[0].split("\n")[0]
            lines.append(f"  {tool.name():<14} {desc}")
        self._app.insert_system("\n".join(lines))

    def _list_files(self, root: Path) -> None:
        """/files：列项目文件（最多 30 个）。遍历规则与 list_files 工具共用 iter_project_files。"""
        shown = iter_project_files(root, max_depth=8, limit=200)[:30]
        self._app.insert_system(f"当前目录下 {len(shown)} 个文件：\n" + "\n".join("  " + f for f in shown))

    def _list_skills(self, skills: SkillRegistry) -> None:
        """/skills：列出已加载的技能，并说明它们从哪来。"""
        all_skills = skills.all()
        if not all_skills:
            self._app.insert_system("没有发现任何技能。\n" + _SKILLS_HINT)
            return
        lines = [f"已加载 {len(all_skills)} 个技能（模型会按需加载，不用你手动挑）："]
        for s in all_skills:
            desc = s.description.replace("\n", " ")
            if len(desc) > 46:
                desc = desc[:46] + "…"
            lines.append(f"  {s.name:<14} [{s.source}] {desc}")
        lines.append("提示：直接说「用 hello-mycode 技能打个招呼」，模型就会调用 use_skill 加载它。")
        self._app.insert_system("\n".join(lines))

    def _print_todo(self, tools: ToolRegistry) -> None:
        """/todo：渲染当前任务清单。

        TodoTool.print_items 直接 print 到 stdout，这里就地捕获成文本再上屏 ——
        不为 GUI 复制一份清单渲染逻辑。
        """
        todo = tools.find("todo")
        if not isinstance(todo, TodoTool):
            self._app.insert_system("todo 工具未加载。")
            return
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            todo.print_items()
        self._app.insert_system(buf.getvalue().rstrip())

    def _print_model(self) -> None:
        """/model：展示当前生效的模型配置，key 打码。按启动参数重新解析，避免误导排查。"""
        app = self._app
        try:
            resolved = llm_factory.resolve(app.args)
            text = (f"服务商 : {resolved.provider}\n接口地址: {resolved.endpoint}\n"
                    f"模型   : {resolved.model}\n密钥   : {llm_factory.mask(resolved.api_key)}\n"
                    f"协议   : {'Anthropic Messages' if resolved.anthropic else 'OpenAI 兼容'}\n"
                    f"语言   : {app.lang.label}")
            app.insert_system(text)
        except Exception as exc:  # noqa: BLE001
            app.insert_system("配置有误：" + _friendly_error(exc), "error")

    def _print_cost(self) -> None:
        """/cost：用量与耗时汇总。配了价格才显示金额，否则只报 token。"""
        session = self._app.bridge.session
        if session is None:
            return
        threshold = session.compress_threshold()
        current = session.context_chars()
        lines = ["模型用量 : " + costs.describe(session.total_usage),
                 f"上下文   : 约 {current} 字"
                 + ("（已超过自动压缩阈值）" if current > threshold else ""),
                 f"压缩阈值 : {threshold} 字（超过后在下一轮提问前自动压缩，任务中途不压，保缓存前缀）",
                 f"轮次     : {session.turns}",
                 f"耗时     : {session.elapsed_seconds()} 秒"]
        if not costs.configured():
            lines.append("提示     : 在 agent.toml 里配置 priceInputPerK / priceCacheHitPerK "
                         "/ priceOutputPerK 可显示金额")
        self._app.insert_system("\n".join(lines))

    def _compress(self) -> None:
        """/compress：手动压缩一次上下文（强制压到当前体量的一半）。

        压缩要调一次模型，走线程桥；太短时直接回话，不值得花那次调用。
        """
        session = self._app.bridge.session
        if session is None:
            return
        before = session.context_chars()
        if before < 200:
            self._app.insert_system(f"上下文太短了（约 {before} 字），没什么可压缩的。")
            return

        def work() -> str:
            stats = session.compress_now()
            if stats.removed_messages == 0:
                return "没有可以摘掉的历史对话了（只剩当前这一轮，压缩会丢掉你正在问的问题）。"
            return (f"已压缩：移除 {stats.removed_messages} 条消息（{stats.removed_rounds} 轮），"
                    f"约 {before} 字 → {session.context_chars()} 字\n"
                    + "摘要方式：" + ("模型生成" if stats.used_model else "机械兜底（模型摘要不可用）")
                    + "\n新的历史摘要：\n  " + session.summary.replace("\n", "\n  "))

        if not self._app.bridge.run_text_task(work):
            self._app.insert_system("当前有事在跑，等它结束再压缩。")

    def _undo(self, text: str) -> None:
        """/undo：把文件退回到上一个版本。带路径时恢复指定文件的最新快照。"""
        arg = text[len("/undo"):].strip()
        root = self._app.root_dir
        try:
            store = BackupStore(root)
            target = root / arg if arg else None
            restored = store.restore(target)
            self._app.insert_system("已恢复到上一个版本: " + _rel(root, restored), "ok")
        except RuntimeError:
            self._app.insert_system("没有可恢复的历史版本（还没改过文件，或快照已用完）。")
        except Exception as exc:  # noqa: BLE001
            self._app.insert_system("恢复失败: " + str(exc), "error")

    def _history(self) -> None:
        """/history：列出最近几次修改前的快照。"""
        root = self._app.root_dir
        try:
            snapshots: list[Snapshot] = BackupStore(root).recent(8)
            if not snapshots:
                self._app.insert_system("还没有历史版本。改过文件之后这里会有记录。")
                return
            lines = [f"最近 {len(snapshots)} 次修改前的快照："]
            for s in snapshots:
                lines.append(f"  {_pretty_stamp(s.stamp)}  {_rel(root, s.file):<40} {s.size:>6} 字节")
            lines.append("提示：/undo 恢复最近一次；/undo 路径 恢复指定文件")
            self._app.insert_system("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self._app.insert_system("读取历史失败: " + str(exc), "error")

    def _list_schedules(self, text: str) -> None:
        """/schedules：查看定时任务（持久化 + 会话内合并视图），子命令管理。

        /schedules cancel <id>   取消会话内任务
        /schedules pause|resume|delete <id>   管理持久化任务
        实现与终端版 _list_schedules 逐行对齐，输出通道换成聊天区。
        """
        from mycode import cron  # noqa: PLC0415 —— 延迟导入避免与 shell 成环
        app = self._app
        arg = text[len("/schedules"):].strip()
        parts = arg.split(maxsplit=1)
        verb = parts[0] if parts else ""
        task_id = parts[1].strip() if len(parts) > 1 else ""

        # ---- 管理子命令 ----
        if verb in ("cancel", "pause", "resume", "delete"):
            if not task_id:
                app.insert_system(f"用法：/schedules {verb} <任务id>")
                return
            if verb == "cancel":
                ok = app.scheduler.cancel(task_id)
                done = "已取消会话内定时任务"
            elif verb == "pause":
                ok = cron.set_enabled(app.root_dir, task_id, False)
                done = "已暂停持久化任务"
            elif verb == "resume":
                ok = cron.set_enabled(app.root_dir, task_id, True)
                done = "已恢复持久化任务"
            else:
                ok = cron.delete_task(app.root_dir, task_id)
                done = "已删除持久化任务"
            app.insert_system((done + " " + task_id + "。") if ok else f"没有找到任务 {task_id}。",
                              "ok" if ok else "dim")
            return

        # ---- 列表：持久化在前，会话内在后 ----
        lines: list[str] = []
        tasks = cron.load_tasks(app.root_dir)
        if tasks:
            lines.append("持久化定时任务（跨会话；系统级自动触发需 --cron-register）：")
            for t in tasks:
                state = "启用" if t.enabled else "停用"
                lines.append(f"  {t.id:<14} [{t.kind}] {state} · 下次 {t.next_run or '-'} · "
                             f"[{t.permission}] · 上次 {t.last_status or '-'} · {t.prompt[:26]}")
            lines.append("提示：/schedules pause|resume|delete <id> 管理；/reports 查看报告。")
        else:
            lines.append("没有持久化定时任务（schedule 工具 scope=persistent 的任务写在这里）。")

        session_tasks = app.scheduler.all()
        if session_tasks:
            lines.append(f"会话内定时任务（退出即失效，共 {len(session_tasks)} 个）：")
            now = time.monotonic()
            for t in session_tasks:
                if t.kind == "once":
                    left = max(0, t.next_fire - now)
                    kind = f"once 剩 {int(left // 60)}分{int(left % 60)}秒"
                else:
                    kind = f"interval 每 {t.interval_minutes:g} 分钟"
                lines.append(f"  {t.task_id:<5} {kind:<20} [{t.permission}] {t.prompt[:36]}")
            lines.append("提示：/schedules cancel <id> 取消某个会话内任务。")
        if not tasks and not session_tasks:
            lines.append("让模型用 schedule 工具创建，例如说「10 分钟后提醒我看看测试结果」。")
        app.insert_system("\n".join(lines))

    def _show_reports(self, text: str) -> None:
        """/reports [任务id]：查看定时任务报告（不带参数看全局最新一份）。

        报告是 Markdown 文件：桌面版按 Markdown 渲染进聊天区（终端版同款内容）。
        """
        from mycode import cron  # noqa: PLC0415 —— 延迟导入避免与 shell 成环
        app = self._app
        arg = text[len("/reports"):].strip()
        root = app.root_dir
        reports_root = root / ".mycode" / "schedules"
        latest: Path | None = None
        if arg:
            task_dir = reports_root / arg
            if task_dir.is_dir():
                files = sorted(task_dir.glob("*.md"))
                latest = files[-1] if files else None
        else:
            all_files = sorted(reports_root.glob("*/*.md"), key=lambda p: p.stat().st_mtime)
            latest = all_files[-1] if all_files else None
        cron.mark_reports_seen(root)
        if latest is None:
            app.insert_system("还没有任何定时任务报告。")
            return
        app.insert_report(latest.relative_to(root).as_posix(),
                          latest.read_text(encoding="utf-8"))

    @staticmethod
    def _suggest(cmd: str) -> str | None:
        """给打错的斜杠命令找最接近的候选（编辑距离），找不到返回 None。"""
        match = difflib.get_close_matches(cmd, KNOWN_COMMANDS, n=1, cutoff=0.6)
        return match[0] if match else None


# ---------------- 小工具 ----------------

def _rel(root: Path, file: Path) -> str:
    """相对路径展示，统一用正斜杠；跨盘等无法相对化的场景退回绝对路径。"""
    try:
        return file.relative_to(root).as_posix()
    except ValueError:
        return str(file)


def _pretty_stamp(stamp: str) -> str:
    """把 20261115-165812-123 变成 2026-11-15 16:58:12。"""
    if len(stamp) < 15:
        return stamp
    return (stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:8]
            + " " + stamp[9:11] + ":" + stamp[11:13] + ":" + stamp[13:15])
