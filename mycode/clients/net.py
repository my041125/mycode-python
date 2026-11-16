"""
HTTP 公共件：URL 校验与连接建立（clients 层共用）。

用标准库 http.client 直连而不是 urllib.request.urlopen：
  1. 这正是 Java 版 java.net.http.HttpClient 的对应物，支持连接超时与连接复用；
  2. 请求地址来自用户自己的 agent.toml / 命令行参数 —— 本机自用工具，没有
     外部攻击者可控的输入源；支持 http://localhost（本地 Ollama）是明确功能需求，
     所以这里只做协议合法性校验，不封锁内网地址。
"""

from __future__ import annotations

import http.client
from urllib.parse import urlparse


def open_connection(url: str, timeout: int) -> tuple[http.client.HTTPConnection, str]:
    """按 URL 建立连接，返回 (连接, 请求路径)。

    scheme 只接受 http / https（防手滑写成 ftp:// 或纯文本）；
    https 走 HTTPSConnection（默认上下文，证书照常校验），http 用于本地服务。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"endpoint 必须是 http(s) 地址: {url}")
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return conn, path


def read_body(resp) -> str:
    """把响应体读成字符串；读失败给占位文本，不让错误路径再炸出新异常。"""
    try:
        return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return "(无法读取错误响应)"
