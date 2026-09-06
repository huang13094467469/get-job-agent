"""运行期护栏辅助：投递计数 / 话术去重的 state 读写（无进程级全局状态）。

原实现用模块级 dict + threading.Lock 存「已成功发送数 / 话术指纹 / 暂存岗位」，只活在进程内存：
重启即失忆、无人值守可能突破单轮上限、去重失效，且与已落盘持久的 job_ledger 形成「一个持久、
一个易失」的割裂口径（评估 P0-2）。现迁入 custom state schema（JobAgentState），随 thread
checkpoint 持久——配 Postgres checkpointer 后天然跨重启保留。

本模块只提供**无状态纯函数**：从 state 读护栏字段、构造发送成功后的 state 更新片段。
工具经注入的 ToolRuntime 读 ``runtime.state``、返回 ``Command(update=...)`` 写入（评估 P1-6）；
state 按 thread_id 由 checkpointer 隔离，故不再需要进程级 thread_id → dict 映射，也不再依赖
``langgraph.config.get_config()`` 取 thread_id。
"""

from __future__ import annotations

import hashlib
from typing import Any


def hash_greeting(text: str) -> str:
    """话术指纹（去重用）：strip 后 sha1 前 16 位。"""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def get_greeting_count(state: Any) -> int:
    """从 state 读「已成功发送数」——投递上限的单一事实源。"""
    try:
        return int((state or {}).get("greeting_count", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def get_sent_hashes(state: Any) -> list[str]:
    """从 state 读已发话术指纹列表（容错非序列化为空）。"""
    h = (state or {}).get("sent_hashes") if hasattr(state, "get") else None
    if isinstance(h, (list, tuple, set)):
        return [str(x) for x in h]
    return []


def is_duplicate(state: Any, text: str) -> bool:
    """本会话（thread state）是否已成功发过完全相同的话术（防同话术重复骚扰 HR）。"""
    return hash_greeting(text) in get_sent_hashes(state)


def get_pending_job(state: Any) -> dict[str, Any] | None:
    """读 compare 暂存的「判定达标、准备沟通」岗位（供 send_greeting 成功后升级为账本已打招呼）。"""
    p = (state or {}).get("pending_job") if hasattr(state, "get") else None
    return p if isinstance(p, dict) else None


def sent_update(state: Any, text: str) -> dict[str, Any]:
    """发送成功后返回 state 更新片段：计数 +1、追加话术指纹。

    覆盖语义（字段无 reducer）：基于当前 state 计算新值，工具放进 ``Command(update=...)``。
    """
    return {
        "greeting_count": get_greeting_count(state) + 1,
        "sent_hashes": [*get_sent_hashes(state), hash_greeting(text)],
    }


__all__ = [
    "hash_greeting",
    "get_greeting_count",
    "get_sent_hashes",
    "is_duplicate",
    "get_pending_job",
    "sent_update",
]
