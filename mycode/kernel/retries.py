"""
模型调用的瞬时故障重试策略（对应 Java 版 Retries）。

放在内核层而不是 clients 层：主循环 Agent 负责执行重试，客户端只负责抛出
带状态码的异常 —— 策略跟着使用者走，同时避免 kernel → clients 的反向依赖。

长任务跑十几步，中途一次 429 或网络抖动就把整轮废掉太亏 ——
前面烧掉的上下文全在本地，重试一次通常就过去了。各家 agent 的标配能力。

分类原则：只有「等等就好」的错误才重试。401/403/400 是配置或参数错误，
重试八次也是白等，必须立刻如实上报让用户去改配置。
"""

from __future__ import annotations


def transient_failure(exc: BaseException) -> bool:
    """判断一次模型调用失败是否值得重试。

    两个客户端抛的都是 RuntimeError，状态码在消息里（"HTTP 429 -> ..."），
    超时和断连是 socket 异常，消息也有固定特征，
    所以按消息分类就够了，不需要引入异常类型层级。
    """
    message = str(exc).lower()

    # 永久错误：key 错、没权限、参数不对 —— 重试没有意义
    if ("401" in message or "403" in message or "400" in message
            or "unauthorized" in message or "forbidden" in message
            or "invalid api key" in message or "invalid_request" in message):
        return False
    # 限流与服务端临时故障
    if ("429" in message or "rate limit" in message
            or "500" in message or "502" in message or "503" in message or "504" in message
            or "server error" in message or "overloaded" in message):
        return True
    # 网络类：超时、断连、DNS —— 多半是瞬时的
    return ("timeout" in message or "timed out" in message
            or "connection" in message or "unknownhost" in message
            or "getaddrinfo failed" in message
            or "reset" in message or "eof" in message or "broken pipe" in message)


def backoff_millis(retry_index: int) -> int:
    """第 n 次重试（从 0 计）前等多久：1s → 2s → 4s，指数退避。"""
    return 1000 << min(retry_index, 4)
