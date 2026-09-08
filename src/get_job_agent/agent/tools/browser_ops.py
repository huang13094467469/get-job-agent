"""浏览器运行轨迹缓冲（保留的轻量观测设施）。

浏览器操作工具已迁移到 Playwright MCP（见 agent/browser_mcp），本模块仅保留
``begin_trace``：供 ws.py 在每段 agent 运行开始时开启一次轨迹记录（ContextVar），
供 observability 等消费；页面操作本身不再经 Content Script 桥。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_trace: ContextVar[list[str] | None] = ContextVar("browser_trace", default=None)


def begin_trace() -> list[str]:
    """开启一次运行期轨迹记录，返回缓冲区。"""
    buf: list[str] = []
    _trace.set(buf)
    return buf


def get_trace() -> list[str] | None:
    """读取当前上下文轨迹（无则返回 None）。"""
    return _trace.get()


__all__ = ["begin_trace", "get_trace"]
