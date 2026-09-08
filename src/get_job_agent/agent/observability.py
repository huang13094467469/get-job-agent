"""Agent 运行观测：RunTraceMiddleware（控制台浓缩摘要 + 工具错误 streak 风控）+ LangSmith 云端追踪。

- LangSmith 官方云：由 deepagents/LangGraph 的 create_deep_agent 自带回调自动上报（见
  init_tracing），把每次运行的模型/工具 I/O、耗时、token、子父执行顺序传到 smith.langchain.com
  Studio 查看，本模块不做手动打 span。
- RunTraceMiddleware 仅做进程内辅助：控制台打印浓缩摘要（每轮 LLM 输入/输出、计划工具、
  工具返回/异常），并对「连续同类工具软错误」计数升级为显式告警（运行期风控）。它不影响
  LangSmith 上报，两者互补。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, HumanMessage

from ..core.config import Settings, get_settings
from ..core.logs import logger

_MAX_TEXT = 300  # 控制台摘要里文本截断长度
_LLM_IN_CLIP = 700
_LLM_OUT_CLIP = 700
_TOOL_ARGS_CLIP = 300
_TOOL_RET_CLIP = 800


def init_tracing(settings: Settings) -> None:
    """按配置点亮 LangSmith 云端追踪（幂等；值均来自 settings，即 .env）。

    create_deep_agent 自带 LangSmith 回调：只要 LANGSMITH_* 进入进程环境变量，
    就会自动把每一步（模型/工具 I/O、耗时、token、子父执行顺序）上报到
    smith.langchain.com，无需手动打 span。
    """
    if not settings.langsmith_tracing:
        os.environ.pop("LANGSMITH_TRACING", None)
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    if settings.langsmith_api_key:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    if settings.langsmith_project:
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    logger.info("LangSmith 云端追踪已开启 project={}", settings.langsmith_project)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _thread_id() -> str:
    """从当前 LangGraph 运行配置里取 thread_id（取不到返回 unknown）。"""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str((cfg.get("configurable") or {}).get("thread_id") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _run_id_fallback() -> str:
    """从当前 LangGraph 运行配置取 per-run run_id；无则返回空。"""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        rid = cfg.get("run_id")
        return str(rid) if rid else ""
    except Exception:  # noqa: BLE001
        return ""


def _clip(s: Any, n: int = _MAX_TEXT) -> str:
    t = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return t if len(t) <= n else t[:n] + "…"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content or "")


def _usage_of(response: Any) -> dict[str, int] | None:
    try:
        for m in reversed(list(getattr(response, "result", None) or [])):
            usage = getattr(m, "usage_metadata", None)
            if usage:
                return {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "total": usage.get("total_tokens", 0),
                }
    except Exception:  # noqa: BLE001
        return None
    return None


def _last_human_text(state: Any) -> str:
    try:
        for m in reversed(list(state.get("messages") or [])):
            if isinstance(m, HumanMessage):
                return _clip(_content_text(m.content), 200)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _model_label(settings: Settings) -> str:
    return f"{settings.llm_provider}/{settings.llm_model}"


def _tool_text(result: Any) -> str:
    return _content_text(getattr(result, "content", result))


def _inspect_tool_result(result: Any) -> tuple[str, str]:
    """返回 (status, error_code)。status ∈ {ok, error}。"""
    status_attr = getattr(result, "status", None)
    text = _tool_text(result)
    obj: Any = None
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        obj = None
    if isinstance(obj, dict):
        if obj.get("ok") is False or obj.get("status") == "error":
            return "error", str(obj.get("error") or obj.get("suggest") or "tool_error")
        if status_attr == "error":
            return "error", "tool_error"
        return "ok", ""
    if status_attr == "error":
        return "error", _clip(text, 120)
    return "ok", ""


# ---------------------------------------------------------------------------
# 单次运行统计（控制台摘要 + streak 风控；全量 trace 由 LangSmith 云端负责）
# ---------------------------------------------------------------------------


@dataclass
class _RunStats:
    run_id: str
    thread_id: str
    started: float = field(default_factory=time.monotonic)
    rounds: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    last_err_key: str = ""
    err_streak: int = 0


_current: ContextVar[_RunStats | None] = ContextVar("run_trace_stats", default=None)


def _last_message_preview(messages: Any, n: int = _LLM_IN_CLIP) -> str:
    try:
        msgs = list(messages or [])
        if not msgs:
            return ""
        m = msgs[-1]
        if isinstance(m, dict):
            role, content = m.get("type", "?"), m.get("content", "")
        else:
            role, content = getattr(m, "type", "?"), getattr(m, "content", "")
        return f"{role}: {_clip(_content_text(content), n)}"
    except Exception:  # noqa: BLE001
        return ""


def _response_preview(response: Any) -> tuple[str, list[str]]:
    try:
        for m in reversed(list(getattr(response, "result", None) or [])):
            if isinstance(m, AIMessage):
                text = _clip(_content_text(m.content), _LLM_OUT_CLIP)
                calls = [
                    f"{tc.get('name')}({_clip(tc.get('args') or {}, 200)})"
                    for tc in (m.tool_calls or [])
                ]
                return text, calls
    except Exception:  # noqa: BLE001
        pass
    return "", []


class RunTraceMiddleware(AgentMiddleware):
    """进程内观测辅助：控制台浓缩摘要 + 连续同类工具软错误 streak 风控。

    监听钩子：awrap_model_call / awrap_tool_call（LangChain AgentMiddleware 异步变体）。
    全量 trace（模型/工具 I/O、耗时、token、执行顺序）由 LangSmith 云端承担，本中间件不重复。
    """

    name = "RunTraceMiddleware"

    # ---- 生命周期 ----

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        settings = get_settings()
        rid = _run_id_fallback() or f"run_{time.time_ns()}"
        stats = _RunStats(run_id=rid, thread_id=_thread_id())
        _current.set(stats)
        goal = _last_human_text(state)
        logger.info("\n━━━━ ▶ 运行开始 run={} thread={} goal={} ━━━━", rid, stats.thread_id, goal)
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        stats = _current.get()
        final = ""
        try:
            for m in reversed(list(state.get("messages") or [])):
                if isinstance(m, AIMessage) and _content_text(m.content).strip():
                    final = _clip(_content_text(m.content), 300)
                    break
        except Exception:  # noqa: BLE001
            pass
        thread = stats.thread_id if stats and stats.thread_id and stats.thread_id != "unknown" \
            else _thread_id()
        dur = time.monotonic() - stats.started if stats else 0.0
        if stats is not None:
            logger.info(
                "━━━━ ■ 运行结束 run={} thread={} 轮次={} 工具={} 错误={} 耗时={:.1f}s ━━━━",
                stats.run_id, thread, stats.rounds, stats.tool_calls,
                stats.tool_errors, dur,
            )
        _current.set(None)
        return None

    # ---- 模型调用 ----

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        settings = get_settings()
        stats = _current.get()
        round_no = (stats.rounds + 1) if stats else "?"
        msgs = request.messages or []
        input_preview = _last_message_preview(msgs)
        logger.info("🧠 LLM输入 ▸ 第{}轮 msgs={}｜最新: {}", round_no, len(msgs), input_preview or "(无)")
        t0 = time.monotonic()
        response = await handler(request)
        if stats is not None:
            stats.rounds += 1
        usage = _usage_of(response)
        out_preview, calls = _response_preview(response)
        logger.info("🤖 LLM输出 ▸ 第{}轮 {:.1f}s{}｜文本: {}",
                    round_no, time.monotonic() - t0,
                    f" tokens={usage}" if usage else "", out_preview or "(无文本)")
        if calls:
            logger.info("🤖→🔧 计划调用 ▸ 第{}轮: {}", round_no, "; ".join(calls))
        return response

    # ---- 工具调用 ----

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        settings = get_settings()
        stats = _current.get()
        if stats is not None:
            stats.tool_calls += 1
        tc = request.tool_call
        tool = str(tc.get("name") or "tool")
        args = tc.get("args") or {}
        logger.info("🔧 工具调用 ▸ {} args={}", tool, _clip(args, _TOOL_ARGS_CLIP))
        t0 = time.monotonic()
        try:
            result = await handler(request)
        except Exception as exc:  # 硬异常
            if stats is not None:
                stats.tool_errors += 1
            logger.error("❌ 工具异常 ▸ {} args={}｜err={}", tool, _clip(args, 200), exc)
            raise

        kind, err_code = _inspect_tool_result(result)
        ret = _clip(_tool_text(result), _TOOL_RET_CLIP)
        if kind == "error":
            if stats is not None:
                stats.tool_errors += 1
            escalate = stats is not None and self._bump_streak(
                stats, f"{tool}:{err_code}", settings)
            tail = (f"｜⚠️ 连续同类失败 {stats.err_streak} 次，疑似环境不可用，应停止重试并提示用户"
                    if escalate else "")
            logger.warning("↩ 工具返回 ▸ {} ✗ {}｜结果: {}{}", tool, err_code, ret, tail)
        else:
            if stats is not None:
                stats.last_err_key = ""
                stats.err_streak = 0
            logger.info("↩ 工具返回 ▸ {} ✓｜结果: {}", tool, ret)
        return result

    def _bump_streak(self, stats: _RunStats, key: str, settings: Settings) -> bool:
        if key == stats.last_err_key:
            stats.err_streak += 1
        else:
            stats.last_err_key = key
            stats.err_streak = 1
        return stats.err_streak >= settings.tool_error_streak


__all__ = ["RunTraceMiddleware", "init_tracing"]