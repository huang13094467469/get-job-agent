"""简历目录启动解析：扫描 attachment_dir（server/jianli）下的简历，未解析则自动构建。

供两个入口复用：
- main.py lifespan 后台任务（启动自动扫描）
- ws.py `resume_build_dir` 处理（Side Panel 手动触发重扫）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agent.resume_service import build_from_attachment
from ..core.config import Settings
from ..core.logs import logger
from ..infra.resume_store import ResumeStore

_SCAN_SUFFIXES = {".pdf", ".docx"}


def _iter_resume_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        # 跳过隐藏 / 临时 / 备份文件
        if name.startswith(".") or name.startswith("~"):
            continue
        if p.suffix.lower() in _SCAN_SUFFIXES:
            out.append(p)
    return out


async def scan_resume_dir(settings: Settings, user_key: str = "default") -> dict[str, Any]:
    """扫描并构建简历，返回汇总 {scanned, skipped, failed, notes}。"""
    directory = Path(settings.attachment_dir)
    files = _iter_resume_files(directory)
    scanned: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    store = ResumeStore(settings)
    try:
        for f in files:
            rel = f.name
            try:
                existing = await store.get_by_file(rel)
            except Exception as exc:  # noqa: BLE001
                failed.append({"file": rel, "reason": f"去重查询失败: {exc}"})
                continue
            if existing:
                skipped.append(rel)
                continue
            try:
                await build_from_attachment(settings, user_key=user_key, file_name=rel)
                scanned.append(rel)
            except Exception as exc:  # noqa: BLE001
                logger.warning("简历解析失败 {}: {}", rel, exc)
                failed.append({"file": rel, "reason": str(exc)})
    finally:
        await store.dispose()

    logger.info(
        "扫描简历目录 {}: 解析成功 {} / 跳过 {} / 失败 {}",
        directory,
        len(scanned),
        len(skipped),
        len(failed),
    )
    return {
        "directory": str(directory),
        "scanned": scanned,
        "skipped": skipped,
        "failed": failed,
        "ok": not failed,
    }


__all__ = ["scan_resume_dir"]