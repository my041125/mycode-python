"""桌面版入口：python -m desktop [参数]。

参数与终端版一致（--demo / --continue / --lang=xx / --permission=xx /
--provider= 等，模型配置参数原样透传给装配层）。--help 打印帮助后退出。
"""

from __future__ import annotations

import sys

_HELP_TEXT = """mycode 桌面版（tkinter 图形界面）

用法：python -m desktop [参数]
  python -m desktop                正常启动（模型配置读 agent.toml）
  python -m desktop --demo         离线演示：脚本化假模型，不联网不花钱
  python -m desktop --continue     恢复最近一次会话（每轮自动存档到 .mycode/sessions/）
  python -m desktop --lang=en      指定回答语言（zh / en）
  python -m desktop --permission=full   权限模式（read-only / edit / full，默认 edit）

模型配置参数与终端版完全一致：--provider= --base-url= --api-key= --model= --protocol=
配置优先级详见 mycode/clients/factory.py，或运行 python -m mycode --config 查看。

窗口内：回车发送，Shift+回车换行，/help 查看全部斜杠命令。
未配置 apiKey 时启动会自动弹窗收集（可勾选保存到 agent.toml，下次不再询问）；
--demo 离线演示不需要密钥，不弹窗。
"""


def main(argv: list[str]) -> None:
    """入口分发：--help 打印帮助；其余参数原样交给主窗口装配层。"""
    if argv and argv[0] in ("--help", "-h"):
        print(_HELP_TEXT)
        return
    # 延迟 import：--help 不需要拉起 Tk 与整条装配链
    import tkinter as tk

    from desktop.app import DesktopApp

    root = tk.Tk()
    DesktopApp(root, argv)
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
