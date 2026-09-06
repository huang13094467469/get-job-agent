"""GET /health：服务与基础设施连通检测。

PostgreSQL（业务库 + checkpoint）是本系统唯一必需的外置基础设施，故只探其连通。
（Ollama 本地 embedding / Qdrant 向量已随「只写不读」的简历向量化管线一并移除。）
"""

from __future__ import annotations

from fastapi import APIRouter

from ...core.config import get_settings
from ...infra import postgres
from ...schemas.common import ComponentStatus, HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """返回服务版本及各基础设施连通状态。

    任一组件不可用即返回 degraded（不视为服务不可用，便于前端降级提示）。
    """
    settings = get_settings()
    ok_postgres = await postgres.check_postgres(settings)
    return HealthResponse(
        status="ok" if ok_postgres else "degraded",
        version="0.1.0",
        components={
            "postgres": ComponentStatus(ok=ok_postgres),
        },
    )


__all__ = ["router"]