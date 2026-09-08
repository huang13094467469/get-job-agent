"""浏览器工具调用重试中间件：对「瞬时连接类」软错误自动退避重试。

浏览器操作已迁移到 Playwright MCP：工具失败多以文本错误返回（不抛异常），
且 MCP 工具不会返回 {ok:false, error} 的结构化 JSON。本中间件：
- 从工具返回文本中识别连接类瞬时错误（not connected / timeout 等关键词）；
- 仅对「只读」MCP 工具（browser_snapshot / browser_find）重试；
  写类（click/type/press_key/navigate）不重试，可能已生效。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest

from ..core.logs import logger

# 纯读、可安全超时重试的浏览器工具
_TIMEOUT_RETRYABLE_TOOLS = {"browser_snapshot", "browser_find"}

# 连接类瞬时错误的文本特征（Playwright MCP 未连/页面未开/超时等）
_TRANSIENT_MARKERS = (
    "not connected",
    "no connection",
    "browser is not connected",
    "timed out",
    "timeout",
    "connection refused",
    "target page, context or browser has been closed",
)


def _soft_error(result: Any) -> str:
    """从工具返回里取软错误码：优先结构化 {ok:false,error}，否则按文本特征识别。"""
    content = getattr(result, "content", None)
    if not isinstance(content, str):
        return ""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and obj.get("ok") is False:
            return str(obj.get("error") or "")
    except Exception:  # noqa: BLE001
        pass
    low = content.lower()
    for marker in _TRANSIENT_MARKERS:
        if marker in low:
            return "transient:" + marker
    return ""


class BrowserToolRetryMiddleware(AgentMiddleware):
    """wrap_tool_call：瞬时错误退避重试（默认最多 2 次、线性退避 2s/4s）。"""

    name = "BrowserToolRetryMiddleware"

    def __init__(self, max_retries: int = 2, backoff_s: float = 2.0) -> None:
        self.max_retries = max_retries
        self.backoff_s = backoff_s

    def _should_retry(self, tool: str, args: dict, err: str) -> bool:
        if not err:
            return False
        if err.startswith("transient:"):
            return tool in _TIMEOUT_RETRYABLE_TOOLS  # 只读工具才重试，避免重复副作用
        return False

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        tc = request.tool_call
        tool = str(tc.get("name") or "")
        args = tc.get("args") or {}
        attempt = 0
        while True:
            result = await handler(request)
            err = _soft_error(result)
            if attempt < self.max_retries and self._should_retry(tool, args, err):
                attempt += 1
                delay = self.backoff_s * attempt
                logger.info(
                    "[retry] 工具 {} 瞬时失败({})，{}s 后第 {}/{} 次重试",
                    tool, err, delay, attempt, self.max_retries,
                )
                await asyncio.sleep(delay)
                continue
            return result


__all__ = ["BrowserToolRetryMiddleware"]
