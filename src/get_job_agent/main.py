"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .api.router import api_router
from .core.config import get_settings
from .core.logs import logger, setup_logging
from .jobs.resume_startup import scan_resume_dir

# psycopg3（AsyncPostgresSaver）异步实现不支持 Windows 默认的 ProactorEventLoop，须在事件循环创建前
# 切到 SelectorEventLoop（评估 P0-1 持久 checkpointer 的运行前提）。在 app 模块导入期设置，覆盖直接
# `uvicorn get_job_agent.main:app` 启动与测试；根 main.py 另在进程入口设置以覆盖更早的时序。
if sys.platform == "win32":  # pragma: no cover - 平台相关
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def _run_startup_resume_scan() -> None:
    """后台扫描简历目录（不阻塞启动），日志记录成功/跳过/失败。"""
    try:
        await scan_resume_dir(get_settings())
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动简历扫描异常: {}", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    from .agent.observability import init_tracing
    from .infra.checkpoint import close_checkpointer, init_checkpointer
    from .infra.postgres import ensure_resumes_table

    init_tracing(settings)
    # P0-1：初始化持久 Postgres checkpointer（单独建库 + 连接池 + setup 建表）。失败则降级——
    # harness.get_checkpointer() 兜底 MemorySaver，保证 Postgres 暂不可达时 server 仍能启动。
    try:
        await init_checkpointer(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Postgres checkpointer 初始化失败，降级 MemorySaver（重启丢会话/HITL 状态）: {}", exc
        )
    # 幂等建业务表 resumes（不再依赖手动 init_m1）。业务库本身须预配置于 POSTGRES_DB。
    try:
        await ensure_resumes_table(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("resumes 表初始化失败（简历链路可能不可用，先服务其它能力）: {}", exc)
    logger.info("Get-Job Agent Server 启动 (v{})", __version__)
    asyncio.create_task(_run_startup_resume_scan())
    yield
    await close_checkpointer()
    logger.info("Get-Job Agent Server 关闭")


def create_app() -> FastAPI:
    """应用工厂，便于测试注入。"""
    app = FastAPI(
        title="Get-Job Agent Server",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()

__all__ = ["app", "create_app"]