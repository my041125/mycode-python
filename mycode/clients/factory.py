"""
模型客户端工厂（对应 Java 版 LlmFactory）：负责回答「用哪家、地址是什么、密钥从哪来」。

配置优先级（从高到低）：
    1. 命令行参数   --provider= --base-url= --api-key= --model= --protocol=
    2. 环境变量     LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / MODEL
    3. 配置文件     agent.toml（项目根目录，见 agent.example.toml）
    4. 内置预设     见下面的 Provider 枚举

最常见的用法：在 agent.toml 里填三行，然后 python -m mycode "问题" 就行了。
"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass
from enum import Enum

from mycode import config
from mycode.clients.anthropic_client import AnthropicClient
from mycode.clients.openai_client import OpenAiCompatibleClient
from mycode.kernel.llm import LlmClient


class Provider(Enum):
    """内置服务商预设：endpoint 是兼容端点，key_env 是该家专用的密钥环境变量。"""

    #                  命令行名      endpoint                                                                    密钥环境变量           默认模型
    DASHSCOPE = ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "DASHSCOPE_API_KEY", "qwen3-max", False)
    OPENAI = ("openai", "https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "gpt-4.1", False)
    DEEPSEEK = ("deepseek", "https://api.deepseek.com/v1/chat/completions", "DEEPSEEK_API_KEY", "deepseek-chat", False)
    MOONSHOT = ("moonshot", "https://api.moonshot.cn/v1/chat/completions", "MOONSHOT_API_KEY", "kimi-k2-0905-preview", False)
    ZHIPU = ("zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "ZHIPU_API_KEY", "glm-4.6", False)
    OLLAMA = ("ollama", "http://localhost:11434/v1/chat/completions", None, "qwen2.5:7b", False)
    ANTHROPIC = ("anthropic", "https://api.anthropic.com/v1/messages", "ANTHROPIC_API_KEY", "claude-sonnet-4-5", True)
    # 自定义中转/私有部署：必须自己给 baseUrl 和 model
    CUSTOM = ("custom", None, None, None, False)

    def __init__(self, cli: str, endpoint: str | None, key_env: str | None,
                 default_model: str | None, anthropic_protocol: bool) -> None:
        self.cli = cli
        self.endpoint = endpoint
        self.key_env = key_env
        self.default_model = default_model
        self.anthropic_protocol = anthropic_protocol

    @classmethod
    def of(cls, name: str) -> "Provider":
        for provider in cls:
            if provider.cli == name.lower():
                return provider
        names = ", ".join(p.cli for p in cls)
        raise ValueError(f"不支持的 provider: {name}。可选: {names}")


@dataclass(frozen=True, slots=True)
class Resolved:
    """解析后的最终配置，打印出来方便排查 401 / 404。"""

    provider: str
    endpoint: str
    api_key: str
    model: str
    anthropic: bool
    max_tokens: int


def resolve(args: list[str]) -> Resolved:
    """解析最终配置，四条来源按优先级合并。

    这个过程刻意做成「纯函数、不发请求」，所以 --config 可以零成本自检：
    打印出来的 endpoint 就是程序真正要请求的地址，401/404 一眼就能定位。
    """
    provider_arg = pick(args, "--provider", "LLM_PROVIDER", None)
    provider_file = config.get("provider")
    provider_name = provider_arg or provider_file or "dashscope"
    provider = Provider.of(provider_name)

    # 命令行显式指定了另一家服务商时，配置文件里的 baseUrl / model 属于「旧的那家」，
    # 必须忽略，否则会出现 provider=openai 但 endpoint 还是 dashscope 的错配
    # （key 是通用配置，保留不动，用户仍可用 --api-key 覆盖）。
    provider_switched = (provider_arg is not None and provider_file is not None
                         and provider_arg.lower() != provider_file.lower())

    # baseUrl：命令行 > 环境变量 > 配置文件（同服务商时才用）> 预设
    base_url = pick(args, "--base-url", "LLM_BASE_URL", None)
    if base_url is None and not provider_switched:
        base_url = config.get("baseUrl")
    if base_url is None:
        base_url = provider.endpoint
    if not base_url or not base_url.strip():
        raise RuntimeError(
            "provider=custom 必须提供 baseUrl。三种写法任选其一：\n"
            "  1) 命令行:  python -m mycode --provider=custom --base-url=https://你的地址/v1 --api-key=sk-xxx\n"
            "  2) 环境变量: set LLM_BASE_URL=https://你的地址/v1\n"
            "  3) 配置文件: 在 agent.toml 里写 baseUrl = \"https://你的地址/v1\"")

    # apiKey：命令行 > LLM_API_KEY > 配置文件 > 该服务商专用变量
    api_key = pick(args, "--api-key", "LLM_API_KEY", None)
    if api_key is None:
        api_key = config.get("apiKey")
    if api_key is None and provider.key_env is not None:
        api_key = os.environ.get(provider.key_env)
    if api_key is None:
        api_key = ""

    model = pick(args, "--model", "MODEL", None)
    if model is None and not provider_switched:
        model = config.get("model")
    if model is None:
        model = provider.default_model
    if not model or not model.strip():
        raise RuntimeError("provider=custom 必须指定 model，例如 --model=qwen3-max")

    # 协议判定：显式指定 > 预设 > 看地址里有没有 /messages
    proto = pick(args, "--protocol", None, None)
    anthropic = (proto is not None and proto.lower() == "anthropic") or \
        (proto is None and (provider.anthropic_protocol or "/messages" in base_url))

    max_tokens = config.get_int("maxTokens", 4096)
    return Resolved(provider.cli, normalize(base_url, anthropic), api_key, model, anthropic, max_tokens)


def normalize(base_url: str, anthropic: bool) -> str:
    """把用户随手填的 baseUrl 补成完整端点 —— 这是最容易出错的地方。

    下面四种写法都能用，结果一致：
        https://xxx.com                    -> https://xxx.com/v1/chat/completions
        https://xxx.com/v1                 -> https://xxx.com/v1/chat/completions
        https://xxx.com/v1/                -> 同上（自动去掉尾部斜杠）
        https://xxx.com/v1/chat/completions -> 原样使用
    """
    trimmed = base_url.strip()
    while trimmed.endswith("/"):
        trimmed = trimmed[:-1]
    tail = "/messages" if anthropic else "/chat/completions"
    if trimmed.endswith(tail):
        return trimmed
    if re.search(r"/v\d+$", trimmed):
        return trimmed + tail  # /v1、/v4（智谱）
    return trimmed + "/v1" + tail


def create(args: list[str]) -> LlmClient:
    """按参数创建真实模型客户端，并打印一行配置摘要。

    打印这一行是有意为之：出 401 时第一眼就能确认「key 到底有没有读到」，
    比在代码里打断点快得多。key 会打码，不会泄露。
    """
    resolved = resolve(args)
    print(f"[模型] provider={resolved.provider} | model={resolved.model} | "
          f"endpoint={resolved.endpoint} | key={mask(resolved.api_key)}")
    if resolved.anthropic:
        return AnthropicClient(resolved.api_key, resolved.model, resolved.endpoint, resolved.max_tokens)
    return OpenAiCompatibleClient(resolved.endpoint, resolved.api_key, resolved.model)


def print_config(args: list[str]) -> None:
    """打印当前生效配置（--config 用）。纯本地解析，不发任何请求。"""
    resolved = resolve(args)
    source = "已读取 agent.toml" if config.has_file() else "未找到 agent.toml（用内置预设与环境变量）"
    print(f"[配置] 配置文件: {source}")
    print(f"  provider = {resolved.provider}")
    print(f"  model    = {resolved.model}")
    print(f"  endpoint = {resolved.endpoint}")
    print(f"  protocol = {'anthropic' if resolved.anthropic else 'openai 兼容'}")
    print(f"  maxTokens= {resolved.max_tokens}")
    print(f"  key      = {mask(resolved.api_key)}")


def mask(key: str | None) -> str:
    """密钥打码：只露头尾各几位，确认「读到了哪把 key」用。"""
    if not key or not key.strip():
        return "(未设置)"
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def pick(args: list[str], arg_name: str, env_name: str | None, default: str | None) -> str | None:
    """按优先级取一个配置值：先找命令行参数（--name=value），再找环境变量，最后用默认值。"""
    for a in args:
        if a.startswith(arg_name + "="):
            value = a[len(arg_name) + 1:].strip()
            if value:
                return value
    if env_name is not None:
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    return default
