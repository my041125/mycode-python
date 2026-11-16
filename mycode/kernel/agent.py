"""
内核部件 3：ReAct 主循环（对应 Java 版 Agent）。整个 agent 的本质就是这个 for 循环。

    目标 → [思考 → 行动 → 观察] × n → 最终答案

三个容易踩的坑，全部在这里处理掉了：
    1. 循环必须有 max_steps 上限，否则模型抽风会无限调工具烧钱；
    2. 没有工具调用 ≠ 出错了，它就是「模型认为可以回答了」，直接返回即可；
    3. 工具结果必须截断，一次大结果就能把上下文撑爆，导致模型跑偏。

之所以用 for 而不是 while True：步数上限是硬约束，写在循环条件里比写在循环体里更难漏。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from mycode import config
from mycode.kernel import retries
from mycode.kernel.context import AgentContext
from mycode.kernel.hooks import Hook
from mycode.kernel.llm import LlmClient, StreamSink, as_sink
from mycode.kernel.registry import ToolRegistry
from mycode.kernel.compressor import Stats, compress
from mycode.kernel.tool import Tool
from mycode.kernel.types import ChatResult, ToolCall, Usage

# 并行执行一批工具调用时的最大线程数
_MAX_PARALLEL_WORKERS = 4


class Agent:
    """ReAct 循环执行器。参数多，全部用关键字传参（对应 Java 版的 Builder）。"""

    def __init__(
        self,
        name: str,
        llm: LlmClient,
        *,
        tools: ToolRegistry | None = None,
        system_prompt: str = "你是一个严谨的助手。",
        hooks: list[Hook] | None = None,
        max_steps: int | None = None,
        max_retries: int | None = None,
        parallel_tools: bool | None = None,
        max_tool_output_chars: int = 1500,
        max_context_tokens: int | None = None,
        compression_enabled: bool = True,
        compress_ratio: float | None = None,
        compress_keep: int | None = None,
    ) -> None:
        self.name = name                  # agent 名称，只用于日志，方便区分子任务
        self.llm = llm                    # 模型客户端，这是唯一与厂商耦合的地方
        self.tools = tools if tools is not None else ToolRegistry()
        self.system_prompt = system_prompt  # 工具清单会被自动追加到它后面
        self.hooks = list(hooks) if hooks else []  # 拦截钩子链，按注册顺序依次执行
        # 以下默认值读 agent.toml（对应 Java 版 Builder 里的 AgentConfig 读取）
        self.max_steps = max_steps if max_steps is not None else _positive_int("maxSteps", 20)
        self.max_retries = max_retries if max_retries is not None else _positive_int("maxRetries", 3)
        self.parallel_tools = (parallel_tools if parallel_tools is not None
                               else config.get_bool("parallelTools", True))
        self.max_tool_output_chars = max_tool_output_chars
        # 上下文上限 60000 字（英文代码约 15k~20k token）：前缀缓存命中部分按约 1/10
        # 计费，长前缀反而更省，所以不必吝啬
        self.max_context_tokens = (max_context_tokens if max_context_tokens is not None
                                   else _positive_int("maxContextTokens", 60000))
        self.compression_enabled = compression_enabled
        self.compress_ratio = compress_ratio if compress_ratio is not None else _ratio("compressRatio", 0.7)
        self.compress_keep = compress_keep if compress_keep is not None else _positive_int("compressKeep", 4)
        self._last_run_usage = Usage.ZERO

    # ---------------- 任务入口 ----------------

    def new_context(self) -> AgentContext:
        """新建一份干净上下文：system 提示词 + 自动追加的工具清单。

        交互式对话需要把上下文留住，所以单独暴露出来。
        """
        return AgentContext.of(self.system_prompt + self.tools.describe())

    def run(self, user_input: str, ctx: AgentContext | None = None,
            sink: StreamSink | None = None) -> str:
        """执行一次完整任务，返回最终回答；出错时返回以 ERROR: 开头的说明文本，不抛异常。

        ctx 传 None 时新建一份上下文（两次调用之间不共享记忆）；
        需要多轮对话请自己持有上下文反复传入 —— 命令行交互模式就是这么做的。
        sink 是文本增量回调，传 None 表示不要流式。
        """
        if ctx is None:
            ctx = self.new_context()
        # 压缩只在任务开始前做一次，绝不能放进循环里：
        # 压缩会重写 system 里的摘要并摘掉最老的对话，等于改写请求的前缀，
        # 而 DeepSeek 的前缀缓存按字节精确匹配 —— 循环内每步都压的话，
        # 上下文一旦超过阈值，之后每一步的前缀都和上一步不同，缓存步步全失效。
        # 挪到任务开始前：一次用户提问最多付一次「缓存作废」的代价。
        self._maybe_compress(ctx)
        ctx.user(user_input)
        return self._run_loop(ctx, sink)

    def run_with_image(self, ctx: AgentContext, user_input: str,
                       image: Path, sink: StreamSink | None = None) -> str:
        """带图片的一次任务（--image 用）。与 run 的唯一差别是用户消息按
        OpenAI 协议的块数组形态写入（文字 + 图片 base64），其余流程完全一致。
        读写图片出错时返回 ERROR 文本，不抛异常 —— 与 run 的失败约定一致。
        """
        self._maybe_compress(ctx)
        try:
            ctx.user_with_image(user_input, image)
        except Exception as exc:  # noqa: BLE001 —— 与 run 的失败约定一致：返回 ERROR 文本
            return "ERROR: 图片读取失败 - " + str(exc)
        return self._run_loop(ctx, sink)

    # ---------------- 主循环本体 ----------------

    def _run_loop(self, ctx: AgentContext, sink: StreamSink | None) -> str:
        # 每次任务开始清零，统计的是「这一次 run」而不是整个会话
        self._last_run_usage = Usage.ZERO

        for _step in range(1, self.max_steps + 1):
            # 循环内只留 trim 兜底：单条超大工具结果把上下文顶爆时丢最老的。
            # 正常情况下它是空操作，一个字节都不动，前缀（也就是缓存）保持原样
            ctx.trim(self.max_context_tokens)
            self._fire(lambda h: h.before_llm(ctx))

            # 模型调用与瞬时故障重试都收在 _call_model 里；失败到不可挽回时
            # 它返回一条以 ERROR 开头的文本结果，走「最终答案」路径如实上报，不抛异常
            result = self._call_model(ctx, sink)
            self._fire(lambda h: h.after_llm(result))
            # 累计本轮用量：一次任务可能调好几轮模型，成本要加总
            self._last_run_usage = self._last_run_usage.plus(result.usage)

            # 分支 A：模型不再要求调用工具 → 这就是最终答案，退出循环
            if not result.has_tool_calls:
                return result.text or ""

            # 分支 B：先把「我想调什么」写回上下文，再逐个执行。
            # 顺序不能反：assistant 消息必须在 tool 消息之前，否则协议校验不过
            ctx.assistant(result.text, result.tool_calls)

            # 1) 审批阶段：串行。确认交互要读共享的 stdin，不能并发。
            #    被拒的调用不执行，但必须回填一条观察，否则模型不知道被拒会一直重试
            batch: list[_Dispatch] = []
            for call in result.tool_calls:
                tool = self.tools.find(call.name)
                allowed = all(h.before_tool(call.name, call.args) for h in self.hooks)
                batch.append(_Dispatch(call, tool, allowed))

            # 2) 执行阶段：一批里放行了多个调用就并行跑（二期子代理扇出的基础），
            #    单个调用或关闭并行时串行。并行只发生在 dispatch 内部，
            #    终端输出由第 3 步按原顺序统一回填，所以界面不会乱
            outputs = self._execute_batch(batch)

            # 3) 回填阶段：串行按原顺序截断、用量归集、通知钩子、写回上下文。
            #    最关键的 ReAct 一步在这里：模型下一轮才能「看到」工具返回了什么
            for d in batch:
                observation = (outputs.get(d.call.id, "") if d.allowed
                               else "ERROR: 该工具调用被审批钩子拦截，请换一种方式或告知用户")
                if d.tool is not None:
                    drain = getattr(d.tool, "drain_usage", None)
                    if callable(drain):
                        # task 这类工具会在主循环之外另花 token（子代理自己跑了好几轮模型），
                        # 把挂起的用量并进本次 run 的总账，成本统计才不会漏记
                        self._last_run_usage = self._last_run_usage.plus(drain())
                obs = self._truncate(observation, d.tool)
                for hook in self.hooks:
                    hook.after_tool(d.call.name, obs)
                ctx.tool_result(d.call.id, d.call.name, obs)

        # 走到这里说明 max_steps 用完还没收敛，属于异常情况，如实上报而不是编一个答案
        return f"ERROR: 达到最大步数 {self.max_steps} 仍未完成，已中止（防止无限循环烧 token）"

    # ---------------- 模型调用与重试 ----------------

    def _call_model(self, ctx: AgentContext, sink: StreamSink | None) -> ChatResult:
        """调一次模型，瞬时故障（限流/超时/断连）自动指数退避重试。

        长任务跑到第十几步遇一次 429 就整轮报废太亏 —— 上下文都在本地，
        重试一次通常就过去了。401/403/400 这类配置或参数错误不重试，
        重试八次也是白等，立刻如实上报。

        流式下只有还一个字都没吐时才允许重试：前一次的半截文本已经在
        终端上了，重试会造成重复输出。

        重试耗尽仍失败时返回一条以 ERROR 开头的文本结果。
        """
        # 先把裸函数形态的回调统一成 StreamSink（对应 Java 函数式接口的便利写法）
        sink = as_sink(sink)
        # 统计已流出的字数，包一层 sink，重试安全性靠它判断
        emitted = 0

        def counted(delta: str) -> None:
            nonlocal emitted
            emitted += len(delta)
            sink.on_text(delta)  # type: ignore[union-attr] —— 调用点已保证 sink 非 None

        counting_sink = counted if sink is not None else None

        failure: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                wait = retries.backoff_millis(attempt - 1)
                reason = "" if failure is None else str(failure)
                for hook in self.hooks:
                    hook.on_model_retry(attempt, wait, reason)
                time.sleep(wait / 1000)
                # 重试是一次真实的模型调用，转圈等界面重新拉起
                for hook in self.hooks:
                    hook.before_llm(ctx)
            try:
                # 有 sink 走流式，没有走普通接口；不支持流式的客户端由 LlmClient 默认实现兜底
                if counting_sink is None:
                    return self.llm.chat(ctx, self.tools.all())
                return self.llm.chat_stream(ctx, self.tools.all(), counting_sink)
            except Exception as exc:  # noqa: BLE001 —— 网络层异常形态太多，按消息分类
                failure = exc
                if emitted > 0 or not retries.transient_failure(exc):
                    break
        return ChatResult.of_text("ERROR: 模型调用失败 - " + ("被中断" if failure is None else str(failure)))

    # ---------------- 工具执行 ----------------

    def _execute_batch(self, batch: list["_Dispatch"]) -> dict[str, str]:
        """执行一批放行的工具调用，返回 call_id -> 观察。

        多个调用并行时用固定小线程池；这要求工具实现无共享可变状态
        （现有工具都满足，task 的用量累积已做同步）。
        parallel_tools=False 或只有单个调用时退回串行，行为完全一致。
        """
        runnable = [d for d in batch if d.allowed]
        out: dict[str, str] = {}
        if not runnable:
            return out

        if not self.parallel_tools or len(runnable) == 1:
            for d in runnable:
                out[d.call.id] = self.tools.dispatch(d.call.name, d.call.args)
            return out

        with ThreadPoolExecutor(max_workers=min(len(runnable), _MAX_PARALLEL_WORKERS),
                                thread_name_prefix="mycode-tool") as pool:
            futures = {d.call.id: pool.submit(self.tools.dispatch, d.call.name, d.call.args)
                       for d in runnable}
            # 按提交顺序收结果：谁先谁后跑完不影响观察的次序
            for d in runnable:
                try:
                    out[d.call.id] = futures[d.call.id].result()
                except Exception as exc:  # noqa: BLE001 —— dispatch 理论上不抛，兜一层防第三方
                    out[d.call.id] = "ERROR: 工具执行失败 - " + str(exc)
        return out

    def _truncate(self, text: str | None, tool: Tool | None) -> str:
        """截断工具结果。宁可丢尾部，也不要让一条大结果挤掉整个对话历史。

        工具可以用 result_char_limit() 申请更大的额度（如 task 返回子代理交付物），
        没申请就用全局默认。
        """
        if text is None:
            return ""
        limit = (tool.result_char_limit()
                 if tool is not None and tool.result_char_limit() > 0
                 else self.max_tool_output_chars)
        return text if len(text) <= limit else text[:limit] + "\n...(结果过长已截断)"

    # ---------------- 压缩与用量 ----------------

    def _maybe_compress(self, ctx: AgentContext) -> None:
        """上下文超阈值时执行压缩。

        注意压缩本身要调一次模型，所以只在真正超限时才做 ——
        每轮都压会让成本和延迟都翻倍。
        """
        if not self.compression_enabled:
            return
        target = int(self.max_context_tokens * self.compress_ratio)
        if ctx.estimate_tokens() <= target:
            return
        stats = compress(ctx, self.llm, target, self.compress_keep)
        if stats.removed_messages == 0:
            return
        # 摘要调用也花钱，算进本次任务成本
        self._last_run_usage = self._last_run_usage.plus(stats.usage)
        for hook in self.hooks:
            hook.on_context_compressed(stats.removed_messages, stats.saved_chars, ctx.summary)

    def compress_now(self, ctx: AgentContext, force: bool = False) -> Stats:
        """立即压缩一次（供 /compress 命令手动触发）。

        force=True 表示不管当前有没有超阈值，都压到「当前体量的一半」；
        False 表示按阈值判断（和自动压缩一致）。
        没有可压内容时返回的 Stats 的 removed_messages 为 0。
        """
        target = (max(600, ctx.estimate_tokens() // 2) if force
                  else int(self.max_context_tokens * self.compress_ratio))
        stats = compress(ctx, self.llm, target, self.compress_keep)
        if stats.removed_messages > 0:
            self._last_run_usage = self._last_run_usage.plus(stats.usage)
            for hook in self.hooks:
                hook.on_context_compressed(stats.removed_messages, stats.saved_chars, ctx.summary)
        return stats

    def compress_threshold(self) -> int:
        """压缩触发阈值（字），供界面展示。"""
        return int(self.max_context_tokens * self.compress_ratio)

    @property
    def last_run_usage(self) -> Usage:
        """最近一次 run 的 token 用量，会话层用它累计总成本。"""
        return self._last_run_usage

    def _fire(self, call) -> None:
        """按注册顺序调用每个钩子的同名方法（小助手，免得两行 for 满天飞）。"""
        for hook in self.hooks:
            call(hook)


@dataclass(frozen=True, slots=True)
class _Dispatch:
    """一轮工具调用的执行单元：调用本身、解析出的工具实例、是否放行。"""

    call: ToolCall
    tool: Tool | None
    allowed: bool


# ---------------- 配置解析小助手（对应 Java 版 Builder 里的静态方法） ----------------

def _positive_int(key: str, default: int) -> int:
    """解析配置里的正整数，非法或非正就回到默认值。"""
    n = config.get_int(key, default)
    return n if n > 0 else default


def _ratio(key: str, default: float) -> float:
    """解析配置里的比例（0.2~1.0 之间才合法），非法或越界就回到默认值。"""
    value = config.get_float(key, default)
    return value if 0.2 < value < 1.0 else default
