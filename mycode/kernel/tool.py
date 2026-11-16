"""
内核部件 1：工具。

一个工具由四样东西组成：

    name()        模型调用时用的名字，全局唯一，建议小写下划线
    description() 给模型看的用途说明，这段文字直接决定调用准确率
    parameters()  参数结构（JSON Schema），告诉模型该传哪些字段
    run()         真正干活的代码（Java 版叫 execute，Python 惯例用 run）

设计上刻意让 parameters() 返回普通 dict，不引入任何 JSON 库的特殊类型 ——
Python 版连这一层都可以说是「真正的零依赖」（Java 版至少还靠 Jackson 序列化）。

实现一个工具有两条路：
    1. 继承 Tool 写一个类（逻辑复杂、需要内部状态时用，如 TodoTool）
    2. 直接用下面的 SimpleTool 三行搞定一个函数式工具（如演示用的天气获取）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from mycode.kernel.types import Usage


class Tool(ABC):
    """工具接口。run 允许抛异常：ToolRegistry.dispatch 会统一捕获并转成
    observation 文本，让模型自己判断是改参数重试还是放弃。"""

    @abstractmethod
    def name(self) -> str:
        """工具名。建议小写下划线，例：web_search、calc_ticket_price。"""

    @abstractmethod
    def description(self) -> str:
        """用途说明。写法要点：说清「什么时候该用」也要说「什么时候不该用」；
        别写「取回数据」这种空话，模型只能靠这段文字做决策。"""

    @abstractmethod
    def parameters(self) -> dict:
        """参数结构，标准 JSON Schema。模型据此生成调用参数，缺字段会直接报错。"""

    @abstractmethod
    def run(self, args: dict) -> str:
        """执行工具。

        参数可能缺字段或类型不对，实现方要自己校验 —— 建议用 ValueError 表示
        「参数不合法」，ToolRegistry 会把它单独归类（这类错误模型改下参数就能
        成功，不必放弃）。返回的结果文本会原样进入模型上下文，注意别太长。
        """

    def result_char_limit(self) -> int:
        """本工具单条结果允许的最大字符数，0 表示用 Agent 的全局默认。

        绝大多数工具不需要覆盖。需要覆盖的典型是 task（二期）：它返回的是
        子代理精炼后的交付物而不是一条原始命令输出，按全局默认截断会把
        结论砍成半截话，主代理拿到后没法续接任务。
        """
        return 0

    # ---------------- 便捷构造：省去手写 schema 的样板代码 ----------------

    @staticmethod
    def schema(props: dict, *required: str) -> dict:
        """构造对象类型的 schema。props 一般用下面的 str_param/num_param 生成。"""
        return {"type": "object", "properties": props, "required": list(required)}

    @staticmethod
    def str_param(desc: str) -> dict:
        """定义一个字符串参数。（Java 版叫 str()，Python 里避开内置名）"""
        return {"type": "string", "description": desc}

    @staticmethod
    def num_param(desc: str) -> dict:
        """定义一个数值参数（整数和小数都用它，模型对 number 的支持最好）。"""
        return {"type": "number", "description": desc}

    @staticmethod
    def canonical(node: object) -> object:
        """递归把 schema 里所有 dict 按 key 排序重建，list 逐项递归。

        为什么需要它：工具声明位于请求报文的最前端，而 DeepSeek 的前缀缓存按
        字节精确匹配。Python 的 json 序列化按 dict 插入顺序输出，工具声明
        散落在各文件里，谁先谁后全凭手写习惯；统一排序后，无论工具怎么声明
        schema，序列化字节都恒定，重启进程后「工具声明 + system」这段公共
        前缀仍能命中缓存。排序只是换了字段出现顺序，JSON Schema 语义不受影响。
        """
        if isinstance(node, dict):
            return {k: Tool.canonical(v) for k, v in sorted(node.items())}
        if isinstance(node, list):
            return [Tool.canonical(item) for item in node]
        return node

    @staticmethod
    def to_int(value: object, default: int) -> int:
        """把模型给的参数值容错转成 int。

        模型可能传数字、字符串数字或小数，解析不了就用默认值 ——
        不能让一次类型失误废掉整个工具调用。
        """
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(float(str(value).strip()))
        except (ValueError, TypeError):
            return default


@runtime_checkable
class UsageContributor(Protocol):
    """会在主循环之外另花 token 的工具（二期的 task 子代理工具是典型）。

    子代理自己跑了好几轮模型，这些用量发生在主循环的模型调用之外；
    实现 drain_usage() 把「挂起的用量」交回主循环，成本统计才不会漏记。
    主循环用 duck-typing 探测（getattr(tool, "drain_usage")），不要求显式继承。
    """

    def drain_usage(self) -> Usage:
        """交回挂起的用量；调用后内部计数应清零（drain 语义）。"""
        ...


class SimpleTool(Tool):
    """函数式工具：把 name / description / parameters / handler 四样打包。

    免写子类，两行注册一个工具：

        SimpleTool("get_weather", "获取城市当日天气",
                   Tool.schema({"city": Tool.str_param("城市名")}, "city"),
                   lambda args: "多云 22~28℃")

    handler 抛出的异常由 ToolRegistry 统一兜住转成 ERROR 文本，不用自己写 try。
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler,
    ) -> None:
        self._name = name
        self._description = description
        self._parameters = parameters
        self._handler = handler

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return self._description

    def parameters(self) -> dict:
        return self._parameters

    def run(self, args: dict) -> str:
        return self._handler(args)
