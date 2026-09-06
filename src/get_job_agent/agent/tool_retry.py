"""浏览器工具调用重试中间件：对「瞬时连接类」软错误自动退避重试。

deepagents/langchain 自带的 ToolRetryMiddleware 只在工具「抛异常」时重试；而本项目
浏览器工具是把失败作为结构化结果（ok:false）返回的软错误，故需自定义一层 wrap_tool_call
来识别并重试。安全策略（避免重复副作用）：
- page_not_connected：动作根本没到达页面 → 任意浏览器工具都可重试（等页内 ~2s 重连）；
- page_action_timeout：仅对「只读」调用重试（browser_snapshot / browser_act op=wait）；
  写类（click/type/send_greeting/navigate/go_back）不重试，可能已生效或由其自身处理。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest

from ..core.logs import logger

# 纯读、可安全超时重试的浏览器工具 / op
_TIMEOUT_RETRYABLE_TOOLS = {"browser_snapshot"}
_TIMEOUT_RETRYABLE_ACT_OPS = {"wait"}


def _soft_error(result: Any) -> str:
    """从工具返回的 ToolMessage 里取软错误码；非结构化 ok:false 则返回 ''。"""
    content = getattr(result, "content", None)
    if not isinstance(content, str):
        return ""
    try:
        obj = json.loads(content)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(obj, dict) and obj.get("ok") is False:
        return str(obj.get("error") or "")
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
        if err.startswith("page_not_connected"):
            return True  # 未到达页面，任何浏览器工具都可安全重试
        if err == "page_action_timeout":
            if tool in _TIMEOUT_RETRYABLE_TOOLS:
                return True
            return tool == "browser_act" and str(args.get("op")) in _TIMEOUT_RETRYABLE_ACT_OPS
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
