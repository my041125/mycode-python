"""
扩展层：与外部生态对接的能力（一期已接入技能，二期再补 MCP 与子代理）。

    frontmatter.py  SKILL.md / agents.md 通用 frontmatter 解析器
    skills.py       Agent Skills：目录扫描注册表 + use_skill 渐进加载工具
    mcp.py          MCP 外部工具桥接（二期）
    subagent.py     子代理定义与派发（二期）

分层位置：本层在 tools 之上（复用 CodeFile 的编码容错读取），shell 之下；
只被 shell 装配调用，绝不反向依赖 shell。
"""
