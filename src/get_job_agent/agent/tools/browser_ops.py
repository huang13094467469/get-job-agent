"""M5 computer-use 浏览器操作工具：让 DeepAgent 自主「看快照 → 决策 → 执行原语」。

实现走 `hub.bridge` 把指令经 WS 路由到页内 Content Script 的 `snapshot`/`act`，
并在同一事件循环内 await 回执。写类操作（click/type/select/press）标记为危险写工具，
便于审计与未来挂确认钩子（默认为直接执行，满足自主）。
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from ...api.page_bridge import PageNotConnectedError
from .. import job_ledger
from .. import runtime_guard as rg

_PAGE_ID = "zhipin"  # 与 content.js PAGE_ID 对齐
_TEXT_LIMIT = 500  # type 文本上限护栏
_GREETING_LIMIT = 300  # 打招呼话术长度上限护栏
_ACT_NEW_ELEMENTS_CAP = 40  # browser_act 回填只保留本步新增/变化元素的上限
# 关键动作按钮：即便未被标 new，也要让模型看到（防“继续沟通”这类随机弹框被 delta 去重藏掉）
_ACT_KEYWORD_BUTTONS = ("继续沟通", "立即沟通", "发送", "取消", "确认", "确定")

# 运行期「动作→结果」轨迹缓冲，供 agent_go 收尾回发
_trace: ContextVar[list[str]] = ContextVar("browser_trace", default=None)


def begin_trace() -> list[str]:
    """开启一次运行期轨迹记录，返回缓冲区（tools 会往里 push）。"""
    buf: list[str] = []
    _trace.set(buf)
    return buf


def _trace_push(entry: str) -> None:
    buf = _trace.get()
    if buf is not None:
        buf.append(entry)


async def _bridge_act(
    action: str, params: dict[str, Any], *, timeout: float = 20.0
) -> dict[str, Any]:
    """经 hub.bridge 向页内 CS 发指令并等回执；页面离线/异常转为结构化文案。"""
    from ...api.routes.ws import hub  # 懒加载，避免 import 环

    try:
        return await hub.bridge.act(
            _PAGE_ID, action, params, route=hub.route_to_page, timeout=timeout
        )
    except PageNotConnectedError:
        return {
            "ok": False,
            "error": f"page_not_connected:{_PAGE_ID}",
            "suggest": "请先打开 Boss 页面并保持在前台",
        }
    except TimeoutError:
        return {"ok": False, "error": "page_action_timeout", "suggest": "页面可能卡顿，请重试"}


def _is_reviewed(company: Any, position: Any) -> bool:
    """该「公司|岗位」是否已在过目清单且为终态（据此从列表快照中过滤）。"""
    return job_ledger.is_terminal(job_ledger.get_entry(job_ledger.USER_KEY, company, position))


def _prune_snapshot(snap: dict[str, Any], *, compact: bool) -> None:
    """就地精简一个快照 dict：过滤已过目岗位；compact 时只留本步新增/变化元素。

    compact=True 用于 browser_act 回填（模型刚看过页面，只需知道“变了什么”）；
    compact=False 用于 browser_snapshot（显式“看整页”，保留全量元素但过滤已处理岗位）。
    """
    jobs = snap.get("jobs")
    if isinstance(jobs, list):
        snap["jobs"] = [j for j in jobs if not _is_reviewed(j.get("company"), j.get("name"))]
    els = snap.get("elements")
    if isinstance(els, list):
        if compact:
            snap["element_total"] = len(els)
            # 保留：本步新增/变化元素 ∪ 关键动作按钮（按 ref 去重、保 DOM 顺序）
            picked: dict[str, Any] = {}
            for e in els:
                txt = str(e.get("text") or "")
                if e.get("new") or any(k in txt for k in _ACT_KEYWORD_BUTTONS):
                    picked[str(e.get("ref"))] = e
            snap["elements"] = list(picked.values())[:_ACT_NEW_ELEMENTS_CAP]
        else:
            snap["elements"] = els[:80]


def _fmt_result(res: dict[str, Any]) -> str:
    _trace_push(json.dumps({"page": True, "action": res.get("_action"), "ok": res.get("ok")},
                           ensure_ascii=False))
    # 控制送入模型的上下文：browser_snapshot 过滤已处理岗位；browser_act 回填只留变化（delta）。
    data = res.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("elements"), list):
            _prune_snapshot(data, compact=False)          # 顶层快照：browser_snapshot
        updated = data.get("updated")
        if isinstance(updated, dict) and isinstance(updated.get("elements"), list):
            _prune_snapshot(updated, compact=True)         # act 回填：精简为变化量
    return json.dumps(res, ensure_ascii=False)


@tool
async def browser_snapshot(goal_hint: str | None = None) -> str:
    """读取当前 Boss 页面的可访问性快照：可见可交互元素清单（含稳定 ref 与文本）。

    每次决策前调用它「看」页面。元素带 ref，若需操作就用 browser_act 作用到该 ref。
    goal_hint 仅用于给模型提示本次目标，可忽略。
    """
    res = await _bridge_act("snapshot", {"max": 80})
    res["_action"] = "snapshot"
    return _fmt_result(res)


@tool
async def browser_act(
    op: str,
    ref: str = "",
    text: str | None = None,
    match_text: str | None = None,
    within_text: str | None = None,
    occurrence: int = 0,
) -> str:
    """在当前 Boss 页面执行一步浏览器操作，并返回**精简的变化量**（仅本步新增/变化元素）。

    不要为“看一眼”而在 act 前后又多调 browser_snapshot：操作后页面变化已含在返回里。
    需要重新看整页（如导航后、或不确定页面结构）时才显式 browser_snapshot。

    op ∈ {click, type, select, scroll, press, go_back, navigate, wait, load_more}：
      - click: 点按 ref 指向的元素（如按钮/链接/职位卡片，含【立即沟通】【继续沟通】）
      - type: 在 ref 指向的输入框输入 text
      - select: 在 ref 指向的下拉框选择 text
      - scroll: 滚动（**无需 ref**，自动滚岗位列表的内层容器；text='up' 向上否则向下）
      - press: 向 ref 元素派发按键（默认 Enter）
      - go_back: 浏览器后退（ref 传空串即可，用于发送话术后返回岗位列表）
      - navigate: 跳转到 text 指定的 URL（ref 传空串即可，用于回到列表页）
      - wait: 主动等待页面就绪，可配 text（等某文案出现）/ref 无关，适合点【立即沟通】后等聊天页加载
      - load_more: 把岗位列表滚到底**触发无限加载下一页**；返回 before/after/added 与新 jobs。
        当前可见岗位处理完/被过滤空了、还需更多候选时，用 load_more 而不是 navigate。
    稳健定位（强烈建议用于【立即沟通】/岗位卡，抗 SPA 重渲染导致的 ref 漂移）：
      当 ref 定位不到时，可省略 ref 并给 match_text（目标元素应包含的文字，如「立即沟通」），
      再用 within_text（目标所在岗位卡片的区分文字，如岗位名「Java工程师」）锁定具体那一张，
      occurrence 为同命中多个时取第几个（0 基）。页面会自动按当前 DOM 找到可点击元素。
    这是「写」类操作工具，会真实触发页面交互。
    """
    params: dict[str, Any] = {"op": op, "ref": ref, "text": text}
    if match_text:
        params["match_text"] = match_text
    if within_text:
        params["within_text"] = within_text
    if occurrence:
        params["occurrence"] = occurrence
    if op == "type" and text and len(text) > _TEXT_LIMIT:
        return json.dumps({"ok": False, "error": "text_too_long"}, ensure_ascii=False)
    res = await _bridge_act("act", params)
    tag = f"act:{op}@{ref or match_text or ''}"
    res["_action"] = tag
    return _fmt_result(res)


@tool
async def start_chat(within_text: str | None = None, occurrence: int = 0) -> str:
    """一步进入某岗位的聊天页（确定性处理【立即沟通】→弹框【继续沟通】→聊天态）。

    沟通某岗位前先调它，替代手点的脆弱多步：内部点【立即沟通】，若弹【已向BOSS发送消息】就点
    【继续沟通】，轮询到聊天页。within_text=岗位名（锁定该卡片的立即沟通），occurrence 可选。
    返回：reached_chat=true 即已在聊天页，可直接 send_greeting；phase=navigating/confirm_clicked
    时先 browser_snapshot 确认到聊天页再发；no_immediate_chat_button 表示该岗位无按钮/已沟通。
    """
    params: dict[str, Any] = {}
    if within_text:
        params["within_text"] = within_text
    if occurrence:
        params["occurrence"] = occurrence
    res = await _bridge_act("start_chat", params, timeout=12.0)
    res["_action"] = "start_chat"
    # 超时/瞬断 ≠ “该岗位无按钮/不匹配”：点【立即沟通】/【继续沟通】可能已生效、页面在跳转。
    # 给模型明确的“先验证再继续、别跳过本岗位”指令，避免误放弃已达标岗位。
    err = str(res.get("error") or "")
    if err == "page_action_timeout" or err.startswith("page_not_connected"):
        res["ok"] = False
        res["retry"] = True
        res["do_not_skip"] = True
        res["hint"] = (
            "start_chat 超时/瞬断不代表该岗位无按钮或不匹配——多半是点击已生效、页面正在跳转。"
            "下一步先 browser_snapshot 检查：若 page=='chat' 且有输入框，就直接 send_greeting；"
            "若还没到聊天页，再重试 start_chat（最多 2 次）。绝不因超时而跳过该岗位或转去下一个。"
        )
    return _fmt_result(res)


@tool
async def send_greeting(text: str, runtime: ToolRuntime) -> Command:
    """向当前岗位 HR 发送打招呼话术（真实发送，写类敏感操作）。

    这是唯一会把话术发给 HR 的工具。在 confirm 模式下调用它会触发人工确认（发送前暂停）；
    在 unattended（无人值守）模式下会直接自动发送。无论哪种模式，系统都会按投递上限护栏：
    达到上限时本工具会拒发并返回 greeting_cap_reached，此时请停止投递、总结已投递岗位并结束。
    你只需把为当前岗位拟好的、突出匹配点的话术作为 text 传入，以工具返回为准。

    内部走 content.js 的 `send_greeting` DOM 原语：定位打招呼输入框 → 输入 → 点发送。
    调用前必须已进入聊天页（URL 含 /web/geek/chat）。

    护栏计数（已发送数 / 话术去重 / 暂存岗位）从注入的 ``runtime.state`` 读、经 ``Command(update)``
    写回 custom state（JobAgentState），随 thread checkpoint 持久——不再用进程级全局 dict。
    """
    from ...core.config import get_settings

    tool_call_id = runtime.tool_call_id
    state = runtime.state

    def _cmd(payload: dict, **state_update: Any) -> Command:
        """把结果包成 ToolMessage，并附带 state 更新（计数/去重），统一经 Command 返回。"""
        update: dict[str, Any] = dict(state_update)
        update["messages"] = [ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=tool_call_id, name="send_greeting",
        )]
        return Command(update=update)

    text = (text or "").strip()
    if not text:
        return _cmd({"ok": False, "error": "greeting_text_empty"})
    if len(text) > _GREETING_LIMIT:
        return _cmd({"ok": False, "error": f"greeting_too_long(>{_GREETING_LIMIT}字)"})

    # 投递上限护栏（confirm / unattended 统一生效）：超限则不发送，让 Agent 收尾。
    # 计数来自 thread state（随 checkpoint 持久），重启不归零，避免突破单轮上限。
    count = rg.get_greeting_count(state)
    cap = int(get_settings().max_greetings_per_run or 0)
    if cap > 0 and count >= cap:
        return _cmd(
            {"ok": False, "error": "greeting_cap_reached", "cap": cap, "sent": count,
             "suggest": f"已达本轮投递上限({cap})，停止发送。请总结已投递岗位并结束。"}
        )

    # 去重前置：完全相同的话术、或同一岗位已打招呼，只发一次，避免重复骚扰 HR。
    if rg.is_duplicate(state, text):
        return _cmd(
            {"ok": False, "error": "duplicate_greeting",
             "suggest": "完全相同的话术本会话已发过，别重复发送；去处理下一个岗位。"}
        )
    job = rg.get_pending_job(state)
    if job and job_ledger.is_terminal(
        job_ledger.get_entry(job_ledger.USER_KEY, job.get("company"), job.get("position"))
    ):
        return _cmd(
            {"ok": False, "error": "already_greeted",
             "target": f"{job.get('company')}|{job.get('position')}",
             "suggest": "该岗位已打过招呼，跳过它、去处理下一个岗位，别再重复发送。"}
        )

    res = await _bridge_act("send_greeting", {"text": text})
    res["_action"] = "send_greeting"
    # 成败以 content 内层 data.ok 为准（外层 ok 只表示 dispatch 成功，会误把
    # not_on_chat_page / 未找到输入框 当成已发送）。成功才计数 + 记“已打招呼”，避免污染账本。
    inner = res.get("data") if isinstance(res.get("data"), dict) else None
    sent_ok = inner.get("ok") is True if inner is not None else bool(res.get("ok"))
    if sent_ok:
        upd = rg.sent_update(state, text)  # {greeting_count: n+1, sent_hashes: [..., h]}
        res["sent_total"] = upd["greeting_count"]
        # 发送成功：把 compare 阶段暂存的达标岗位登记为“已打招呼”（pending 不清，使同岗位再发被拦）
        if job:
            job_ledger.record(job_ledger.USER_KEY, job.get("company"), job.get("position"),
                              "已打招呼", link=job.get("link"))
            res["reviewed"] = f"{job.get('company')}|{job.get('position')}=已打招呼"
        _trace_push(json.dumps({"page": True, "action": "send_greeting", "ok": True},
                               ensure_ascii=False))
        return _cmd(res, **upd)
    if inner is not None and inner.get("ok") is False:
        # 把内层软错误（not_on_chat_page / 未找到输入框）透出到顶层，让模型纠正、RunTrace 记为错误
        res["ok"] = False
        res["error"] = str(inner.get("error") or "send_failed")
        if inner.get("hint"):
            res["suggest"] = inner["hint"]
    _trace_push(json.dumps({"page": True, "action": "send_greeting", "ok": res.get("ok")},
                           ensure_ascii=False))
    return _cmd(res)


__all__ = ["browser_snapshot", "browser_act", "start_chat", "send_greeting", "begin_trace"]