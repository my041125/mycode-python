"""
内核层：agent 的四个核心部件，不依赖任何上层（clients/tools/shell）。

    types.py       数据类：Usage / ToolCall / ChatResult（全项目唯一定义处）
    tool.py        内核部件 1：Tool 接口与 SimpleTool 快捷实现
    registry.py    内核部件 1 的容器：工具注册表 + 安全分发
    context.py     内核部件 2：上下文 / 记忆（OpenAI 协议消息数组）
    llm.py         模型客户端端口：LlmClient 接口与 StreamSink
    agent.py       内核部件 3：ReAct 主循环
    session.py     会话：上下文 + agent 的多轮状态容器
    compressor.py  上下文压缩器：把最老的一轮对话换成摘要
    retries.py     模型调用的瞬时故障分类与指数退避策略
    hooks.py       内核部件 4：拦截钩子（Hook 接口 + 三个内置实现）
    permission.py  权限：三档模式 + 命令安全策略 + 审批钩子

上层可以 import 本层，本层绝不 import 上层 —— 这是全项目最重要的依赖铁律。
"""
