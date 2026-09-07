"""简历画像构建编排：把两来源（在线 / 平台附件本地文件）串成完整流程。

流程（对齐需求 FR-1.2 / FR-1.3 / FR-1.4 / FR-1.5）：
    来源 → 取文本 → 结构化抽取(Qwen) → 校验 → 入库(resumes)

不含「上传」：附件 = server 约定目录下的本地文件（扩展负责下载放置）；
在线 = CS 读取平台简历后经 WS 传回的原始字段。
（原「→ 向量化(Qdrant)」已移除：Qdrant 只写不读、无检索路径，属无消费者负担。）
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import Settings
from ..infra.resume_store import ResumeStore
from .tools.resume_extract import extract_resume
from .tools.resume_parse import parse_resume
from .tools.resume_validate import validate_resume


async def _run_resume_pipeline(
    store: ResumeStore,
    *,
    user_key: str,
    source: str,
    raw_text: str,
    file_path: str | None = None,
    file_name: str | None = None,
) -> dict:
    """执行解析→抽取→校验→入库→向量化，返回汇总结果。

    file_path 用于读取本地附件文本（真实路径）；file_name 为落库用的相对文件名。
    """
    # 1) 取文本：附件需先经 parse_resume 读本地文件
    if source == "attachment":
        if not file_path:
            raise ValueError("附件来源必须提供 file_path")
        text = parse_resume.invoke({"file_path": file_path})
    else:
        text = raw_text

    # 2) 结构化抽取（返回的是 JSON 字符串）
    profile_str = extract_resume.invoke({"resume_text": text})

    # 3) 校验（校验器同样按 JSON 文本输入）
    check = validate_resume.invoke({"profile_json": profile_str})

    # 4) 入库（含 raw 与画像）；必须先解析成 dict 再入库，
    #    否则 ResumeStore 会对 str 再 dumps 一次 → JSONB 存成字符串标量，读取链断掉（见架构全景 A.1）
    profile_obj = json_loads(profile_str)
    if source == "attachment":
        # 附件按 user_key + 文件名替换更新（同名覆盖，无需重复 update_profile）
        resume_id = await store.upsert_attachment(
            user_key=user_key,
            file_name=file_name,
            raw_text=text,
            profile_json=profile_obj,
        )
    else:
        resume_id = await store.insert(
            user_key=user_key,
            source=source,
            file_name=file_name,
            raw_text=text,
            profile_json=profile_obj,
        )
        await store.update_profile(resume_id, profile_obj)

    return {
        "resume_id": resume_id,
        "source": source,
        "validation": json_loads(check),
    }


def json_loads(s: str) -> dict:
    import json

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"_raw": s}


async def build_from_attachment(
    settings: Settings, *, user_key: str, file_name: str
) -> dict:
    """附件来源：用户把简历放到 attachment_dir 下，file_name 为相对名。

    读取用真实路径 file_path；落库用相对 file_name，便于去重判定。
    """
    attach_dir = Path(settings.attachment_dir)
    if not attach_dir.exists():
        raise FileNotFoundError(f"附件目录不存在: {attach_dir}")
    full = attach_dir / file_name
    if not full.is_file():
        raise FileNotFoundError(f"附件不存在: {full}")
    store = ResumeStore(settings)
    try:
        return await _run_resume_pipeline(
            store,
            user_key=user_key,
            source="attachment",
            raw_text="",
            file_path=str(full),
            file_name=file_name,
        )
    finally:
        await store.dispose()


async def build_from_online(
    settings: Settings, *, user_key: str, raw_text: str
) -> dict:
    """在线来源：CS 读取平台简历回传的 raw 字段。"""
    store = ResumeStore(settings)
    try:
        return await _run_resume_pipeline(
            store,
            user_key=user_key,
            source="online",
            raw_text=raw_text,
            file_name=None,
        )
    finally:
        await store.dispose()


__all__ = ["build_from_attachment", "build_from_online", "json_loads"]