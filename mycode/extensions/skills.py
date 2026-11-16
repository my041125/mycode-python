"""
Agent Skills：目录扫描注册表 + use_skill 渐进加载工具。

兼容 Claude Code 的 Agent Skills 目录约定，所以直接读 ~/.claude/skills
就能用现成技能（docx / xlsx / pptx 等）：
    <项目>/.mycode/skills/   项目私有技能（优先级最高）
    <项目>/.claude/skills/   与 Claude Code 共用的项目技能
    <用户>/.claude/skills/   个人技能（Claude Code 装的那些默认放这）
    <mycode.home>/skills     随客户端分发（MYCODE_HOME 环境变量，缺省为项目根）

同名技能只保留优先级最高的那份 —— 项目级覆盖个人级，这是 Claude Code 的规则。

渐进披露：启动时只把「名字 + 描述」注入 system（每个约 30~120 字），
模型判断相关时才调 use_skill 把正文读进来。这样装十几个技能也不会撑爆上下文。

技能里的脚本由模型经 run_command 执行 —— 那条路径有三道安全保险
（CommandPolicy / PermissionHook / 超时强杀），技能永远拿不到免审批通道。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mycode import config
from mycode.kernel.tool import Tool
from mycode.tools.code_file import CodeFile
from mycode.extensions.frontmatter import Frontmatter

# 描述注入 system 时的截断长度。太长会让清单本身变成负担
DESC_LIMIT = 120

# 单个技能附带文件的展示上限
FILE_LIMIT = 24

# 内置技能目录的来源标签与查找顺序（先扫到的优先）
_SOURCES = (
    (".mycode", "skills", "项目"),
    (".claude", "skills", "项目(Claude)"),
)


@dataclass(frozen=True, slots=True)
class Skill:
    """一个技能（沿用 Claude Code 的 Agent Skills 格式）。

    design 上只保留真正需要的字段，其余（model / effort / maxTurns）解析但不使用 ——
    本客户端没有多模型路由，也没法按技能切换 effort。
    """

    name: str              # 技能名，唯一标识，也是 use_skill 的参数
    description: str       # 描述，启动时注入 system 供模型判断相关性（会被截断）
    dir: Path              # 技能目录，模型跑脚本时需要它来拼路径
    body: str              # SKILL.md 的正文（不含 frontmatter），激活时才加载
    allowed_tools: tuple[str, ...]  # 声明的工具白名单，用于提示可用范围；空表示不限制
    source: str            # 来源标签，例如「个人」「项目」，让用户知道技能从哪来


class SkillRegistry:
    """技能注册表：扫描磁盘上的 SKILL.md，提供「清单」和「按需加载」。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}  # 保持扫描顺序，便于展示

    @classmethod
    def discover(cls, project_root: Path) -> "SkillRegistry":
        """扫描所有位置，返回注册表。

        目录不存在就返回空表，绝不抛异常（技能是可选能力，不该阻断启动）。
        """
        registry = cls()
        root = Path(project_root).resolve()

        for sub, leaf, source in _SOURCES:
            registry._scan(root / sub / leaf, source)

        # 个人技能：Claude Code 装的那些默认在 ~/.claude/skills
        registry._scan(Path.home() / ".claude" / "skills", "个人")

        # 随客户端分发的内置技能：MYCODE_HOME 优先，缺省为项目根（包的上一级）
        install = os.environ.get("MYCODE_HOME") or str(root)
        registry._scan(Path(install) / "skills", "内置")

        registry._apply_filter(config.get("skills"))
        return registry

    def _scan(self, directory: Path, source: str) -> None:
        """扫描一个目录下的「子目录/SKILL.md」；已存在同名技能则跳过（先扫到的优先）。"""
        if not directory.is_dir():
            return
        try:
            candidates = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return  # 目录读不了就算了
        for sub in candidates:
            skill_file = sub / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                skill = self._parse(sub, source)
                if skill is None:
                    continue
                # 已存在就跳过而不是覆盖 —— 扫描顺序就是优先级顺序
                if skill.name not in self._skills:
                    self._skills[skill.name] = skill
            except Exception:
                pass  # 单个技能格式不对不影响其它技能

    @staticmethod
    def _parse(directory: Path, source: str) -> Skill | None:
        """解析一个技能目录；缺 SKILL.md 或内容为空返回 None。"""
        file = directory / "SKILL.md"
        # 用 CodeFile 读，顺带支持 GBK 编码的 SKILL.md
        text = CodeFile.read(file).text
        if not text.strip():
            return None

        fm = Frontmatter.split(text)
        name = fm.value("name")
        if not name or not name.strip():
            # 没写 name 就用目录名，和 Claude Code 的行为一致
            name = directory.name
        desc = fm.value("description")
        if not desc or not desc.strip():
            desc = "(未提供描述)"

        allowed: list[str] = []
        tools = fm.value("allowed-tools")
        if tools and tools.strip():
            allowed = [t.strip() for t in tools.split(",") if t.strip()]
        return Skill(name.strip(), desc.strip(), directory, fm.body.strip(),
                     tuple(allowed), source)

    def _apply_filter(self, allow_list: str | None) -> None:
        """按配置里的白名单过滤，例如 skills = ["docx", "xlsx"]。"""
        if not allow_list or not allow_list.strip():
            return
        keep = [s.strip() for s in allow_list.split(",") if s.strip()]
        if not keep:
            return
        self._skills = {name: skill for name, skill in self._skills.items()
                        if name in keep}

    def all(self) -> list[Skill]:
        """全部技能，按扫描顺序。"""
        return list(self._skills.values())

    def find(self, name: str | None) -> Skill | None:
        """按名字查找；找不到返回 None。"""
        if not name:
            return None
        return self._skills.get(name.strip())

    def size(self) -> int:
        """技能个数。"""
        return len(self._skills)

    def catalog(self) -> str:
        """生成注入 system 的技能清单。

        只放名字和截断后的描述，正文留到 use_skill 时再加载。
        """
        if not self._skills:
            return ""
        lines = ["\n\n可用技能（处理对应文件类型时先用 use_skill 加载完整步骤）："]
        for skill in self._skills.values():
            desc = skill.description.replace("\n", " ")
            if len(desc) > DESC_LIMIT:
                desc = desc[:DESC_LIMIT] + "…"
            lines.append(f"- {skill.name}：{desc}")
        lines.append("当用户的操作对象是 Word/Excel/PPT/PDF 等文件时，必须先 use_skill "
                     "取回该技能的完整说明再动手。")
        return "\n".join(lines)

    @staticmethod
    def bundled_files(skill: Skill) -> list[str]:
        """技能目录下的文件清单，供模型知道有什么脚本可用。

        只列相对路径，最多 FILE_LIMIT 条；缓存/隐藏目录不列。
        """
        out: list[str] = []
        base = skill.dir
        stack = [(base, 0)]
        while stack and len(out) < FILE_LIMIT:
            current, depth = stack.pop()
            if depth > 3:
                continue
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if child.name not in (".git", "__pycache__"):
                        stack.append((child, depth + 1))
                elif child.is_file():
                    if child.name == "SKILL.md":
                        continue
                    out.append(child.relative_to(base).as_posix())
                    if len(out) >= FILE_LIMIT:
                        break
        return out


class SkillTool(Tool):
    """技能加载工具 use_skill。

    为什么做成「工具」而不是启动时把技能全塞进 system：
    一个技能正文动辄十几 KB（pptx 那份 238 行），全塞进去每次请求都要付费；
    做成工具后，只有真正用到时才加载，模型自己判断「这个话题跟 docx 有关」。
    这就是 Agent Skills 规范里的「渐进披露」。
    """

    # Claude Code 的工具名 -> 本客户端的工具名，方便模型换用
    _TOOL_MAPPING = {
        "read": "read_code",
        "write": "edit_code",
        "edit": "edit_code",
        "bash": "run_command",
        "grep": "list_files",
        "glob": "list_files",
        "webfetch": "（二期未接入）",
        "websearch": "web_search",
        "task": "（不支持）",
    }

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def name(self) -> str:
        return "use_skill"

    def description(self) -> str:
        return ("加载指定技能的完整说明。处理 Word/Excel/PPT/PDF 等专业文件时必须先调用它，"
                "拿到分步骤的做法和附带脚本后再动手，不要凭常识直接写代码。"
                "参数 name 取技能清单里列出的名字（例如 docx、xlsx、pptx）。")

    def parameters(self) -> dict:
        return Tool.schema({
            "name": Tool.str_param("技能名，例如 docx"),
            "args": Tool.str_param("可选参数，会替换技能正文里的 $ARGUMENTS 占位符"),
        }, "name")

    def run(self, args: dict) -> str:
        """加载技能：正文 + 工具映射 + 附带文件清单，一次拿全。"""
        skill_name = str(args.get("name") or "").strip()
        skill = self._registry.find(skill_name)
        if skill is None:
            raise ValueError(f"没有名为「{skill_name}」的技能。可用技能：{self._names()}")

        body = skill.body
        # $ARGUMENTS 是 Claude Code 的约定，把用户给的参数填进去
        extra = args.get("args")
        if extra is not None and str(extra).strip():
            body = body.replace("$ARGUMENTS", str(extra).strip())
        body = self._rewrite_dynamic_commands(body)

        lines = [f"【技能：{skill.name}】来源：{skill.source}",
                 "技能目录：" + skill.dir.as_posix()]
        if skill.allowed_tools:
            lines.append("原技能声明的工具：" + ", ".join(skill.allowed_tools)
                         + "（本客户端对应：" + self._map_tools(skill.allowed_tools) + "）")
        lines.append("")
        lines.append(body)
        lines.append("")

        files = SkillRegistry.bundled_files(skill)
        if files:
            lines.append("附带的文件（路径相对技能目录）：")
            lines.extend(f"- {f}" for f in files)
            lines.append("需要跑脚本时用 run_command，路径请用上面的「技能目录」拼绝对路径。")
        return "\n".join(lines)

    @staticmethod
    def _rewrite_dynamic_commands(body: str) -> str:
        """把 `!命令` 这类动态注入改写成说明文字，而不是直接执行。

        Claude Code 会自动执行这些命令并把输出内联。我们不这么做，原因很简单：
        技能来自外部，等于一段来源不明的脚本，自动执行就是给了它任意命令执行权。
        改成提示后，模型会自己决定要不要调 run_command —— 那条路径有权限确认把关。
        """
        out: list[str] = []
        for line in body.split("\n"):
            trimmed = line.strip()
            if trimmed.startswith("!") and len(trimmed) > 1:
                out.append("（此处技能原本会自动执行：" + trimmed[1:].strip()
                           + "；本客户端不自动执行，需要时自行调用 run_command）")
            else:
                out.append(line)
        return "\n".join(out)

    @classmethod
    def _map_tools(cls, claude_tools: tuple[str, ...]) -> str:
        """把声明的工具白名单逐个映射成本客户端的工具名，拼成一行。"""
        return ", ".join(
            f"{t}→{cls._TOOL_MAPPING.get(t.lower(), '未映射')}" for t in claude_tools)

    def _names(self) -> str:
        """可用技能名，拼成一行用于报错提示。"""
        names = [s.name for s in self._registry.all()]
        return "，".join(names) if names else "（当前没有任何技能）"
