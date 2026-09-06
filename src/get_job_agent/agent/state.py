"""求职 Agent 的 custom state schema：把跨 run 存活、需随 thread checkpoint 持久的护栏计数入 state。

对齐 context-engineering.md「Custom state schema」：rate limiting / usage tracking 这类
cross-cutting concern 应放进继承 ``DeepAgentState`` 的 custom state（或 store），而非进程级
全局可变 dict。配持久 checkpointer（Postgres）后，这些计数天然跨重启保留，解决「重启即失忆 /
无人值守突破单轮上限 / 去重失效」（评估 P0-2）。

字段无自定义 reducer → 覆盖语义；工具经注入的 ``ToolRuntime`` 读 ``runtime.state``、
返回 ``Command(update=...)`` 写入（评估 P1-6）。state 本身按 thread_id 由 checkpointer 隔离，
故不再需要进程级 thread_id → dict 的映射。
"""

from __future__ import annotations

from typing import Any, NotRequired

from deepagents import DeepAgentState


class JobAgentState(DeepAgentState):
    """继承 DeepAgentState（保留 messages 的 DeltaChannel reducer，使 checkpoint 增长线性）。

    新增三个护栏字段（替代原 runtime_guard 的进程级全局 dict）：
    - ``greeting_count``: 本 thread 已成功发送的打招呼数 —— 投递上限的单一事实源。
    - ``sent_hashes``: 已发话术指纹列表 —— 防同话术重复发（用 list 而非 set，保证可 JSON 序列化）。
    - ``pending_job``: 最近一个「达标待沟通」岗位 —— compare 暂存 → send_greeting 取用并升级账本。
    """

    greeting_count: NotRequired[int]
    sent_hashes: NotRequired[list[str]]
    pending_job: NotRequired[dict[str, Any] | None]


__all__ = ["JobAgentState"]
