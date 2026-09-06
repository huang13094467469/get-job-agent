"""路由汇总。"""

from __future__ import annotations

from fastapi import APIRouter

from .routes import agent, health, ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(agent.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]