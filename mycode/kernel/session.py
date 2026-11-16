"""
会话（对应 Java 版 ChatSession）：把「一份上下文 + 一个 agent」包起来，支持多轮连续对话。

为什么单独抽一个类：Agent 本身是无状态的（每次 run 只负责跑一轮 ReAct），
而交互式命令行需要「记住上一轮说了什么」。把状态放在会话层，
Agent 就能同时被单次任务和连续对话两种场景复用，互不干扰。

放在 kernel/session.py 而不是 context.py：它依赖 Agent，而 agent.py 又依赖
context.py —— 单独一个文件避免循环导入。

一个会话 = 一份 AgentContext + 累计轮数 + 起始时间，
顺手提供上下文长度和耗时，方便 /cost 这类命令展示。
"""

from __future__ import annotations

import time
from pathlib import Path

from mycode.kernel.agent import Agent
from mycode.kernel.compressor import Stats
from mycode.kernel.context import AgentContext
from mycode.kernel.llm import StreamSink
from mycode.kernel.types import Usage


class ChatSession:
    """一次连续对话的状态容器。"""

    def __init__(self, agent: Agent, ctx: AgentContext | None = None,
                 turns: int = 0, usage: Usage | None = None) -> None:
        self._agent = agent
        # --continue 恢复会话时上下文从存档来；全新会话时向 agent 要一份干净上下文
        self._ctx = ctx if ctx is not None else agent.new_context()
        self._turns = turns
        self._total_usage = usage if usage is not None else Usage.ZERO
        self._start = time.monotonic()

    def persist(self, store) -> None:
        """把当前会话整体写到存档文件。存档内部吞异常，这里不用再兜。"""
        store.save(self._ctx, self._turns, self._total_usage)

    def ask(self, user_input: str, sink: StreamSink | None = None,
            image: Path | None = None) -> str:
        """提一个问题并返回本轮最终回答，上下文自动累积。

        文字边生成边推给 sink（传 None 表示不要流式），答案仍然完整返回。
        image 非 None 时走多模态通道（--image 用），是否支持取决于所配模型。
        """
        self._turns += 1
        if image is not None:
            answer = self._agent.run_with_image(self._ctx, user_input, image, sink)
        else:
            answer = self._agent.run(user_input, ctx=self._ctx, sink=sink)
        # 一次提问可能内部调好几轮模型，这里把整轮消耗一起累计
        self._total_usage = self._total_usage.plus(self._agent.last_run_usage)
        return answer

    def compress_now(self) -> Stats:
        """手动触发一次上下文压缩（对应 /compress 命令）。

        与自动压缩的区别：手动是「我现在就想瘦身」，所以强制压到当前体量的一半，
        不管有没有超阈值；自动压缩只在超过阈值时才动手，避免白白多花一次模型调用。
        """
        stats = self._agent.compress_now(self._ctx, force=True)
        # 摘要调用会花 token，也要计入会话成本
        self._total_usage = self._total_usage.plus(self._agent.last_run_usage)
        return stats

    def compress_threshold(self) -> int:
        """压缩触发阈值（字），界面展示用。"""
        return self._agent.compress_threshold()

    def reset(self) -> None:
        """清空记忆，重新开始（对应 /clear 命令）。"""
        self._ctx = self._agent.new_context()
        self._turns = 0
        self._total_usage = Usage.ZERO

    @property
    def total_usage(self) -> Usage:
        """整场会话累计用量。"""
        return self._total_usage

    @property
    def turns(self) -> int:
        """已完成轮数。"""
        return self._turns

    def context_chars(self) -> int:
        """当前上下文的估算字数，用来判断是不是快撑满了。"""
        return self._ctx.estimate_tokens()

    @property
    def summary(self) -> str:
        """当前的历史摘要（压缩产物），空串表示还没压缩过。"""
        return self._ctx.summary

    def elapsed_seconds(self) -> int:
        """会话已持续的秒数。"""
        return int(time.monotonic() - self._start)
