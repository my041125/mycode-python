"""
内置工具层：一工具一模块，全部实现 kernel/tool.py 的 Tool 接口。

    command.py      run_command —— shell 命令执行（含后台任务登记）
    code_file.py    代码文件内存映像：保住编码 / 换行 / BOM 等隐形属性
    code_tools.py   read_code / list_files —— 只读定位
    code_editor.py  纯函数文本变换：锚点定位、缩进对齐、差异统计
    code_edit.py    edit_code —— 编辑落盘 + 校验 + 自动回滚
    syntax_check.py 语法校验：.py 走 ast，其它文件走括号平衡粗检
    web_search.py   web_search —— Tavily / 博查 / 离线模拟三供应商
    todo.py         todo —— 可视化任务清单
    backup.py       多版本快照仓库，支撑 /undo 与 /history

本层只依赖 kernel 与包根 config；shell 层负责装配注册。
"""
