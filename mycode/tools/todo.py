"""
任务清单工具（对应 Java 版 TodoTool）：把当前任务的计划写成结构化清单，做一项更新一项。

Claude Code 的 TodoWrite、Codex 的 update_plan 都是同一个东西 ——
长任务的「骨架」：模型先把计划落成清单，用户随时看到进度，
模型自己也靠它对齐「现在做到哪了、还差什么」。

清单由工具自己渲染到终端（和 ConsoleHook 一个路数，界面对象注入），
返回给模型的只有一句确认，不占观察。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from mycode.kernel.hooks import ConsolePresenter
from mycode.kernel.tool import Tool


@dataclass(frozen=True, slots=True)
class Item:
    """一项任务：内容 + 状态。"""

    content: str
    status: str  # pending（待办）/ in_progress（进行中）/ completed（已完成）


class TodoTool(Tool):
    """todo 工具。当前清单全量替换，不提供单项增删 ——
    小模型对「整体重写」的理解远好于补丁式更新。"""

    def __init__(self, presenter: ConsolePresenter | None = None) -> None:
        self._items: list[Item] = []
        self._presenter = presenter  # 终端渲染对象（shell 层注入）；None 时输出纯文本
        self._lock = threading.Lock()

    def name(self) -> str:
        return "todo"

    def description(self) -> str:
        return ("维护当前任务的可视化清单。接到多步任务先写出全部步骤（全为 pending），"
                "开始某一步前把该步标为 in_progress，做完标为 completed。"
                "每次调用传入完整清单（全量替换，不是追加）。"
                "一两次工具调用就能完成的小事不要用。")

    def parameters(self) -> dict:
        """数组型 schema 要手写：Tool.str_param/num_param 便捷方法只覆盖标量。"""
        item_schema = {
            "type": "object",
            "properties": {
                "content": Tool.str_param("这一步要做什么，一句话"),
                "status": Tool.str_param("pending（待办）/ in_progress（进行中）/ completed（已完成）"),
            },
            "required": ["content", "status"],
        }
        return Tool.schema({
            "todos": {
                "type": "array",
                "items": item_schema,
                "description": "完整清单，每次全量替换",
            },
        }, "todos")

    def run(self, args: dict) -> str:
        """全量替换清单，渲染到终端，返回一句确认给模型。"""
        parsed: list[Item] = []
        for entry in args.get("todos") or []:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            parsed.append(Item(content, _normalize_status(entry.get("status"))))

        with self._lock:
            self._items = parsed
        self.print_items()

        done = sum(1 for i in self._items if i.status == "completed")
        if not self._items:
            return "清单已清空"
        return f"OK: 清单已更新（共 {len(self._items)} 项，已完成 {done}）"

    def print_items(self) -> None:
        """渲染当前清单到终端。/todo 命令和工具执行共用这一个入口。"""
        dim = (lambda s: self._presenter.dim(s)) if self._presenter else (lambda s: s)
        accent = (lambda s: self._presenter.accent(s)) if self._presenter else (lambda s: s)
        with self._lock:
            items = list(self._items)
        print("  " + dim("任务清单："))
        if not items:
            print("  " + dim("（空）"))
            return
        for item in items:
            if item.status == "completed":
                mark, text = dim("☑"), dim(item.content)
            elif item.status == "in_progress":
                mark, text = accent("▸"), item.content
            else:
                mark, text = "☐", item.content
            print("  " + mark + " " + text)


def _normalize_status(raw) -> str:
    """宽容解析状态：中英文、大小写都认，认不出按待办处理。"""
    s = str(raw or "").strip().lower()
    if "progress" in s or "进行" in s:
        return "in_progress"
    if "complet" in s or "done" in s or "完成" in s or "已" in s:
        return "completed"
    return "pending"
