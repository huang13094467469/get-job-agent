"""PostgreSQL 接入：SQLAlchemy async engine / session、连通自检、幂等建表。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..core.config import Settings
from ..core.logs import logger

# resumes 表的唯一定义（单一事实源，供 lifespan 自动建表与 scripts/init_m1.py 复用）
CREATE_RESUMES = """
CREATE TABLE IF NOT EXISTS resumes (
    id           BIGSERIAL PRIMARY KEY,
    user_key     TEXT NOT NULL,
    source       TEXT NOT NULL,
    file_name    TEXT,
    raw_text     TEXT,
    profile_json JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
CREATE_RESUMES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_resumes_user_key ON resumes (user_key);
"""


def build_engine(settings: Settings) -> AsyncEngine:
    """构建异步 SQLAlchemy engine。"""
    return create_async_engine(settings.postgres_dsn, echo=False, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """基于 engine 构建 session 工厂。"""
    return async_sessionmaker(engine, expire_on_commit=False)


async def ensure_resumes_table(settings: Settings) -> None:
    """幂等建业务表：resumes + user_key 索引（不存在才建）。

    在 FastAPI lifespan 里调用，使首次启动无需手动跑 init_m1。
    """
    engine = build_engine(settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(CREATE_RESUMES))
            await conn.execute(text(CREATE_RESUMES_INDEX))
    finally:
        await engine.dispose()
    logger.info("resumes 表就绪（幂等）")


async def check_postgres(settings: Settings) -> bool:
    """连通自检：执行 SELECT 1。"""
    engine = create_async_engine(settings.postgres_dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL 连通失败: {}", exc)
        return False
    finally:
        await engine.dispose()


__all__ = [
    "build_engine",
    "build_session_factory",
    "check_postgres",
    "ensure_resumes_table",
    "CREATE_RESUMES",
    "CREATE_RESUMES_INDEX",
]