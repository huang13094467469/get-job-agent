"""WS /ws：定向中继（方案 C）。

Server 为唯一中枢，协调两路长连接：
- 「网页面板」panel：发高层指令；接收状态推送。
- 「页内 Content Script」page：唯一 DOM 执行器；经此接收 DOM 指令并回报结果。

路由规则：
- 面板指令（带目标 page）→ 转发给对应页内 CS；
- 页内 CS 的 `dom_result` → 按 `idem` 回发给发起该指令的面板（未命中则广播给全部面板）；
- 页内 CS 的状态消息（register / ping）→ 广播给全部面板。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from ...agent.job_filter import filter_jobs
from ...agent.resume_service import build_from_online
from ...core.config import get_settings
from ...core.logs import logger
from ..page_bridge import PageActionBridge

router = APIRouter()


class _WsHub:
    """两路长连接的注册表 + 定向中继。单 asyncio 事件循环内调用，无需额外加锁。"""

    def __init__(self) -> None:
        self._pages: dict[str, WebSocket] = {}  # page_id -> 页内 CS
        self._page_meta: dict[str, Any] = {}     # page_id -> 注册 payload（供面板回放）
        self._panels: list[WebSocket] = []      # 全部面板连接
        self._idem: dict[str, WebSocket] = {}   # idem -> 发起该指令的面板
        self.bridge = PageActionBridge()        # Agent 侧页操作桥
        self.bridge.bind_pages(lambda: self._pages)

    # ---- 注册 / 注销 ----
    def add_page(self, page_id: str, ws: WebSocket, payload: Any = None) -> None:
        # 同页签重新连接时替换旧连接
        old = self._pages.get(page_id)
        self._pages[page_id] = ws
        self._page_meta[page_id] = payload or {}
        logger.info("页内 CS 注册 page_id={} (替换={})", page_id, old is not None)

    def remove_page(self, page_id: str, ws: WebSocket | None = None) -> None:
        # 身份守卫：只有当“正在断开的这条 ws 恰好还是当前登记的这条”时才真删。
        # 整页跳转时新页 content script 的 ws 会先 add_page 覆盖旧 ws；随后旧 ws 才被发现断开，
        # 若无条件按 page_id 删除就会误删新连接 → 面板“失去当前 Get-Job 页”。
        cur = self._pages.get(page_id)
        if ws is not None and cur is not None and cur is not ws:
            logger.info("忽略陈旧连接的注销 page_id={}（已被新连接替换）", page_id)
            return
        if self._pages.pop(page_id, None):
            logger.info("页内 CS 断开 page_id={}", page_id)
        self._page_meta.pop(page_id, None)
        self.bridge.drop_page(page_id)

    def connected_pages(self) -> list[tuple[str, Any]]:
        """当前已连接的页内 CS 列表 [(page_id, payload)]，供新面板注册时回放连接态。"""
        return [(pid, self._page_meta.get(pid)) for pid in self._pages]

    def add_panel(self, ws: WebSocket) -> None:
        self._panels.append(ws)
        logger.info("面板连接，当前 {} 个", len(self._panels))

    def remove_panel(self, ws: WebSocket) -> None:
        if ws in self._panels:
            self._panels.remove(ws)
        # 清理该面板发起的未决 idem
        stale = [k for k, v in self._idem.items() if v is ws]
        for k in stale:
            self._idem.pop(k)
        logger.info("面板断开，当前 {} 个", len(self._panels))

    # ---- 发送工具 ----
    @staticmethod
    async def _send(ws: WebSocket, obj: dict[str, Any]) -> None:
        try:
            await ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass  # 发失败由连接级异常捕获兜底

    async def broadcast_to_panels(self, obj: dict[str, Any]) -> None:
        for p in list(self._panels):
            await self._send(p, obj)

    async def route_to_page(self, page_id: str, obj: dict[str, Any]) -> None:
        ws = self._pages.get(page_id)
        if ws is None:
            logger.warning("目标页签不存在，丢弃指令 page_id={} type={}", page_id, obj.get("type"))
            return
        await self._send(ws, obj)

    # ---- 中间处理 ----
    def reg_idem(self, idem: str, ws: WebSocket) -> None:
        if idem:
            self._idem[idem] = ws

    async def forward_page_result(self, msg: dict[str, Any]) -> None:
        """页内 CS 的 dom_result：优先回发给发起面板；否则广播。"""
        idem = msg.get("idem")
        target = self._idem.pop(idem, None) if idem else None
        if target is not None:
            await self._send(target, msg)
        else:
            await self.broadcast_to_panels(msg)


hub = _WsHub()


# 无人值守自主闭环参数
# 一个 goal 最多自动续跑多少段（配合投递上限防死循环）
_MAX_AUTO_STEPS = 60
# recursion_limit 解耦为单纯防死循环的较大常量：单段封顶已交给官方限额中间件
# （ModelCallLimit/ToolCallLimit 的 run_limit，见 harness），不再与手动 cap 强耦合（P0-3）。
_RECURSION_LIMIT = 256
# Agent 确认「无更多达标岗位」时输出的哨兵，驱动据此收尾
_DONE_SENTINEL = "【求职任务结束】"
# 自动续跑时注入的推进指令（同一 thread、携带历史）
_CONTINUE_MSG = (
    "继续按 SOP 处理下一个达标岗位：先回岗位列表页（browser_navigate 到 "
    "https://www.zhipin.com/web/geek/jobs），等页面稳定后 browser_snapshot，"
    "跳过已沟通过（contacted=true / 【已沟通】）的岗位，对下一个匹配达标岗位走完整闭环"
    "（browser_click 立即沟通→若弹框点【继续沟通】→进入聊天页→拟话术→check_greeting 预检→"
    "browser_click/type/press_key 发送→browser_snapshot 确认清空→confirm_greeting_sent 登记），"
    "然后继续找下一个。"
    "**不要只返回筛选清单就停**；只有当列表（含翻页）确无更多达标岗位时，"
    "才输出“【求职任务结束】”并简要总结已投递岗位。"
)

# idem -> 运行中的浏览器 Agent 任务（用于用户终止）
_agent_tasks: dict[str, asyncio.Task] = {}

# 线程 key（client_id 或 ws 兜底） -> 当前正在运行的最新 idem。
# 新 agent_go 进来时据此打断该连接在跑的旧任务，保证“随时可发新消息、自动顶掉旧任务”
# （连续对话：无需等上一个浏览器任务跑完/停止）。线程 key 与 _panel_threads 同源，
# 面板重连后按 client_id 仍能匹配到旧任务并打断。
_panel_running: dict[str, str] = {}

# 面板连接 -> 会话 thread_id：同一连接的多轮 agent_go/resume 共享同一 LangGraph 线程，
# 状态由 checkpointer 按 thread_id 持久化（跨轮对话 / 原生 HITL 暂停恢复）。
# 线程 key 优先用「面板持久 client_id」（面板刷新/扩展重载后不变，保证连续对话），
# 未上报 client_id 的旧面板回退用 ws 对象 id。
_panel_clients: dict[int, str] = {}  # id(ws) -> 面板持久 client_id
_panel_threads: dict[str, str] = {}  # client_id 或 ws 兜底 key -> thread_id


def _agent_key(idem: str, ws: WebSocket) -> str:
    """任务注册键：优先用面板 idem，缺失时以 ws 对象兜底。"""
    return idem or f"ws_{id(ws)}"


def _panel_key(ws: WebSocket) -> str:
    """面板的任务归属键（用于打断旧任务）：优先用持久 client_id，缺失时以 ws 兜底。"""
    return _panel_clients.get(id(ws)) or f"ws_{id(ws)}"


def _cancel_panel_task(ws: WebSocket) -> None:
    """打断该面板连接上正在运行的旧任务（如有）。新消息到来时调用，实现连续对话自动顶替。"""
    key = _panel_key(ws)
    prev_idem = _panel_running.get(key)
    if not prev_idem:
        return
    prev = _agent_tasks.pop(_agent_key(prev_idem, ws), None)
    if prev and not prev.done():
        prev.cancel()
        logger.info("新任务打断旧任务 thread_key={} 旧idem={}", key, prev_idem)


def _get_thread(ws: WebSocket, *, reset: bool = False) -> str:
    """取（或按需新建）本面板连接的会话 thread_id。reset=True 强制换新线程（清空会话）。

    换新 thread 即丢弃跨轮历史：新 thread 在 checkpointer 里无状态，护栏计数（greeting_count /
    sent_hashes / pending_job）自然归零，无需再显式 reset 全局 dict（已迁入 state，P0-2）。
    线程按持久 client_id 绑定：面板重载/WS 重连后仍续用同一 thread，保持连续对话。
    """
    key = _panel_clients.get(id(ws)) or f"ws_{id(ws)}"
    if reset:
        _panel_threads.pop(key, None)
    tid = _panel_threads.get(key)
    if tid is None:
        tid = f"sess-{uuid.uuid4().hex}"
        _panel_threads[key] = tid
    return tid


async def _state_greeting_count(agent, thread_id: str) -> int:
    """从 checkpointer 持久 state 读「已成功发送数」（替代进程级全局计数，评估 P0-2）。

    配持久 Postgres checkpointer 后 state 跨重启保留；读不到（新 thread / 异常）时返回 0。
    """
    try:
        snap = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        return int((snap.values or {}).get("greeting_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _push_agent_status(ws: WebSocket, idem: str, payload: dict[str, Any]) -> None:
    """向发起面板推送运行状态（不经意，尽力而为）。"""
    asyncio.create_task(hub._send(ws, {
        "from": "server", "type": "agent_status", "idem": idem, "payload": payload,
    }))


def _push_agent_event(ws: WebSocket, idem: str, payload: dict[str, Any]) -> None:
    """流式推送运行过程事件（工具调用/结果/模型叙述）到发起面板，让用户实时看到后端在干吗。"""
    asyncio.create_task(hub._send(ws, {
        "from": "server", "type": "agent_event", "idem": idem, "payload": payload,
    }))


def _content_str(content: Any) -> str:
    """把消息 content（str 或内容块列表）拍平成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content or "")


def _result_ok(body: str) -> bool | None:
    """从工具返回文本里尽量抽出 ok 标志（供面板标红/标绿）。"""
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(obj, dict) and "ok" in obj:
        return bool(obj.get("ok"))
    return None


def _summarize_tool_result(body: str) -> str:
    """把工具返回压缩成一行摘要（绝不回传整页快照，省带宽/token）。"""
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return body[:120]
    if not isinstance(obj, dict):
        return body[:120]
    parts: list[str] = []
    if obj.get("_action"):
        parts.append(str(obj["_action"]))
    if "ok" in obj:
        parts.append("ok" if obj.get("ok") else "fail")
    if obj.get("error"):
        parts.append(f"err={obj['error']}")
    for key in ("data", "updated"):
        d = obj.get(key)
        if isinstance(d, dict):
            if isinstance(d.get("elements"), list):
                parts.append(f"{len(d['elements'])}元素")
            if isinstance(d.get("jobs"), list):
                parts.append(f"{len(d['jobs'])}岗位")
    summary = " ".join(parts)
    return summary or body[:120]


def _extract_interrupt(interrupts: Any) -> dict[str, Any] | None:
    """从 astream updates 的 __interrupt__ 里抽出 HITL 待确认信息。

    interrupts 通常是 (Interrupt(value={...}),)。value 为 HumanInTheLoop 中
    注入的 {"action_requests":[{name,args}], "review_configs":[{action_name,allowed_decisions}]}。
    """
    items = interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]
    for it in items:
        value = getattr(it, "value", it)
        if not isinstance(value, dict):
            continue
        action_requests = value.get("action_requests") or []
        review_configs = value.get("review_configs") or []
        if not action_requests:
            continue
        actions: list[dict[str, Any]] = []
        allowed: list[str] = []
        greeting: str | None = None
        for ar in action_requests:
            name = str(ar.get("name") or "")
            args = ar.get("args") or {}
            actions.append({"name": name, "args": args})
            if name == "send_greeting" and greeting is None:
                greeting = str(args.get("text") or "")
        for rc in review_configs:
            ad = rc.get("allowed_decisions") if isinstance(rc, dict) else None
            if ad:
                allowed = list(ad)
                break
        return {"actions": actions, "greeting": greeting,
                "allowed_decisions": allowed or ["approve", "edit", "reject"]}
    return None


async def _stream_agent_once(
    *, inputs: Any, thread_id: str, idem: str, ws: WebSocket, mode: str | None = None
) -> dict:
    """在给定 thread 上跑一段（一次 astream），实时推事件；返回状态字典。

    inputs 为 {"messages":[...]}（新目标/续跑）或 Command(resume=...)（恢复被暂停会话）。
    mode 为该 thread 的运行模式（confirm/unattended）：决定取哪张编译图（是否挂 HITL 中断），
    缺省时用全局 settings.agent_mode。
    不在这里发 agent_go_result（终态由 _run_agent_turn 统一决定）；但会推运行中状态、
    工具/叙述事件，以及 confirm 模式下的中断事件。返回：
      {done, interrupted, failed, final}。
    CancelledError（用户终止）不在此捕获，交由驱动统一处理。
    """
    from ...agent.harness import ctx_config, get_browser_agent
    from ...agent.tools import begin_trace

    st: dict[str, Any] = {"done": False, "interrupted": False, "failed": False, "final": ""}
    begin_trace()
    settings = get_settings()
    run_mode = str(mode or settings.agent_mode).lower()
    run_settings = (
        settings if run_mode == str(settings.agent_mode).lower()
        else settings.model_copy(update={"agent_mode": run_mode})
    )

    agent = get_browser_agent(run_settings)
    is_resume = isinstance(inputs, Command)
    cfg = ctx_config(
        thread_id=thread_id,
        recursion_limit=_RECURSION_LIMIT,
        run_name="get_job_browser_resume" if is_resume else "get_job_browser_run",
        tags=["resume"] if is_resume else ["new-goal"],
    )
    seen: set[int] = set()
    final_answer: Any = ""
    last_assistant_text = ""  # 最后一次模型输出文本：final 为空时的兜底（正常回复不被折叠）
    round_no = 0  # 仅用于前端展示；单段封顶已交给官方限额中间件（不再手动 break，评估 P0-3）
    try:
        async for chunk in agent.astream(inputs, config=cfg, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            # confirm 模式下的原生 HITL 中断：send_greeting 暂停等待用户确认
            if "__interrupt__" in chunk:
                info = _extract_interrupt(chunk["__interrupt__"])
                if info is None:
                    info = {"actions": [], "greeting": None,
                            "allowed_decisions": ["approve", "reject"]}
                _push_agent_status(ws, idem, {"status": "awaiting_confirmation"})
                _push_agent_event(ws, idem, {"kind": "interrupt", "thread": thread_id, **info})
                st["interrupted"] = True
                return st
            for node, update in chunk.items():
                msgs = update.get("messages") if isinstance(update, dict) else None
                if not msgs:
                    continue
                for m in msgs:
                    m_id = id(m)
                    if m_id in seen:
                        continue
                    seen.add(m_id)
                    if isinstance(m, AIMessage):
                        round_no += 1
                        ans_text = _content_str(m.content)
                        if ans_text.strip():
                            final_answer = m.content
                            last_assistant_text = ans_text
                            # 模型输出（含正常回复）完整推送：前端直接作为 Agent 正文显示，
                            # 不截断，避免「只看到思考、看不到最终输出」。
                            _push_agent_event(ws, idem, {
                                "kind": "assistant", "round": round_no, "text": ans_text,
                            })
                        for tc in m.tool_calls or []:
                            args_s = json.dumps(tc.get("args") or {}, ensure_ascii=False)
                            logger.debug(
                                "TOOL调用 第{}轮 name={} args={}",
                                round_no, tc.get("name"), args_s[:500],
                            )
                            _push_agent_status(ws, idem, {
                                "status": "operating", "tool": tc.get("name"), "round": round_no,
                            })
                            _push_agent_event(ws, idem, {
                                "kind": "tool_call", "round": round_no,
                                "tool": tc.get("name"), "args": args_s[:200],
                            })
                    elif isinstance(m, ToolMessage):
                        body = _content_str(m.content)
                        _push_agent_event(ws, idem, {
                            "kind": "tool_result", "tool": m.name or "tool",
                            "ok": _result_ok(body), "summary": _summarize_tool_result(body),
                        })
            _push_agent_status(ws, idem, {"status": "running", "round": round_no})
    except Exception as exc:  # noqa: BLE001  图/工具异常（不含 CancelledError）
        logger.warning("agent 段运行异常: {}", exc)
        st["failed"] = True
        st["final"] = str(exc)
        return st

    # 限额中间件（ModelCallLimit exit_behavior='end'）超限时图干净 END、astream 正常结束；
    # 是否续跑由 _run_agent_turn 依据哨兵/投递上限/步数判定，本段不再手动 break（评估 P0-3）。
    # final 优先取模型最后一条消息；为空时用最后一次 assistant 文本兜底，保证前端正文非空。
    st["final"] = str(final_answer) or last_assistant_text
    st["done"] = True
    return st


async def _send_turn_result(ws: WebSocket, idem: str, *, ok: bool, output: str = "",
                            error: str = "", stopped: bool = False,
                            sent: int | None = None) -> None:
    """向面板发送本轮（一个 goal 的完整自主运行）终态；sent 为已投递数（供面板展示）。"""
    status = "stopped" if stopped else ("done" if ok else "failed")
    _push_agent_status(ws, idem, {"status": status})
    await hub._send(ws, {
        "from": "server", "type": "agent_go_result", "idem": idem,
        "payload": {"ok": ok, "stopped": stopped, "output": output, "error": error or None,
                    "sent": sent},
    })


async def _run_agent_turn(*, start_inputs: Any, thread_id: str, idem: str, ws: WebSocket) -> None:
    """一个 goal 的完整运行驱动：confirm 跑一段；unattended 自动续跑直到结束/触顶。

    unattended：只要本段没报错/没中断、未输出结束哨兵、未达投递上限与最大步数，
    就向同一 thread 注入 _CONTINUE_MSG 接着处理下一个岗位，实现真正无人值守。

    运行配置（模式/投递上限/意图）从 thread state 读（面板 agent_go 时写入，持久在 checkpoint）：
    - mode 决定取哪张编译图（是否挂 HITL）与是否自动发送；
    - job_hunt 决定是否自动续跑：**只有找工作任务才注入 _CONTINUE_MSG 续跑**，
      普通对话/咨询无论模式只跑一段即返回，避免聊天时被诱导去投递；
    - cap 与 send_greeting 工具同源，避免"工具已拒发但驱动还在续跑"的窗口。
    """
    from ...agent.harness import ctx_config, get_browser_agent
    from ...agent.runtime_guard import get_agent_mode, get_max_greetings

    key = _agent_key(idem, ws)
    settings = get_settings()
    cfg = ctx_config(thread_id=thread_id, recursion_limit=_RECURSION_LIMIT)
    agent = get_browser_agent(settings)
    # 读 thread 持久 state 里的运行配置（首轮由 _register_agent_go 写入）
    snap = await agent.aget_state(cfg)
    st_values = (snap.values or {}) if snap else {}
    mode = get_agent_mode(st_values, settings.agent_mode)
    cap = get_max_greetings(st_values, int(settings.max_greetings_per_run or 0))
    job_hunt = bool(st_values.get("job_hunt", False))
    run_settings = (
        settings if mode == str(settings.agent_mode).lower()
        else settings.model_copy(update={"agent_mode": mode})
    )
    agent = get_browser_agent(run_settings)
    unattended = mode != "confirm"
    inputs = start_inputs
    final = ""
    steps = 0
    try:
        while True:
            steps += 1
            st = await _stream_agent_once(
                inputs=inputs, thread_id=thread_id, idem=idem, ws=ws, mode=mode
            )
            final = st.get("final") or final
            if st["failed"]:
                await _send_turn_result(ws, idem, ok=False, error=final or "运行异常",
                                        sent=await _state_greeting_count(agent, thread_id))
                return
            if st["interrupted"]:
                return  # confirm 已推中断事件，等面板 agent_resume
            if not (unattended and job_hunt):
                # 非找工作任务（聊天/咨询），或 confirm 模式：跑完一段即返回。
                # 聊天不续跑：即使无人值守也不注入找工作推进指令，防止误投递。
                await _send_turn_result(ws, idem, ok=True, output=final,
                                        sent=await _state_greeting_count(agent, thread_id))
                return
            # unattended：判定是否自动续跑
            if _DONE_SENTINEL in final:
                await _send_turn_result(ws, idem, ok=True,
                                        output=final.replace(_DONE_SENTINEL, "").strip(),
                                        sent=await _state_greeting_count(agent, thread_id))
                return
            # 投递上限从 checkpointer 持久 state 读（替代进程级全局计数，评估 P0-2）
            sent = await _state_greeting_count(agent, thread_id)
            if cap > 0 and sent >= cap:
                out = (final + f"\n（已达本轮投递上限 {cap} 个，自动停止）").strip()
                await _send_turn_result(ws, idem, ok=True, output=out, sent=sent)
                return
            if steps >= _MAX_AUTO_STEPS:
                out = (final + "\n（已达最大自动续跑步数，如需继续请再发起）").strip()
                await _send_turn_result(ws, idem, ok=True, output=out, sent=sent)
                return
            logger.info("无人值守自动续跑 step={} thread={} 已投递={}", steps, thread_id, sent)
            inputs = {"messages": [{"role": "user", "content": _CONTINUE_MSG}]}
    except asyncio.CancelledError:
        logger.info("agent 运行被终止 idem={}", idem)
        await _send_turn_result(ws, idem, ok=False, output="", error="已由用户停止", stopped=True)
    except Exception as exc:  # noqa: BLE001
        # 兜底：任何未预期异常都必须回执 agent_go_result，否则面板 running 永远为 true，
        # 后续消息会被误判为「运行中」而排队卡死（用户反馈的 bug）。
        logger.exception("agent 运行异常（未兜底回执） idem={}", idem)
        await _send_turn_result(ws, idem, ok=False, error=f"运行异常: {exc}")
    finally:
        _agent_tasks.pop(key, None)
        # 若该 connection 的最新运行确实落到本任务，则清空 running 标记，放行下一次发送
        if _panel_running.get(_panel_key(ws)) == idem:
            _panel_running.pop(_panel_key(ws), None)


async def _classify_intent(goal: str) -> str:
    """判断用户目标是「找工作/投递」(job_hunt) 还是「对话/咨询」(chat)。

    用对话模型做一次轻量分类（输出单 token 级判断）。失败降级为 chat——
    chat 语义下 agent 仍可凭 AGENTS.md 的意图分流自行判断并加载 skill，只是不自动续跑，安全。
    """
    from ...agent.harness import build_model
    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        model = build_model(get_settings())
        resp = await model.ainvoke([
            SystemMessage(content=(
                "你是求职助手意图分类器。判断用户消息是否表达「找工作/投递简历」的明确意图"
                "（找工作、搜岗位、投递、逐岗沟通、发打招呼、跑求职流程、对比岗位挑投递等）。"
                "只输出一个词：job_hunt 或 chat。"
            )),
            HumanMessage(content=goal),
        ])
        text = str(getattr(resp, "content", "") or "").strip().lower()
        return "job_hunt" if "job_hunt" in text else "chat"
    except Exception as exc:  # noqa: BLE001
        logger.warning("意图分类失败，按 chat 处理: {}", exc)
        return "chat"


async def _register_agent_go(hub, msg: dict[str, Any], idem: str, websocket) -> None:
    """面板发起自然语言目标 → 写入运行配置到 thread state → 启动（可能自动续跑的）运行驱动。

    面板可在 payload 里透传：
    - mode: "confirm" | "unattended"（缺省用全局 settings.agent_mode）
    - max_greetings: 本轮投递上限，0=不限（缺省用全局 settings.max_greetings_per_run）
    """
    from ...agent.harness import ctx_config, get_browser_agent

    payload = msg.get("payload") or {}
    goal = str(payload.get("goal") or "").strip()
    if not goal:
        asyncio.create_task(hub._send(websocket, {
            "from": "server", "type": "agent_go_result",
            "idem": idem, "payload": {"ok": False, "error": "goal 为空"},
        }))
        return
    settings = get_settings()
    # 模式：仅接受 confirm/unattended，非法值回退全局配置
    mode = str(payload.get("mode") or settings.agent_mode).lower()
    if mode not in ("confirm", "unattended"):
        mode = str(settings.agent_mode).lower()
    # 投递上限：非负整数，非法值回退全局配置
    try:
        cap = int(payload.get("max_greetings"))
    except (TypeError, ValueError):
        cap = int(settings.max_greetings_per_run or 0)
    cap = max(0, cap)

    thread_id = _get_thread(websocket)
    # 连续对话：新消息直接顶掉本面板在跑的旧任务（浏览器自动化可能长时间续跑，不能让用户干等）
    _cancel_panel_task(websocket)
    # 意图识别：找工作/投递 → 允许无人值守自动续跑；对话/咨询 → 只跑一段，避免被诱导去投递
    intent = await _classify_intent(goal)
    logger.info("agent_go 受理 thread={} mode={} max_greetings={} intent={} goal={}",
                thread_id, mode, cap, intent, goal[:80])
    # 把本轮运行配置写入 thread state（随 checkpoint 持久），send_greeting / 续跑判定同源读取
    agent = get_browser_agent(
        settings if mode == str(settings.agent_mode).lower()
        else settings.model_copy(update={"agent_mode": mode})
    )
    try:
        await agent.aupdate_state(
            ctx_config(thread_id=thread_id, recursion_limit=_RECURSION_LIMIT),
            {"max_greetings": cap, "agent_mode": mode, "job_hunt": intent == "job_hunt"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入运行配置到 thread state 失败（将用全局默认）: {}", exc)
    start_inputs = {"messages": [{"role": "user", "content": goal}]}
    task = asyncio.create_task(
        _run_agent_turn(start_inputs=start_inputs, thread_id=thread_id, idem=idem, ws=websocket)
    )
    _agent_tasks[_agent_key(idem, websocket)] = task
    _panel_running[_panel_key(websocket)] = idem


def _normalize_decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把面板回传的确认结果规整成 HITL 的 decisions 列表。

    面板可发 payload.decision（单个）或 payload.decisions（列表）。元素形如：
      {"type":"approve"} | {"type":"edit","text":<新话术>} | {"type":"reject","message":<理由>}
    edit 的 text 会被装配成 send_greeting 的 edited_action。
    """
    raw = payload.get("decisions")
    if not isinstance(raw, list) or not raw:
        single = payload.get("decision")
        raw = [single] if isinstance(single, dict) else []
    decisions: list[dict[str, Any]] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        dtype = str(d.get("type") or "reject")
        if dtype == "edit":
            text = str(d.get("text") or d.get("edited_text") or "").strip()
            decisions.append({
                "type": "edit",
                "edited_action": {"name": "check_greeting", "args": {"text": text}},
            })
        elif dtype == "reject":
            rmsg = d.get("message") or "用户拒绝发送，请跳过本岗位，继续下一个或结束。"
            decisions.append({"type": "reject", "message": str(rmsg)})
        else:
            decisions.append({"type": "approve"})
    if not decisions:
        decisions = [{"type": "reject", "message": "未收到有效确认，按拒绝处理。"}]
    return decisions


def _register_agent_resume(idem: str, ws: WebSocket, payload: dict[str, Any]) -> None:
    """面板对 HITL 中断的确认（仅 confirm 模式）→ 以 Command(resume) 恢复同一 thread。

    投递上限已由 check_greeting（护栏预检）+ confirm_greeting_sent（发送后登记）把关。
    """
    thread_id = _get_thread(ws)
    decisions = _normalize_decisions(payload)
    logger.info("agent_resume thread={} decisions={}", thread_id, decisions)
    _cancel_panel_task(ws)
    start_inputs = Command(resume={"decisions": decisions})
    task = asyncio.create_task(
        _run_agent_turn(start_inputs=start_inputs, thread_id=thread_id, idem=idem, ws=ws)
    )
    _agent_tasks[_agent_key(idem, ws)] = task
    _panel_running[_panel_key(ws)] = idem


def _register_stop_agent(idem: str, ws: WebSocket) -> None:
    """终止指定 idem 正在运行的浏览器 Agent。"""
    key = _agent_key(idem, ws)
    task = _agent_tasks.pop(key, None)
    if _panel_running.get(_panel_key(ws)) == idem:
        _panel_running.pop(_panel_key(ws), None)
    if task and not task.done():
        task.cancel()
        _push_agent_status(ws, idem, {"status": "stopping"})
    else:
        _push_agent_status(ws, idem, {"status": "idle", "detail": "当前没有运行中的任务"})


def _register_reset_agent(idem: str, ws: WebSocket) -> None:
    """清空会话：停掉运行中的 Agent，并为该连接换一条全新的 thread（丢弃跨轮历史）。"""
    key = _agent_key(idem, ws)
    task = _agent_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
    _panel_running.pop(_panel_key(ws), None)  # 清重置后本面板无在跑任务，放行后续发送
    _get_thread(ws, reset=True)  # 生成新 thread_id，并清零旧线程的投递计数
    logger.info("会话已清空（新线程） idem={}", idem)
    asyncio.create_task(hub._send(ws, {
        "from": "server", "type": "reset_agent_result", "idem": idem,
        "payload": {"ok": True},
    }))


async def _run_resume_dir_scan(idem: str, ws: WebSocket) -> None:
    """手动触发简历目录重扫，把汇总回发给发起面板。"""
    from ...jobs.resume_startup import scan_resume_dir

    try:
        result = await scan_resume_dir(get_settings())
    except Exception as exc:  # noqa: BLE001
        logger.warning("手动简历扫描异常: {}", exc)
        result = {"ok": False, "error": str(exc)}
    await hub._send(ws, {
        "from": "server", "type": "resume_build_dir_result",
        "idem": idem, "payload": result,
    })


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    role: str | None = None
    page_id: str | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("收到非 JSON 报文，忽略: {}", raw[:120])
                continue
            if not isinstance(msg, dict):
                continue

            msg_from = msg.get("from")

            # ---- 角色注册 ----
            if msg.get("type") == "register":
                if msg_from == "page":
                    role, page_id = "page", msg.get("id", "page:default")
                    hub.add_page(page_id, websocket, msg.get("payload"))
                    await hub.broadcast_to_panels(
                        {"from": "server", "type": "page_connected",
                         "page_id": page_id, "payload": msg.get("payload")}
                    )
                elif msg_from == "panel":
                    role = "panel"
                    hub.add_panel(websocket)
                    # 绑定面板持久 client_id：线程按此复用（面板重载/重连后连续对话）
                    cid = str(msg.get("client_id") or "").strip()
                    if cid:
                        _panel_clients[id(websocket)] = cid
                    await hub._send(websocket, {
                        "from": "server", "type": "registered", "role": "panel",
                    })
                    # 关键：向刚注册/重连的面板回放当前页面连接态，
                    # 避免“页面早于面板已连接→面板错过注册广播→不刷新就显示未连接”。
                    pages = hub.connected_pages()
                    if pages:
                        for pid, meta in pages:
                            await hub._send(websocket, {"from": "server", "type": "page_connected",
                                                        "page_id": pid, "payload": meta})
                    else:
                        await hub._send(websocket, {"from": "server", "type": "page_connected",
                                                    "page_id": None, "payload": None})
                continue

            if role is None:
                logger.warning("未注册角色先发指令，忽略: type={}", msg.get("type"))
                continue

            # ---- 面板 → 页内 CS ----
            if role == "panel":
                idem = msg.get("idem")
                if idem:
                    hub.reg_idem(idem, websocket)
                # T3.5 过滤引擎：Server 侧直接处理，不经页内 CS
                if msg.get("type") == "filter_jobs":
                    payload = msg.get("payload") or {}
                    try:
                        result = filter_jobs(
                            payload.get("jobs") or [],
                            rules=payload.get("rules"),
                            config_path=payload.get("config_path", "config/config.yaml"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {"ok": False, "error": str(exc)}
                    await hub._send(
                        websocket,
                        {"from": "server", "type": "filter_jobs_result",
                         "idem": idem, "payload": result},
                    )
                    continue
                # M1 在线简历画像构建：面板提交简历文本 → Server 解析/抽取/校验/入库/向量化
                if msg.get("type") == "build_resume":
                    payload = msg.get("payload") or {}
                    raw = str(payload.get("raw_text") or "").strip()
                    if not raw:
                        await hub._send(
                            websocket,
                            {"from": "server", "type": "build_resume_result",
                             "idem": idem, "payload": {"ok": False, "error": "raw_text 为空"}},
                        )
                        continue
                    try:
                        result = await build_from_online(
                            get_settings(),
                            user_key=str(payload.get("user_key") or "default"),
                            raw_text=raw,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("在线简历画像构建失败: {}", exc)
                        result = {"ok": False, "error": str(exc)}
                    await hub._send(
                        websocket,
                        {"from": "server", "type": "build_resume_result",
                         "idem": idem, "payload": result},
                    )
                    continue
                # M5 computer-use：面板发起自然语言目标，后台跑浏览器操作 Agent
                if msg.get("type") == "agent_go":
                    await _register_agent_go(hub, msg, idem, websocket)
                    continue
                # 终止正在运行的浏览器 Agent
                if msg.get("type") == "stop_agent":
                    _register_stop_agent(idem, websocket)  # 传原始 idem，与 reset 口径对齐
                    continue
                # 清空会话：停止运行中的 Agent + 换新 thread
                if msg.get("type") == "reset_agent":
                    _register_reset_agent(idem, websocket)
                    continue
                # 原生 HITL：面板对 send_greeting 中断的确认（approve/edit/reject）
                # → 以 Command(resume) 恢复同一 thread 继续运行
                if msg.get("type") == "agent_resume":
                    _register_agent_resume(idem, websocket, msg.get("payload") or {})
                    continue
                # 简历目录手动重扫：Side Panel 触发
                if msg.get("type") == "resume_build_dir":
                    asyncio.create_task(_run_resume_dir_scan(idem, websocket))
                    continue
                target = msg.get("to") or msg.get("page_id")
                if target:
                    await hub.route_to_page(target, msg)
                # 无目标的面板消息此处先不广播，避免多余会话
                continue

            # ---- 页内 CS → 面板 ----
            if role == "page":
                if msg.get("type") == "online_resume":
                    # CS 读取平台在线简历 → 构建画像 → 广播结果给面板
                    user_key = msg.get("user_key") or "default"
                    raw_text = (msg.get("payload") or {}).get("raw_text") or ""
                    if not raw_text:
                        await hub.broadcast_to_panels(
                            {"from": "server", "type": "online_resume_error",
                             "page_id": page_id, "payload": {"reason": "raw_text 为空"}}
                        )
                        continue
                    try:
                        result = await build_from_online(
                            get_settings(),
                            user_key=user_key, raw_text=raw_text,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("在线简历构建失败: {}", exc)
                        await hub.broadcast_to_panels(
                            {"from": "server", "type": "online_resume_error",
                             "page_id": page_id, "payload": {"reason": str(exc)}}
                        )
                        continue
                    await hub.broadcast_to_panels(
                        {"from": "server", "type": "online_resume_done",
                         "page_id": page_id, "payload": result}
                    )
                    continue
                if msg.get("type") == "keepalive":
                    # CS 心跳：单播回一条 pong 供页内看门狗判活（不广播、不落日志）。
                    # 半开连接时服务端会移除该页且后续发送失败，回 pong 能让页内及时重连。
                    await hub._send(websocket, {"from": "server", "type": "pong"})
                    continue
                if msg.get("type") == "dom_result":
                    # 先喂给 Agent 侧页操作桥：命中 pending 即被消费，不再回面板
                    if hub.bridge.resolve(page_id or "", msg.get("idem"), msg.get("result")):
                        continue
                    await hub.forward_page_result(msg)
                else:
                    await hub.broadcast_to_panels(msg)
                continue

    except WebSocketDisconnect:
        pass
    finally:
        if role == "page" and page_id:
            hub.remove_page(page_id, websocket)  # 传 websocket 做身份守卫，避免陈旧连接误删新登记
            # 最后一个页内 CS 断开 → 通知面板翻到未连接（页面会在 ~2s 后自动重连并重新注册）
            if not hub.connected_pages():
                await hub.broadcast_to_panels(
                    {"from": "server", "type": "page_connected", "page_id": None, "payload": None}
                )
        elif role == "panel":
            hub.remove_panel(websocket)
            # 只清理「连接 -> client_id」映射；线程本身按 client_id 保留，面板重连后继续复用
            # （只有 reset_agent 才换新线程）。旧逻辑按 id(ws) 弹线程会切断连续对话，已废弃。
            _panel_clients.pop(id(websocket), None)


__all__ = ["router"]