---
name: hello-mycode
description: 内置演示技能：展示 Agent Skills 的兼容格式与 use_skill 的加载方式。想了解技能怎么写、怎么被发现、怎么被加载时使用。
allowed-tools: Read, Bash
---

# hello-mycode

这是一个**内置演示技能**，用于验证本客户端完整兼容 Claude Code 的 Agent Skills 格式。

## 技能格式要点

1. 一个技能 = 一个子目录 + 一个 `SKILL.md`；
2. frontmatter 里写 `name` / `description`（都可省略，缺省分别用目录名和「未提供描述」）；
3. frontmatter 之后的正文，就是模型调用 use_skill 后看到的操作说明；
4. `allowed-tools` 可声明工具白名单，本客户端会把 Claude Code 的工具名
   映射成自己的工具名（Read→read_code、Bash→run_command）提示给模型；
5. 技能目录里可以放脚本和参考资料，use_skill 会列出附带文件清单。

## 验证方式

- `/skills` 查看已加载技能与来源标签（本技能的来源应显示「内置」）；
- 让模型「用 hello-mycode 技能打个招呼」，观察 `use_skill` 工具调用；
- 技能脚本用 run_command 执行 —— 项目有 `.venv` 时 python 自动指向其中的解释器，
  技能的依赖装在 .venv 里即可，不污染全局环境。

## $ARGUMENTS 占位符

use_skill 的 `args` 参数会替换正文里的占位符。当前参数：$ARGUMENTS
