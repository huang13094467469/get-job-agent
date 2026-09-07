"""REST 端点：简历画像构建与查询。

覆盖两条来源：
- POST /resume/build     附件(本地文件名) 或 在线(raw 字段) → 解析/抽取/校验/入库
- POST /resume/upload    插件上传附件(PDF/DOCX) → 解析 → 按文件名替换更新入库
- GET  /resume/{id}      按 id 查一条简历（含画像）
- GET  /resume/list      按 user_key 列简历
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...agent.resume_service import build_from_attachment, build_from_online
from ...agent.tools.resume_parse import SUPPORTED_SUFFIXES
from ...core.config import get_settings
from ...infra.resume_store import ResumeStore
from ...schemas.common import BaseModel, Field

router = APIRouter()


class ResumeBuildRequest(BaseModel):
    """触发简历画像构建的请求体。"""

    user_key: str = Field(..., description="当前平台账号标识")
    source: Literal["online", "attachment"] = Field(..., description="简历来源")
    file_name: str | None = Field(
        None, description="附件来源：attachment_dir 下的相对文件名"
    )
    raw_text: str | None = Field(
        None, description="在线来源：CS 读取平台简历回传的结构字段文本"
    )


@router.post("/resume/build")
async def resume_build(req: ResumeBuildRequest) -> dict:
    """构建一份简历画像（附件由扩展下载到 server 约定目录后，仅传相对文件名）。"""
    settings = get_settings()
    try:
        if req.source == "attachment":
            if not req.file_name:
                raise HTTPException(400, "附件来源必须提供 file_name")
            return await build_from_attachment(
                settings, user_key=req.user_key, file_name=req.file_name
            )
        if not req.raw_text:
            raise HTTPException(400, "在线来源必须提供 raw_text")
        return await build_from_online(
            settings, user_key=req.user_key, raw_text=req.raw_text
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/resume/upload")
async def resume_upload(
    user_key: str = Form(..., description="当前平台账号标识"),
    file: UploadFile = File(..., description="简历附件（PDF/DOCX）"),
) -> dict:
    """插件上传简历附件 → 保存到 attachment_dir → 解析 → 按文件名替换更新入库。"""
    # 安全取文件名：剥离路径分隔，防止路径穿越（如 ..\\evil.pdf / ../evil.pdf）
    raw_name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not raw_name:
        raise HTTPException(400, "文件名无效")
    suffix = Path(raw_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            400, f"不支持的简历格式: {suffix}，仅支持 {'/'.join(SUPPORTED_SUFFIXES)}"
        )

    settings = get_settings()
    attach_dir = Path(settings.attachment_dir)
    attach_dir.mkdir(parents=True, exist_ok=True)
    dest = attach_dir / raw_name
    content = await file.read()
    if not content:
        raise HTTPException(400, "上传内容为空")
    dest.write_bytes(content)

    try:
        return await build_from_attachment(
            settings, user_key=user_key, file_name=raw_name
        )
    except Exception as exc:  # noqa: BLE001 解析/抽取/入库失败，统一转为 500 便于前端提示
        raise HTTPException(500, f"简历解析失败: {exc}") from exc


@router.get("/agent/run-config")
async def agent_run_config() -> dict:
    """返回 Agent 运行默认配置（供插件面板初始化无人值守开关与投递上限）。"""
    s = get_settings()
    return {
        "agent_mode": s.agent_mode,
        "max_greetings_per_run": int(s.max_greetings_per_run or 0),
    }


@router.get("/resume/list")
async def resume_list(user_key: str) -> dict:
    """列出某账号的全部简历（含画像）。"""
    store = ResumeStore(get_settings())
    try:
        rows = await store.list_by_user(user_key)
        return {"items": rows}
    finally:
        await store.dispose()


@router.get("/resume/{resume_id}")
async def resume_get(resume_id: int) -> dict:
    """按 id 查询单条简历。"""
    store = ResumeStore(get_settings())
    try:
        row = await store.get(resume_id)
        if not row:
            raise HTTPException(404, f"resume {resume_id} 不存在")
        return row
    finally:
        await store.dispose()


__all__ = ["router"]