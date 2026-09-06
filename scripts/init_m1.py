"""M1 初始化：确保 PostgreSQL resumes 表存在（幂等）。

现在 resumes 表会在 server 启动时由 lifespan 自动创建（见 infra/postgres.ensure_resumes_table），
本脚本仅在需要手动前置确认/建表时使用；建表 SQL 与运行时为同一份（单一事实源）。

用法：
    PYTHONPATH=src python scripts/init_m1.py   # 在项目根目录执行
"""

from __future__ import annotations

import asyncio

from get_job_agent.core.config import get_settings
from get_job_agent.core.logs import logger
from get_job_agent.infra.postgres import ensure_resumes_table


async def main() -> None:
    settings = get_settings()
    await ensure_resumes_table(settings)
    logger.info("resumes 表就绪")


if __name__ == "__main__":
    asyncio.run(main())