"""自定义工具包：简历解析/结构化抽取/校验（JsonB 入库存档）+ 求职业务工具。

浏览器操作能力已迁移到 Playwright MCP（见 agent.browser_mcp）：BROWSER_TOOLS 只保留
不依赖浏览器的业务工具（简历摘要/岗位比对/过目清单/打招呼护栏），MCP 浏览器工具由
harness 在运行时与 BROWSER_TOOLS 合并后注入 Agent。
"""

from __future__ import annotations

from .browser_ops import begin_trace
from .greeting_ops import check_greeting, confirm_greeting_sent
from .job_ledger_tools import get_reviewed_jobs, review_job
from .resume_carry import compare_job_with_resume, get_resume_summary
from .resume_extract import extract_resume
from .resume_parse import parse_resume
from .resume_validate import validate_resume

# 求职业务工具（不依赖浏览器，与 MCP 浏览器工具共同注入 Agent）
BROWSER_TOOLS = [
    get_resume_summary,
    compare_job_with_resume,
    get_reviewed_jobs,
    review_job,
    check_greeting,
    confirm_greeting_sent,
]

__all__ = [
    "BROWSER_TOOLS",
    "parse_resume",
    "extract_resume",
    "validate_resume",
    "get_resume_summary",
    "compare_job_with_resume",
    "get_reviewed_jobs",
    "review_job",
    "check_greeting",
    "confirm_greeting_sent",
    "begin_trace",
]
