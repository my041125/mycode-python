"""
入口模块：命令行参数分发（对应 Java 版 Main，只做分发不做业务）。

分发顺序与 Java 版保持一致：

    无参数            → 交互模式（主用法）
    --help / -h       → 中文帮助
    --config          → 打印当前生效配置（不发请求）
    --dump            → 打印 OpenAI/Anthropic 两种协议报文
    --mock / --real   → 门票演示：查景点 → 查天气 → 算门票（--mock 离线假模型）
    --demo            → 交互模式的离线演示（假模型跑完整工具链）
    --code-demo       → 代码编辑离线演示：读文件 → 替换 → 校验 → 出差异
    "问题"            → 单次提问，回答后退出
    其余参数           → 交互模式

每个分支只做「解析参数 → 调对应模块」，不写业务。
"""

from __future__ import annotations

import sys

from mycode import __version__


def _print_help() -> None:
    """打印中文帮助（对应 Java 版 --help 分支）。"""
    print(
        f"""mycode {__version__} —— 从零手写的命令行 AI 编程助手（Python 复写版）

用法:
  python -m mycode                 交互模式（主用法）
  python -m mycode "问题"          单次提问，回答后退出
  python -m mycode --demo          离线演示：假模型跑完整工具链，不联网不花钱
  python -m mycode --help          本帮助

常用参数:
  --provider=名字 / --base-url=地址 / --api-key=密钥 / --model=模型名
                                   临时指定模型，优先级高于 agent.toml
  --permission=read-only|edit|full 权限模式（默认 edit：可改代码，跑命令要批准）
  --lang=zh|en                     回答语言（界面恒为中文）
  --continue                       恢复上次会话
  --no-compress                    关闭上下文自动压缩
  --image=路径 "问题"              带图提问（需多模态模型）

调试入口:
  --config     查看当前生效的模型配置（不发请求）
  --dump       打印 OpenAI 与 Anthropic 两种协议的真实报文
  --mock       门票演示：查景点 → 查天气 → 算门票，离线假模型跑完整 ReAct 循环
  --code-demo  代码编辑演示：读文件 → 替换 → 语法校验 → 出差异

定时任务:
  --cron             执行所有到期的持久化定时任务（供任务计划每分钟拉起）
  --cron-register    注册 Windows 计划任务 mycode-cron（每分钟拉起 --cron）
  --cron-unregister  移除该计划任务
"""
    )


def main(argv: list[str]) -> int:
    """参数分发主函数，返回进程退出码。"""
    # 1. 无参数 → 交互模式（主用法）
    if not argv:
        from mycode.shell import shell
        shell.run(argv)
        return 0

    first = argv[0]

    # 2. --help / -h → 中文帮助
    if first in ("--help", "-h"):
        _print_help()
        return 0

    # 3. --config → 打印生效配置（不发请求，纯本地解析）
    if first == "--config":
        from mycode.clients.factory import print_config
        print_config(argv)
        return 0

    # 4. --dump → 打印 OpenAI 与 Anthropic 两种协议报文
    if first == "--dump":
        from mycode.clients import dump
        dump.run()
        return 0

    # 5. --mock / --real → 门票演示：完整 ReAct 循环（--mock 离线假模型）
    if first in ("--mock", "--real"):
        from mycode.demo import mock_demo
        mock_demo(argv, mock=(first == "--mock"))
        return 0

    # 6. --code-demo → 代码编辑离线演示；--demo → 交互模式的离线演示
    if first == "--code-demo":
        from mycode.demo import coding_demo
        coding_demo(argv)
        return 0
    if first == "--demo":
        from mycode.shell import shell
        shell.run(argv)
        return 0

    # 7. 定时任务无头执行器与系统级注册（用户手动执行注册）
    if first == "--cron":
        from mycode import cron
        return cron.run_due(argv)
    if first == "--cron-register":
        from mycode import cron
        return cron.register(argv)
    if first == "--cron-unregister":
        from mycode import cron
        return cron.unregister(argv)

    # 8. 首参数不以 - 开头（或参数串里夹着裸词）→ 单次提问
    non_flags = [a for a in argv if not a.startswith("-")]
    if non_flags:
        from mycode.shell import shell
        shell.run_once(" ".join(non_flags), argv)
        return 0

    # 9. 其余（--shell / 只给了 --key=value 这类模型参数）→ 交互模式
    from mycode.shell import shell
    shell.run(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
