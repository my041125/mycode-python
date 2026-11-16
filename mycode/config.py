"""
配置读取（对应 Java 版 AgentConfig）：读项目根目录的 agent.toml（可选）。

优先级从高到低：命令行参数 > 环境变量 > agent.toml > 内置预设。
之所以保留文件这一层，是因为 Windows 下改环境变量要重开终端，调试时很烦。

与 Java 版的差异：原版用自解析的 key=value 格式（agent.properties），
Python 版改用 TOML（3.11 起标准库自带 tomllib），写法见 agent.example.toml，
键名与 Java 版完全一致，方便两边对照学习。

模块导入时读一次文件，之后只读不写 —— 省掉每次取配置都碰磁盘。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

def _candidate_paths() -> list[Path]:
    """配置文件的查找顺序：运行目录 → MYCODE_HOME → 安装目录（包的上一级）。"""
    candidates: list[Path] = [Path.cwd() / "agent.toml"]
    home = os.environ.get("MYCODE_HOME")
    if home:
        candidates.append(Path(home) / "agent.toml")
    # 包目录的上一级 = 项目根（python -m mycode 从项目根启动时就是它）
    candidates.append(Path(__file__).resolve().parent.parent / "agent.toml")
    return candidates


def _load() -> dict:
    """解析配置文件。任何异常都不外抛：配置文件读不了就当没有，
    不能让一个格式写错的文本文件阻断整个程序启动。"""
    for path in _candidate_paths():
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as fh:
                return tomllib.load(fh)
        except Exception:
            return {}  # 读失败就返回空表，上层会退回到环境变量和内置预设
    return {}


# 配置文件解析结果：模块导入时读一次，之后全程只读
_FILE: dict = _load()


def has_file() -> bool:
    """是否成功读到了配置文件 —— 只用于 --config 自检时提示来源。"""
    return bool(_FILE)


def get(key: str) -> str | None:
    """取配置项（字符串形态）。

    没配或配了空串都返回 None，方便调用方用「是否为 None」判断。
    TOML 里的标量统一转成字符串（True → "true"），与 Java 版的字符串语义对齐。
    """
    value = _FILE.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text or None


def get_bool(key: str, default: bool) -> bool:
    """取布尔配置。兼容 TOML 布尔与 Java 版的 on/off 写法。"""
    value = _FILE.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "on", "1", "yes"):
        return True
    if text in ("false", "off", "0", "no", ""):
        return False
    return default


def get_int(key: str, default: int) -> int:
    """取整数配置，解析失败就用默认值，不让一个写错的值导致启动失败。"""
    value = _FILE.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    """取小数配置，解析失败同样退回默认值。"""
    value = _FILE.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
