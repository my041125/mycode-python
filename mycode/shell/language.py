"""
回答语言（对应 Java 版 Language）。

分成两层来理解：
    1. 界面文字（横幅、提示、错误）—— 本身就是中文硬编码，不随这个枚举变化；
    2. 模型回答的语言 —— 由这个枚举决定，附加到 system 提示词里。

为什么要单独做：模型有个通病，工具返回英文（git 输出、编译器报错、英文注释）时，
它会顺着英文继续用英文回答。只在提示词里写「用中文」不够，得写清楚
「即使上下文是英文，回答也必须是中文」，否则中文客户端会出现中英混杂。

代码标识符、命令输出本身保持原样 —— 那是事实数据，翻译反而会误导。
"""

from __future__ import annotations

from enum import Enum

from mycode import config


class Language(Enum):
    """回答语言。hint 会拼进 system 提示词末尾（优先级最高的一段）。"""

    ZH = ("zh", "简体中文", (
        "语言要求（最高优先级）：\n"
        "- 你的所有回答一律使用简体中文；\n"
        "- 即使工具返回的是英文（git 输出、编译器报错、英文注释），也要用中文转述和解释；\n"
        "- 代码、命令、标识符、文件名保持原样，不要翻译；\n"
        "- 引用英文报错时可以先原文引用一句，紧接着用中文说明含义。"
    ))
    EN = ("en", "English", (
        "Language requirement (highest priority):\n"
        "- Always reply in English, even if tool output is in Chinese;\n"
        "- Keep code, commands, identifiers and file names unchanged;\n"
        "- Briefly quote an error in its original language, then explain in English."
    ))

    def __init__(self, code: str, label: str, hint: str) -> None:
        self.code = code          # 配置里用的值，例如 language="zh"
        self.label = label        # 人类可读的名字，横幅里展示
        self.hint = hint          # 追加到 system 提示词末尾的语言约束

    @classmethod
    def parse(cls, value: str | None) -> "Language":
        """解析语言，非法值回退到中文。

        客户端默认中文，所以任何解析失败都按中文处理，而不是报错中断启动。
        """
        if not value or not value.strip():
            return cls.ZH
        v = value.strip().lower()
        for language in cls:
            if language.code == v:
                return language
        return cls.ZH

    @classmethod
    def from_config(cls) -> "Language":
        """从配置文件读，缺省中文。"""
        return cls.parse(config.get("language"))

    @classmethod
    def from_args(cls, args: list[str]) -> "Language":
        """从命令行 --lang=xx 读，其次配置文件，最后中文。"""
        for a in args:
            if a.startswith("--lang="):
                return cls.parse(a[len("--lang="):])
        return cls.from_config()
