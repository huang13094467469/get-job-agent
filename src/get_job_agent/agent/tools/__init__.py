"""自定义工具包：简历解析/结构化抽取/校验（JsonB 入库存档）+ 浏览器操作（M5）。

简历工具（parse/extract/validate）由 resume_service 直接 .invoke 编排；
本包仅向 Agent 暴露浏览器操作工具集 BROWSER_TOOLS。
"""

from __future__ import annotations

from .browser_ops import (
    begin_trace,
    browser_act,
    browser_snapshot,
    send_greeting,
    start_chat,
)
from .job_ledger_tools import get_reviewed_jobs, review_job
from .resume_carry import compare_job_with_resume, get_resume_summary
from .resume_extract import extract_resume
from .resume_parse import parse_resume
from .resume_validate import validate_resume

# M5/M6 computer-use 浏览器操作工具集（含简历摘要 + 岗位逐模块比对 + 打招呼发送 + 过目清单）
# send_greeting 是写类敏感工具，在 create_deep_agent 中通过 interrupt_on 挂人工确认。
BROWSER_TOOLS = [
    browser_snapshot,
    browser_act,
    start_chat,
    get_resume_summary,
    compare_job_with_resume,
    get_reviewed_jobs,
    review_job,
    send_greeting,
]

__all__ = [
    "BROWSER_TOOLS",
    "parse_resume",
    "extract_resume",
    "validate_resume",
    "browser_snapshot",
    "browser_act",
    "start_chat",
    "send_greeting",
    "get_resume_summary",
    "compare_job_with_resume",
    "get_reviewed_jobs",
    "review_job",
    "begin_trace",
]