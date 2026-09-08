"""打招呼护栏工具（纯逻辑，不操作页面）：发送预检 + 发送后登记。

背景：浏览器操作已迁移到 Playwright MCP 工具，send_greeting 的「发送」动作由模型用
MCP 工具完成（browser_click/type/press_key）；但投递上限/话术去重/成功登记等护栏
若也交给模型自觉，可靠性会下降。这里拆成两个**确定性纯逻辑工具**，由 SOP 强制串联：

1. ``check_greeting``：发送前预检（上限/去重/长度/岗位状态），ok=true 才允许发。
2. ``confirm_greeting_sent``：发送成功（页面确认输入框已清空）后登记计数/指纹/岗位。

护栏状态（计数/指纹/暂存岗位）仍从 ``runtime.state`` 读、经 ``Command(update)`` 写回，
随 thread checkpoint 持久，与先前 send_greeting 口径一致。
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from .. import job_ledger
from .. import runtime_guard as rg
from ...core.config import get_settings

_GREETING_LIMIT = 300  # 打招呼话术长度上限护栏


def _cmd(tool_name: str, runtime: ToolRuntime, payload: dict[str, Any],
         **state_update: Any) -> Command:
    """把结果包成 ToolMessage，并附带 state 更新（计数/去重），统一经 Command 返回。"""
    update: dict[str, Any] = dict(state_update)
    update["messages"] = [ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=runtime.tool_call_id, name=tool_name,
    )]
    return Command(update=update)


@tool
async def check_greeting(text: str, runtime: ToolRuntime) -> str:
    """打招呼发送前的护栏预检（不操作页面）。

    在拟好话术后、调用浏览器发送前，必须先调本工具。它检查：
    - 话术非空、长度合规；
    - 未达本轮投递上限；
    - 话术未重复发送过；
    - 当前暂存岗位尚未打过招呼。
    返回 {ok:true} 表示可发送；{ok:false,error,...} 给出具体拦截原因与建议，
    此时不要强行发送，按 error 处理（如达到上限则总结并结束）。
    """
    state = runtime.state
    text = (text or "").strip()
    if not text:
        return _cmd("check_greeting", runtime, {"ok": False, "error": "greeting_text_empty"})
    if len(text) > _GREETING_LIMIT:
        return _cmd("check_greeting", runtime, {
            "ok": False, "error": f"greeting_too_long(>{_GREETING_LIMIT}字)"})
    count = rg.get_greeting_count(state)
    cap = rg.get_max_greetings(state, int(get_settings().max_greetings_per_run or 0))
    if cap > 0 and count >= cap:
        return _cmd("check_greeting", runtime, {
            "ok": False, "error": "greeting_cap_reached", "cap": cap, "sent": count,
            "suggest": f"已达本轮投递上限({cap})，停止发送。请总结已投递岗位并结束。"})
    if rg.is_duplicate(state, text):
        return _cmd("check_greeting", runtime, {
            "ok": False, "error": "duplicate_greeting",
            "suggest": "完全相同的话术本会话已发过，别重复发送；去处理下一个岗位。"})
    job = rg.get_pending_job(state)
    if job and job_ledger.is_terminal(
        job_ledger.get_entry(job_ledger.USER_KEY, job.get("company"), job.get("position"))
    ):
        return _cmd("check_greeting", runtime, {
            "ok": False, "error": "already_greeted",
            "target": f"{job.get('company')}|{job.get('position')}",
            "suggest": "该岗位已打过招呼，跳过它、去处理下一个岗位，别再重复发送。"})
    return _cmd("check_greeting", runtime, {
        "ok": True, "sent": count, "cap": cap,
        "suggest": "通过预检，请用浏览器工具发送：browser_click 聚焦输入框 → browser_type 输入话术 → browser_press_key Enter。"})


@tool
async def confirm_greeting_sent(text: str, runtime: ToolRuntime) -> str:
    """确认打招呼已成功发送后登记（不操作页面）。

    必须在你用浏览器工具发送、并已通过页面状态确认「输入框已清空」（发送成功）后调用。
    它完成发送后的登记：投递计数 +1、话术指纹入库（防重复）、把暂存岗位升级为「已打招呼」。
    不要臆测发送成功——以页面确认结果为准，未确认发送不要调用。
    """
    state = runtime.state
    text = (text or "").strip()
    if not text:
        return _cmd("confirm_greeting_sent", runtime, {"ok": False, "error": "greeting_text_empty"})
    upd = rg.sent_update(state, text)  # {greeting_count: n+1, sent_hashes: [...]}
    job = rg.get_pending_job(state)
    reviewed = None
    if job:
        job_ledger.record(job_ledger.USER_KEY, job.get("company"), job.get("position"),
                          "已打招呼", link=job.get("link"))
        reviewed = f"{job.get('company')}|{job.get('position')}=已打招呼"
    return _cmd("confirm_greeting_sent", runtime, {
        "ok": True, "sent_total": upd["greeting_count"], "reviewed": reviewed,
    }, **upd)


__all__ = ["check_greeting", "confirm_greeting_sent"]
