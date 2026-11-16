"""
桌面版前端（tkinter）：mycode 的图形界面，与 shell/ 平级的另一种「脸面」。

    theme.py     配色 / 字体 / Tk tag 常量（终端 ANSI 配色的 GUI 对照表）
    render.py    Tk 版 Markdown 渲染：文本 + tag 区间（与 shell/markdown.py 同一套语法）
    hook.py      GuiHook 钩子 + 权限确认 + TodoTool 的 presenter 等价物
    bridge.py    线程桥：worker 线程跑会话、事件队列、权限确认的跨线程等待
    runtime.py   装配：模型 / 工具 / 权限 / 技能（对照 shell.Shell 的装配段）
    commands.py  斜杠命令（清单与终端版对齐，输出落到聊天区）
    app.py       主窗口：对话区 / 输入区 / 状态栏 + 事件分发主循环
    __main__.py  入口：python -m desktop

与终端版的分工：kernel / clients / tools / extensions 四层完全复用、一行不改；
本层替换的只是「脸面」—— 终端的 ANSI 绘制换成 Tk 控件，input() 换成输入框，
阻塞式按键确认换成模态对话框。分层铁律与 shell 一致：
本层可以依赖 kernel / clients / tools / extensions 全部下层；反向禁止。

线程模型（内核是纯同步阻塞代码，Tk 只允许主线程碰控件）：
    worker 线程跑 ChatSession.ask，流式增量与钩子事件全部投递事件队列；
    主线程 root.after 轮询队列分发到控件。权限确认在 worker 线程被同步调用，
    用「主线程弹模态框 + threading.Event 等待」跨线程收拢回三态结果。

启动方式：python -m desktop [--demo] [--continue] [--lang=zh|en] [--permission=...]，
参数含义与终端版一致。
"""
