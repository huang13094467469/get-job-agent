"""PostgreSQL 简历存储：resumes 表的插入、查询、更新画像。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from ..core.config import Settings
from .postgres import build_engine, build_session_factory


def _as_json(value: Any) -> str | None:
    """asyncpg 需要把 dict/list 序列化为 JSON 字符串才能写入 JSONB 列。

    若传入的已是 JSON 字符串（历史调用方误传），先解一次避免 JSONB 出现二次编码的字符串标量。
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass  # 非 JSON 文本，按字面量存储
    return json.dumps(value, ensure_ascii=False)


def _decode_profile(row: dict[str, Any]) -> dict[str, Any]:
    """把读回的 JSONB 字符串列反序列化为 dict/list。"""
    out = dict(row)
    pj = out.get("profile_json")
    if isinstance(pj, str):
        out["profile_json"] = json.loads(pj)
    return out


class ResumeStore:
    """针对 resumes 表的异步读写封装。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine = build_engine(settings)
        self._factory = build_session_factory(self._engine)

    async def insert(
        self,
        *,
        user_key: str,
        source: str,
        file_name: str | None = None,
        raw_text: str | None = None,
        profile_json: dict[str, Any] | None = None,
    ) -> int:
        """新增一条简历，返回 id。"""
        async with self._factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO resumes (user_key, source, file_name, raw_text, profile_json)
                        VALUES (:user_key, :source, :file_name, :raw_text, :profile_json)
                        RETURNING id
                        """
                    ),
                    {
                        "user_key": user_key,
                        "source": source,
                        "file_name": file_name,
                        "raw_text": raw_text,
                        "profile_json": _as_json(profile_json),
                    },
                )
            ).scalar_one()
            await session.commit()
            return int(row)

    async def update_profile(self, resume_id: int, profile_json: dict[str, Any]) -> None:
        """回写结构化画像。"""
        async with self._factory() as session:
            await session.execute(
                text("UPDATE resumes SET profile_json = :p WHERE id = :id"),
                {"p": _as_json(profile_json), "id": resume_id},
            )
            await session.commit()

    async def upsert_attachment(
        self,
        *,
        user_key: str,
        file_name: str,
        raw_text: str,
        profile_json: dict[str, Any] | None,
    ) -> int:
        """按 user_key + 附件文件名替换更新：同名已有记录则覆盖内容，否则新增。返回 id。"""
        async with self._factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT id FROM resumes
                            WHERE user_key = :uk AND file_name = :name AND source = 'attachment'
                            ORDER BY id DESC LIMIT 1
                            """
                        ),
                        {"uk": user_key, "name": file_name},
                    )
                ).scalar_one_or_none()
                if row is not None:
                    resume_id = int(row)
                    await session.execute(
                        text(
                            """
                            UPDATE resumes
                            SET raw_text = :raw, profile_json = :p
                            WHERE id = :id
                            """
                        ),
                        {"raw": raw_text, "p": _as_json(profile_json), "id": resume_id},
                    )
                    return resume_id
                resume_id = int(
                    (
                        await session.execute(
                            text(
                                """
                                INSERT INTO resumes (user_key, source, file_name, raw_text, profile_json)
                                VALUES (:user_key, 'attachment', :file_name, :raw_text, :profile_json)
                                RETURNING id
                                """
                            ),
                            {
                                "user_key": user_key,
                                "file_name": file_name,
                                "raw_text": raw_text,
                                "profile_json": _as_json(profile_json),
                            },
                        )
                    ).scalar_one()
                )
                return resume_id

    async def get(self, resume_id: int) -> dict[str, Any] | None:
        """按 id 取一条简历。"""
        async with self._factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, user_key, source, file_name, raw_text, profile_json, created_at
                        FROM resumes WHERE id = :id
                        """
                    ),
                    {"id": resume_id},
                )
            ).mappings().first()
            if not row:
                return None
            return _decode_profile(dict(row))

    async def get_by_file(self, file_name: str) -> dict[str, Any] | None:
        """按附件相对文件名查一条已解析简历（用于去重判定）。"""
        async with self._factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, user_key, source, file_name, raw_text, profile_json, created_at
                        FROM resumes WHERE file_name = :name AND source = 'attachment'
                        ORDER BY id DESC LIMIT 1
                        """
                    ),
                    {"name": file_name},
                )
            ).mappings().first()
            if not row:
                return None
            return _decode_profile(dict(row))

    async def latest_attachment(self, user_key: str) -> dict[str, Any] | None:
        """取该用户最新的附件来源简历（含画像），用于 Agent 读取摘要/比对。"""
        async with self._factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, user_key, source, file_name, raw_text, profile_json, created_at
                        FROM resumes WHERE user_key = :uk AND source = 'attachment'
                        ORDER BY id DESC LIMIT 1
                        """
                    ),
                    {"uk": user_key},
                )
            ).mappings().first()
            if not row:
                return None
            return _decode_profile(dict(row))

    async def list_by_user(self, user_key: str) -> list[dict[str, Any]]:
        """列出某用户的全部简历（含画像与原始字段）。"""
        async with self._factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, user_key, source, file_name, created_at, profile_json
                        FROM resumes WHERE user_key = :uk ORDER BY id DESC
                        """
                    ),
                    {"uk": user_key},
                )
            ).mappings().all()
            return [_decode_profile(dict(r)) for r in rows]

    async def dispose(self) -> None:
        """释放连接池。"""
        await self._engine.dispose()


__all__ = ["ResumeStore"]