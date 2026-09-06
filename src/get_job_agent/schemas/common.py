"""通用 API 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ComponentStatus(BaseModel):
    """单个基础设施组件的连通状态。"""

    ok: bool
    detail: str = ""


class HealthResponse(BaseModel):
    """GET /health 响应体。"""

    status: str
    version: str
    components: dict[str, ComponentStatus]


__all__ = ["BaseModel", "Field", "ComponentStatus", "HealthResponse"]