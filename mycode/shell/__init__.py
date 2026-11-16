"""
外围层：界面、配置展示、成本、语言与会话存档 —— agent 的「脸面」。

    ui.py            终端渲染：圆角框、CJK 对齐、转圈等待、选项确认
    costs.py         token 用量与费用折算（价格来自 agent.toml 可选配置）
    language.py      模型回答语言（界面恒为中文）
    session_store.py 会话 JSONL 存档与 --continue 恢复
    shell.py         REPL 主循环 + 斜杠命令 + 装配（启动时把各层拼起来）

本层可以依赖 kernel / clients / tools 全部下层；反向禁止。
"""
