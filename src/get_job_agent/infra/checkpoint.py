"""持久 checkpointer：PostgreSQL（AsyncPostgresSaver），替代易失的 MemorySaver。

对齐 going-to-production.md「Durability」：checkpoint 支撑崩溃续跑、time travel、以及
HITL「暂停数分钟到数天后精确恢复」。复用项目已有 Postgres 实例，但**单独建一个库**
（settings.checkpoint_db，默认 get_job_agent_ckpt）与业务数据隔离；表由 saver.setup() 自动建。

生命周期：在 FastAPI lifespan 里 ``await init_checkpointer(settings)`` 建库+连接池+建表，
关闭时 ``await close_checkpointer()`` 释放连接池。harness.get_checkpointer() 取此单例；
未初始化（如离线测试）时由 harness 兜底 MemorySaver。

Windows 注意：psycopg3 的异步实现不支持 ProactorEventLoop，进程须使用 SelectorEventLoop
（在 main.py 入口设置 WindowsSelectorEventLoopPolicy）。
"""

from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..core.logs import logger

# 进程级单例：连接池 + saver。由 lifespan 初始化/关闭，harness 读取。
_POOL: Any = None
_SAVER: Any = None


async def _ensure_database(settings: Settings) -> None:
    """连到 postgres 维护库，CREATE DATABASE（已存在则跳过）。

    setup() 只建表不建库，故库需预先创建。用 autocommit 连接（CREATE DATABASE 不能在事务里）。
    """
    import psycopg

    maint_dsn = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )
    async with await psycopg.AsyncConnection.connect(maint_dsn, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.checkpoint_db,)
        )
        if await cur.fetchone():
            logger.debug("checkpoint 数据库 {} 已存在", settings.checkpoint_db)
            return
        await conn.execute(f'CREATE DATABASE "{settings.checkpoint_db}"')
        logger.info("已创建 checkpoint 数据库 {}", settings.checkpoint_db)


async def init_checkpointer(settings: Settings) -> Any:
    """初始化持久 Postgres checkpointer：建库 → 连接池 → AsyncPostgresSaver → setup() 建表。

    幂等：已初始化则直接返回单例。失败时抛异常，由调用方（lifespan）决定降级策略。
    """
    global _POOL, _SAVER
    if _SAVER is not None:
        return _SAVER

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    await _ensure_database(settings)
    # prepare_threshold=0：禁用 prepared statement，规避连接池/代理下的语句缓存失效问题。
    _POOL = AsyncConnectionPool(
        conninfo=settings.checkpoint_dsn,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    await _POOL.open()
    _SAVER = AsyncPostgresSaver(_POOL)
    await _SAVER.setup()  # 幂等建表：checkpoints / blobs / writes / migrations
    logger.info("Postgres checkpointer 就绪 db={} tables=setup()", settings.checkpoint_db)
    return _SAVER


async def close_checkpointer() -> None:
    """释放连接池（app 关闭时调用）。"""
    global _POOL, _SAVER
    if _POOL is not None:
        try:
            await _POOL.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭 checkpoint 连接池异常: {}", exc)
    _POOL = None
    _SAVER = None
    logger.info("Postgres checkpointer 已关闭")


def get_checkpointer() -> Any:
    """返回已初始化的持久 saver 单例；未初始化返回 None（由 harness 兜底 MemorySaver）。"""
    return _SAVER


__all__ = ["init_checkpointer", "close_checkpointer", "get_checkpointer"]
