"""
成本估算（对应 Java 版 Costs）。

为什么不内置各家价格表：模型价格变动非常频繁，写死在代码里迟早变成错误信息。
这里改成从 agent.toml 读可选配置：

    priceInputPerK     = 0.002   每千输入 token 的价格（元，未命中缓存）
    priceCacheHitPerK  = 0.0002  每千「命中缓存」输入 token 的价格（元）
    priceOutputPerK    = 0.003   每千输出 token 的价格（元）

DeepSeek 的前缀缓存自动生效：历史上下文重复发送时命中部分按约 1/10 计费，
所以输入实际按两档算 —— 命中部分用 priceCacheHitPerK，未命中部分用 priceInputPerK。
不配价格就只显示 token 数，不显示金额 —— 宁可没有，也不给一个错的数字。
"""

from __future__ import annotations

from mycode import config
from mycode.kernel.types import Usage


def _price(key: str) -> float:
    """读一项价格配置；未配置或非法时按 0 处理。"""
    value = config.get_float(key, 0.0)
    return max(0.0, value)


def _price_input() -> float:
    """每千输入 token 的价格（未命中缓存），单位元。"""
    return _price("priceInputPerK")


def _price_cache_hit() -> float:
    """每千「命中缓存」输入 token 的价格，单位元。"""
    return _price("priceCacheHitPerK")


def _price_output() -> float:
    """每千输出 token 的价格，单位元。"""
    return _price("priceOutputPerK")


def configured() -> bool:
    """是否配置了价格。没配置时界面只显示 token。"""
    return _price_input() > 0 or _price_output() > 0


def _cached_of(usage: Usage) -> int:
    """输入里命中缓存的部分。个别服务商会报出比 prompt 还大的数，夹一下防负数。"""
    return min(max(usage.cache_read_tokens, 0), usage.prompt_tokens)


def money(usage: Usage | None) -> str | None:
    """估算费用，形如 "0.0123 元"；未配置价格时返回 None。

    命中部分若没单独配价格，就按普通输入价算 —— 宁可把费用报高，也不报偏低的假优惠。
    """
    if usage is None or not configured():
        return None
    cached = _cached_of(usage)
    hit_price = _price_cache_hit() if _price_cache_hit() > 0 else _price_input()
    yuan = ((usage.prompt_tokens - cached) / 1000.0 * _price_input()
            + cached / 1000.0 * hit_price
            + usage.completion_tokens / 1000.0 * _price_output())
    return f"{yuan:.4f} 元"


def describe(usage: Usage | None) -> str:
    """生成一段可读的用量摘要，例如：

        1234 输入 + 567 输出 = 1801 tokens · 缓存命中 900/1234（73%），省 0.0016 元（约 0.0123 元）

    缓存命中率只在有过命中时才显示；省钱额需要配置价格，否则只报比例不报钱。
    """
    if usage is None:
        return "无用量数据"
    parts = [f"{usage.prompt_tokens} 输入 + {usage.completion_tokens} 输出 = {usage.total()} tokens"]
    line = _cache_line(usage)
    if line:
        parts.append(line)
    amount = money(usage)
    if amount:
        parts.append(f"（约 {amount}）")
    return " · ".join(parts)


def _cache_line(usage: Usage) -> str:
    """缓存命中摘要；服务商不报告或一次都没命中过时返回空串。"""
    hit = _cached_of(usage)
    if hit <= 0:
        return ""
    prompt = usage.prompt_tokens
    pct = 0 if prompt <= 0 else round(hit * 100.0 / prompt)
    text = f"缓存命中 {hit}/{prompt}（{pct}%）"
    saved = _saved_money(hit)
    return text + (f"，省 {saved}" if saved else "")


def _saved_money(hit_tokens: int) -> str | None:
    """这部分命中 token 相比全价省了多少钱；没配全两档价格时返回 None（不瞎猜）。"""
    if _price_input() <= 0 or _price_cache_hit() <= 0:
        return None
    saved = hit_tokens / 1000.0 * (_price_input() - _price_cache_hit())
    return f"{max(0.0, saved):.4f} 元"
