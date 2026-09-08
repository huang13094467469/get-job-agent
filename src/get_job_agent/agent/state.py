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

    新增护栏/运行配置字段（替代原 runtime_guard 的进程级全局 dict）：
    - ``greeting_count``: 本 thread 已成功发送的打招呼数 —— 投递上限的单一事实源。
    - ``sent_hashes``: 已发话术指纹列表 —— 防同话术重复发（用 list 而非 set，保证可 JSON 序列化）。
    - ``pending_job``: 最近一个「达标待沟通」岗位 —— compare 暂存 → check_greeting 预检 /
      confirm_greeting_sent 登记时取用并升级账本。
    - ``max_greetings``: 本轮投递上限（0=不限），由面板 agent_go 写入 —— 与 greeting_count 同源判定。
    - ``agent_mode``: 创建本 thread 的运行模式（confirm/unattended），resume 时按此取正确的图。
    - ``job_hunt``: 本轮任务是否为找工作（投递）意图：True 才允许无人值守自动续跑并注入
      _CONTINUE_MSG；普通对话/咨询则只跑一段即回，避免聊天时被诱导去投递。
    """

    greeting_count: NotRequired[int]
    sent_hashes: NotRequired[list[str]]
    pending_job: NotRequired[dict[str, Any] | None]
    max_greetings: NotRequired[int]
    agent_mode: NotRequired[str]
    job_hunt: NotRequired[bool]


__all__ = ["JobAgentState"]
