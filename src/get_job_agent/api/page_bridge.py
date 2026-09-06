"""页操作桥：面向 Server 内部编排（Agent 工具）的「发指令→等回执」async 原语。

现状：页内 CS 的 `dom_result` 仅能按 idem 回发给发起面板；DeepAgent 工具不是面板，
需要一个可在同事件循环内 await、等页面回执再返回的通道。本桥即为此而生：

    idem -> asyncio.Future 账本；`act()` 发指令后 await，`resolve()` 在 dom_result
    到达时填充 Future。单事件循环内调用，无需加锁。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..core.logs import logger

# dom_result 里 "result" 即 CS dispatch 的 {ok:true,data}|{ok:false,error}
_RESULT_TIMEOUT = 15.0


class PageNotConnectedError(RuntimeError):
    """目标页面 Content Script 不在线。"""


class _Pending:
    __slots__ = ("fut", "created", "sent_at")

    def __init__(self, fut: asyncio.Future) -> None:
        self.fut = fut
        self.created = time.monotonic()
        self.sent_at = time.monotonic()


class PageActionBridge:
    def __init__(self) -> None:
        self._pending: dict[str, dict[str, _Pending]] = {}
        self._pages_fn: Callable[[], dict[str, Any]] | None = None

    def bind_pages(self, fn: Callable[[], dict[str, Any]]) -> None:
        """注入 `_pages` 访问函数（由 hub 调用），用于离线判定的在线表。"""
        self._pages_fn = fn

    def _pages(self) -> dict[str, Any]:
        return self._pages_fn() if self._pages_fn else {}

    async def act(
        self,
        page_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _RESULT_TIMEOUT,
        route: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """发一条 DOM 指令到页内 CS 并等待回执。

        页面离线抛 PageNotConnectedError；超时抛 TimeoutError。
        `route` 由调用方注入（hub.route_to_page），避免桥反向依赖 ws 模块。
        """
        if page_id not in self._pages():
            raise PageNotConnectedError(page_id)

        idem = f"pgact:{page_id}:{uuid.uuid4().hex}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending.setdefault(page_id, {})[idem] = _Pending(fut)

        msg = {
            "from": "server",
            "type": "dom",
            "to": page_id,
            "idem": idem,
            "payload": {"action": action, "params": params or {}},
        }
        try:
            if route is not None:
                await route(page_id, msg)
            else:
                raise RuntimeError("PageActionBridge 未注入 route 函数")
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._discard(page_id, idem)
            logger.warning("页操作超时 page_id={} action={}", page_id, action)
            raise
        except asyncio.CancelledError:
            self._discard(page_id, idem)
            raise

    def resolve(self, page_id: str, idem: str, result: Any) -> bool:
        """dom_result 到达：命中 pending 则填充 Future，返回真（已消费）；否则假。"""
        bucket = self._pending.get(page_id)
        if not bucket or idem not in bucket:
            return False
        pend = bucket.pop(idem)
        if not bucket:
            self._pending.pop(page_id, None)
        if not pend.fut.done():
            pend.fut.set_result(result)
        return True

    def drop_page(self, page_id: str) -> None:
        """页面断开：把所有 pending 置为异常结果，避免 await 悬挂。"""
        bucket = self._pending.pop(page_id, None)
        if not bucket:
            return
        for pend in bucket.values():
            if not pend.fut.done():
                pend.fut.set_exception(PageNotConnectedError(page_id))

    def _discard(self, page_id: str, idem: str) -> None:
        bucket = self._pending.get(page_id)
        if bucket:
            bucket.pop(idem, None)
            if not bucket:
                self._pending.pop(page_id, None)


__all__ = ["PageActionBridge", "PageNotConnectedError"]