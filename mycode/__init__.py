"""
mycode —— 从零手写的命令行 AI 编程助手（Python 复写版）。

除 Python 标准库外零第三方依赖，目标是让人看清一个 agent 到底由哪些零件组成，
而不是再学一套框架 API。原版为同名的 Java 项目（E:\\javaagent），模块一一对应。

分层结构（上层可依赖下层，下层禁止 import 上层）：

    kernel/   内核：工具接口、会话上下文、ReAct 主循环、钩子 —— agent 的本质
    clients/  模型客户端：OpenAI 兼容 / Anthropic 双协议 + SSE 流式 + 重试
    tools/    内置工具：命令执行 / 代码编辑 / 联网搜索 / 待办清单
    shell/    外围：交互界面、配置、存档、费用统计

配置文件 agent.toml 的加载在包根 config.py，各层都可以用。
"""

__version__ = "1.0"
