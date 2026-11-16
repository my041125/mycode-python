"""
SKILL.md 的 frontmatter 解析器（对应 Java 版 Frontmatter）。

为什么自己写而不引 YAML 库：SKILL.md 的 frontmatter 实际上只用到了
「key: value」这一种形式，为一个 20 行的解析器引入第三方依赖不值当 ——
这也符合本项目「零第三方依赖」的底线。

支持的写法：
    name: docx
    description: "带引号的描述"
    allowed-tools: Read, Write, Edit
不支持的（按「整块跳过」处理，不影响其它字段）：
    多行折叠字符串（> 或 |）、嵌套对象、数组用 - 逐行写
"""

from __future__ import annotations


class Frontmatter:
    """一份 frontmatter 的解析结果：键值对 + 正文。"""

    def __init__(self, values: dict[str, str], body: str) -> None:
        self._values = values  # 键统一小写
        self._body = body      # frontmatter 之后的正文

    @classmethod
    def split(cls, text: str | None) -> "Frontmatter":
        """切分 frontmatter 与正文。

        没有 frontmatter 时，整篇都算正文（这样普通 markdown 也能当技能用）。
        """
        if not text:
            return cls({}, "")

        normalized = text.replace("\r\n", "\n")
        # 必须以 --- 开头，且后面有另一条 --- 作为结束
        if not normalized.startswith("---"):
            return cls({}, normalized)

        header_end = normalized.find("\n")
        if header_end < 0:
            return cls({}, normalized)

        close = normalized.find("\n---", header_end)
        if close < 0:
            return cls({}, normalized)

        yaml = normalized[header_end + 1:close]
        body_start = normalized.find("\n", close + 1)
        body = "" if body_start < 0 else normalized[body_start + 1:]

        values: dict[str, str] = {}
        for raw in yaml.split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # 嵌套行（以空格或 - 开头）不支持，直接跳过而不是把父键覆盖成垃圾值
            if raw.startswith((" ", "\t")) or line.startswith("- "):
                continue
            i = line.find(":")
            if i <= 0:
                continue
            key = line[:i].strip().lower()
            value = _unquote(line[i + 1:].strip())
            # 同一个键只取第一次出现
            if key not in values:
                values[key] = value
        return cls(values, body)

    def value(self, key: str) -> str | None:
        """取某个字段，不存在返回 None。"""
        return self._values.get(key.lower())

    @property
    def body(self) -> str:
        """frontmatter 之后的正文。"""
        return self._body


def _unquote(v: str) -> str:
    """去掉成对的引号。"""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v
