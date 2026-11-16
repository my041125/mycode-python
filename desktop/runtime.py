"""装配：把模型、工具、权限、技能拼成一个可运行的 agent（桌面版的组合根）。

对照终端版 shell.Shell 的装配段（_build_runtime / _build_tools / _build_agent /
_open_session / _demo_model），差异只有三处：
    1. 确认器来自界面（bridge.confirm 跨线程弹框），不是终端按键；
    2. 不用 factory.create() —— 它会 print 配置摘要，GUI 把这行信息显示在头部；
    3. system_prompt 传 surface="桌面窗口"，身份句与输出条目随界面形态措辞。
另外提供 save_api_key：首启弹窗收集的密钥写进 agent.toml（行级替换，保留注释）。
工具清单、技能扫描、权限模式、--continue 恢复全部与终端版同一套。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mycode.clients.anthropic_client import AnthropicClient
from mycode.clients import factory as llm_factory
from mycode.clients.factory import Resolved
from mycode.clients.openai_client import OpenAiCompatibleClient
from mycode.clients.scripted import ScriptedLlmClient
from mycode.extensions.scheduler import ScheduleTool, SessionScheduler
from mycode.extensions.skills import SkillRegistry, SkillTool
from mycode.kernel.agent import Agent
from mycode.kernel.hooks import Hook
from mycode.kernel.llm import LlmClient
from mycode.kernel.permission import PermissionHook, PermissionMode
from mycode.kernel.registry import ToolRegistry
from mycode.kernel.session import ChatSession
from mycode.shell.language import Language
from mycode.shell.session_store import SessionStore
from mycode.shell.shell import env_block, system_prompt
from mycode.tools.code_edit import CodeEditTool
from mycode.tools.code_tools import list_tool, read_tool
from mycode.tools.command import CommandTool
from mycode.tools.todo import TodoTool
from mycode.tools.web_search import BochaSearchProvider, TavilySearchProvider, WebSearchTool

from desktop.hook import PlainPresenter


@dataclass(frozen=True, slots=True)
class Runtime:
    """运行期依赖。/lang 切换语言要重建 agent，所以这些引用得留着。"""

    llm: LlmClient
    tools: ToolRegistry
    mode: PermissionMode
    permission: PermissionHook
    skills: SkillRegistry
    resolved: Resolved   # /model 展示与头部信息栏用，不用再解析一遍


def build_runtime(args: list[str], root: Path, confirmer: PermissionHook.Confirmer,
                  scheduler: SessionScheduler,
                  persistent_creator: Callable[[dict], str]) -> Runtime:
    """装配运行期依赖：模型、工具、权限、技能、定时任务。

    confirmer 是界面的确认器（bridge.confirm）。桌面窗口永远是交互环境，
    不需要终端版那套 isatty 检测。
    """
    resolved = llm_factory.resolve(args)
    llm = _create_client(resolved) if "--demo" not in args else demo_model()
    mode = PermissionMode.parse(llm_factory.pick(args, "--permission", None, None))
    # 技能要先扫描：use_skill 的注册与 system 提示词都依赖它
    skills = SkillRegistry.discover(root)
    tools = build_tools(root, skills, scheduler, persistent_creator)
    permission = PermissionHook.for_mode(mode, confirmer)
    return Runtime(llm, tools, mode, permission, skills, resolved)


def _create_client(resolved: Resolved) -> LlmClient:
    """按解析结果直接构造客户端（等价 factory.create，但不 print 配置摘要）。"""
    if resolved.anthropic:
        return AnthropicClient(resolved.api_key, resolved.model, resolved.endpoint, resolved.max_tokens)
    return OpenAiCompatibleClient(resolved.endpoint, resolved.api_key, resolved.model)


def build_tools(root: Path, skills: SkillRegistry, scheduler: SessionScheduler,
                persistent_creator: Callable[[dict], str]) -> ToolRegistry:
    """组装工具：代码三件套 + 命令执行 + 待办清单 + schedule + use_skill + 可选联网搜索。

    与终端版完全一致；TodoTool 的 presenter 换成 GUI 等价物（着色交给渲染层），
    ScheduleTool 挂 app 提供的会话调度器与持久化创建回调。
    搜索工具只在配了密钥时才挂上 —— 缺一个可选功能不该让程序启动失败。
    """
    tools = ToolRegistry().register(
        list_tool(root),        # 定位文件
        read_tool(root),        # 读取原文，改代码的前提
        CodeEditTool(root),     # 精准修改 + 校验 + 快照
        CommandTool(root),      # 编译、跑测试，闭环的关键
        TodoTool(presenter=PlainPresenter()),  # 长任务的可视化清单
        # 定时任务：scope=session 走会话调度器，scope=persistent 走持久化文件
        ScheduleTool(scheduler, persistent_creator=persistent_creator),
    )
    # 有技能才注册 use_skill，没技能时少一个工具少一份提示词开销
    if skills.size() > 0:
        tools.register(SkillTool(skills))
    bocha = os.environ.get("BOCHA_API_KEY")
    tavily = os.environ.get("TAVILY_API_KEY")
    if bocha:
        tools.register(WebSearchTool(BochaSearchProvider(bocha), 3))
    elif tavily:
        tools.register(WebSearchTool(TavilySearchProvider(tavily), 3))
    return tools


def build_agent(rt: Runtime, root: Path, lang: Language, gui_hooks: list[Hook]) -> Agent:
    """组装 agent：模型 + 工具 + 钩子 + 语言约束。

    钩子顺序有讲究：GuiHook 先展示「它要调用什么」，PermissionHook 再决定放不放行，
    这样用户在被询问之前就知道这行命令是什么来路。
    """
    return Agent(
        "mycode",
        llm=rt.llm,
        tools=rt.tools,
        # 身份句与输出条目按「桌面窗口」措辞；运行环境紧跟其后
        system_prompt=system_prompt("桌面窗口") + env_block(root)
                     + rt.skills.catalog() + "\n" + lang.hint,
        hooks=[*gui_hooks, rt.permission],
    )


def build_scheduled_agent(rt: Runtime, root: Path, lang: Language,
                          gui_hooks: list[Hook], permission: PermissionHook) -> Agent:
    """定时任务用的独立 agent：同一份 system 与工具，权限换成任务自带的非交互档。

    两个刻意（与终端版 _run_scheduled 一致）：
    不带主对话的记忆 —— 定时任务语义上是独立请求；
    不带交互确认器 —— 后台线程弹确认框会跟用户操作打架，
    需要确认的命令在定时执行中自动拒绝并如实写进结果。
    """
    return Agent(
        "mycode",
        llm=rt.llm,
        tools=rt.tools,
        system_prompt=system_prompt("桌面窗口") + env_block(root)
                     + rt.skills.catalog() + "\n" + lang.hint,
        hooks=[*gui_hooks, permission],
    )


def open_session(agent: Agent, store: SessionStore, cont: bool) -> tuple[ChatSession, str]:
    """开会话：--continue 就尝试恢复最近一份存档，否则全新开始。

    返回 (会话, 提示文案)。无论恢复成功与否都给一行说明 ——
    记忆被替换这种事必须让用户知道。
    """
    if not cont:
        return ChatSession(agent), ""
    restored = store.load_latest()
    if restored is None:
        return ChatSession(agent), "没有找到可恢复的会话，已开始新会话。"
    notice = (f"↻ 已恢复上次会话（{restored.turns} 轮，上下文约 "
              f"{restored.ctx.estimate_tokens()} 字）")
    return ChatSession(agent, restored.ctx, restored.turns, restored.usage), notice


# 匹配 agent.toml 里现存的 apiKey 行（行级替换用，保留缩进与注释）
_API_KEY_LINE_RE = re.compile(r"^\s*apiKey\s*=")


def save_api_key(root: Path, key: str) -> str:
    """把首启弹窗里填的 apiKey 写进 agent.toml（没有就创建），返回结果说明文案。

    查找顺序与 mycode/config.py 一致：运行目录 → MYCODE_HOME；
    两处都没有文件时在运行目录新建 —— config 的查找顺序里它排最前，
    下次启动就会被读到，弹窗自然不再出现。
    写入只做行级替换 / 追加：文件里其余内容（含注释）原样保留。
    """
    candidates = [root / "agent.toml"]
    home = os.environ.get("MYCODE_HOME")
    if home:
        candidates.append(Path(home) / "agent.toml")
    target = next((p for p in candidates if p.is_file()), candidates[0])
    # TOML 基本串的最小转义：反斜杠与双引号（密钥通常是 token，实际命不中，防一手）
    value_line = 'apiKey = "' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:
        if target.is_file():
            lines = target.read_text(encoding="utf-8").splitlines()
            replaced = False
            for i, old in enumerate(lines):
                if _API_KEY_LINE_RE.match(old):
                    lines[i] = value_line
                    replaced = True
                    break
            if not replaced:
                lines.append(value_line)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            target.write_text(
                "# mycode 配置（桌面版生成；键名与 agent.example.toml 一致）\n"
                + value_line + "\n", encoding="utf-8")
        return "已保存到 " + target.name + "（下次启动直接读取，不再弹窗）。"
    except Exception as exc:  # noqa: BLE001 —— 保存失败不影响本会话（密钥已在启动参数里生效）
        return "保存到 agent.toml 失败（本次会话仍然生效）：" + str(exc)


def demo_model() -> ScriptedLlmClient:
    """离线演示用的假模型：列目录 → 读文件 → 跑一条只读命令，然后给结论。

    与终端版同一套脚本；回答里的启动指引换成桌面版的入口。
    """
    return ScriptedLlmClient.of(
        [
            ScriptedLlmClient.call("call_1", "list_files",
                                   {"dir": "mycode/kernel", "keyword": "agent"}),
            ScriptedLlmClient.call("call_2", "read_code",
                                   {"path": "samples/InvoiceService.py"}),
            ScriptedLlmClient.call("call_3", "run_command",
                                   {"command": "python --version"}),
        ],
        lambda ctx: (
            "这是**离线演示模式**的回答：列出了目录、读过一个文件，"
            "并执行了一条只读命令 —— *没有真的调用模型*。\n\n"
            "要体验真实效果，请在 `agent.toml` 里填好 `apiKey`，然后运行：\n\n"
            "```\n"
            "python -m desktop\n"
            "```\n\n"
            "刚才这条链路自检了三件事：\n"
            "1. 工具调用与观察回填 —— 正常；\n"
            "2. 权限闸门（只读命令直接放行）—— 正常；\n"
            "3. 流式输出与 **Markdown 实时渲染** —— 你现在看到的就是。\n\n"
            "下面是你刚才实际执行的最后一条工具返回，可以用它确认链路是否正常：\n\n"
            "```\n"
            + ScriptedLlmClient.last_observation(ctx) + "\n"
            + "```"
        ),
    )
