"""结构化日志：基于 loguru，控制台 + 滚动文件双通道。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)

_LOG_DIR = "logs"


def setup_logging(level: str = "INFO", log_dir: str = _LOG_DIR) -> None:
    """初始化日志。重复调用安全（先清空已有 handler）。

    通道：stderr 控制台 + 滚动文件（logs/agent_YYYYMMDD.log，10MB 轮转，保留 7 天）。
    有了文件通道，关掉控制台后内容日志（LLM 输入/输出、工具调用、ContextSizeHandler
    的 token 分解）仍可事后回溯，而不再仅依赖 JSONL trace 的截断预览。
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, format=_LOG_FORMAT, colorize=True)
    logger.add(
        os.path.join(log_dir, "agent_{time:YYYYMMDD}.log"),
        level=level,
        format=_LOG_FORMAT,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
    )


__all__ = ["logger", "setup_logging"]