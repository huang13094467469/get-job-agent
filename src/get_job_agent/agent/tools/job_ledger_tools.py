"""岗位过目清单工具：查/登记「公司-岗位 → 结果(原因)」，跨重启持久，避免反复处理同一岗位。

compare/send_greeting 已自动维护账本；这两个工具供 Agent 主动查阅与补登异常场景。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from .. import job_ledger
from .resume_carry import _parse_job_input, _text


@tool
async def get_reviewed_jobs() -> str:
    """返回本账号「已过目清单」：每行 公司 | 岗位 → 结果（原因）。

    开始筛选、或准备沟通某岗位前先看它；凡已记录的公司-岗位，直接跳过，别再读JD/比对/沟通。
    """
    rows = job_ledger.list_reviewed(job_ledger.USER_KEY)
    if not rows:
        return "（过目清单为空，尚未处理过任何岗位）"
    lines = [
        f"- {r.get('company')} | {r.get('position')} → {r.get('result')}"
        + (f"（{r.get('reason')}）" if r.get("reason") else "")
        for r in rows
    ]
    return f"已过目 {len(rows)} 个岗位：\n" + "\n".join(lines)


@tool
async def review_job(job: str, result: str, reason: str = "") -> str:
    """把某岗位的处置结果手动登记进过目清单（供后续跳过）。

    job：browser_snapshot 的岗位对象或含公司/岗位名的文本；
    result：已打招呼 / 不匹配跳过 / 已沟通重复 / 发送失败。
    （compare 与 send_greeting 会自动登记，本工具用于补登异常或纠正记录。）
    """
    norm = _parse_job_input(job) or {}
    company = _text(norm.get("company"))
    position = _text(norm.get("position"))
    if not company and not position:
        return json.dumps({"ok": False, "error": "job 缺 company/position"}, ensure_ascii=False)
    job_ledger.record(job_ledger.USER_KEY, company, position, result or "已处理", reason)
    return json.dumps(
        {"ok": True, "reviewed": f"{company}|{position}={result}"}, ensure_ascii=False
    )


__all__ = ["get_reviewed_jobs", "review_job"]
