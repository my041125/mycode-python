"""
模型客户端层：唯一知道厂商报文格式的地方。

    sse.py            SSE 增量累加器 + 工具参数解析公共件
    net.py            HTTP 公共件：URL 校验与连接建立（http.client 直连）
    openai_client.py  OpenAI 兼容协议（含流式）
    anthropic_client.py Anthropic Messages 协议（含协议转换）
    factory.py        按配置选厂商：resolve 解析配置、create 造客户端
    scripted.py       离线假模型（先验证控制流，再接真模型）
    dump.py           --dump 协议对照：同一对话渲染成两种报文

本层实现 kernel/llm.py 里定义的 LlmClient 端口；只依赖 kernel 与包根 config。
"""
