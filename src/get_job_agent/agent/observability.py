"""Agent 运行观测：结构化中间件日志 + 本地 JSONL trace + LangSmith 追踪开关。

设计（双轨，均默认本地优先、LangSmith 可选外发）：
1. LangSmith 追踪：init_tracing() 依据配置写入 LANGSMITH_*/LANGCHAIN_* 环境变量，
   create_deep_agent（LangGraph 运行时）即自动把每一步（模型/工具/耗时/token）上报，
   在 LangSmith Studio 查看 trace 树、拉数据集、跑评估——零侵入。
2. RunTraceMiddleware：不依赖外网的本地观测。记录整轮运行的正常/异常事件，
   尤其能识别「工具调用成功返回、但结果体里是软错误」（如 page_not_connected），
   这类错误框架的 ToolErrorMiddleware 抓不到。并统计连续同类软错误，超阈值升级告警
   （定位「页面断开却反复重试」）。事件同时写 loguru 与本地 JSONL（供离线回放/评估）。

deepagents 会把本中间件并入其栈（在 PatchToolCalls 之后），不改动模型可见工具面。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
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

_MAX_TEXT = 500  # 单条日志里文本截断长度


# ---------------------------------------------------------------------------
# LangSmith 追踪开关
# ---------------------------------------------------------------------------

def init_tracing(settings: Settings | None = None) -> None:
    """按配置设置 LangSmith/LangChain 追踪环境变量（在构建/运行 agent 前调用一次）。

    同时兼容两套变量名（LANGSMITH_* 新、LANGCHAIN_* 旧），确保不同版本 client 生效。
    """
    settings = settings or get_settings()
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        if settings.langsmith_endpoint:
            os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
            os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint
        logger.info("LangSmith 追踪已开启 project={}", settings.langsmith_project)
    else:
        # 未配置则显式关闭，避免误上报或残留环境变量造成意外外发。
        os.environ.setdefault("LANGSMITH_TRACING", "false")
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
        logger.info("LangSmith 追踪未开启（本地 JSONL trace 仍按配置生效）")


# ---------------------------------------------------------------------------
# 本地 JSONL trace 落盘
# ---------------------------------------------------------------------------

def _emit_trace(settings: Settings, record: dict[str, Any]) -> None:
    """把一条运行事件以 JSONL 追加到本地 trace 文件（失败静默，绝不影响主流程）。"""
    if not settings.trace_local_enabled:
        return
    try:
        path = Path(settings.trace_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("本地 trace 写入失败: {}", exc)


def _thread_id() -> str:
    """从当前 LangGraph 运行配置里取 thread_id（取不到返回 unknown）。"""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str((cfg.get("configurable") or {}).get("thread_id") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


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


def _last_human_text(state: Any) -> str:
    """取 state.messages 里最后一条人类消息文本，作为本轮 goal 摘要。"""
    try:
        for m in reversed(list(state.get("messages") or [])):
            if isinstance(m, HumanMessage):
                return _clip(_content_text(m.content), 200)
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------------------------------------------------------------------------
# 单次运行统计（contextvar 隔离：每个 astream/resume 是独立 asyncio 任务，天然并发安全）
# ---------------------------------------------------------------------------

@dataclass
class _RunStats:
    thread_id: str
    started: float = field(default_factory=time.monotonic)
    rounds: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    last_err_key: str = ""
    err_streak: int = 0


_current: ContextVar[_RunStats | None] = ContextVar("run_trace_stats", default=None)

# 控制台内容日志的截断长度（结构化全量 trace 只进文件，不刷控制台）
_LLM_IN_CLIP = 700
_LLM_OUT_CLIP = 700
_TOOL_ARGS_CLIP = 300
_TOOL_RET_CLIP = 800


def _last_message_preview(messages: Any, n: int = _LLM_IN_CLIP) -> str:
    """取本轮发给 LLM 的消息列表里最后一条（本轮新增输入）的角色+内容预览。"""
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
    """从 ModelResponse 取最后一条 AIMessage 的文本与计划调用的工具。"""
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
    """控制台打印每轮真实内容（LLM 输入/输出、工具调用/返回）；结构化 trace 只落文件。"""

    name = "RunTraceMiddleware"

    # ---- 生命周期 ----

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        stats = _RunStats(thread_id=_thread_id())
        _current.set(stats)
        settings = get_settings()
        goal = _last_human_text(state)
        logger.info("\n━━━━ ▶ 运行开始 thread={} goal={} ━━━━", stats.thread_id, goal)
        _emit_trace(settings, {
            "event": "run_start", "thread_id": stats.thread_id,
            "ts": time.time(), "goal": goal,
        })
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        settings = get_settings()
        stats = _current.get()
        final = ""
        try:
            for m in reversed(list(state.get("messages") or [])):
                if isinstance(m, AIMessage) and _content_text(m.content).strip():
                    final = _clip(_content_text(m.content), 300)
                    break
        except Exception:  # noqa: BLE001
            pass
        if stats is not None:
            dur = time.monotonic() - stats.started
            logger.info(
                "━━━━ ■ 运行结束 thread={} 轮次={} 工具={} 错误={} 耗时={:.1f}s ━━━━",
                stats.thread_id, stats.rounds, stats.tool_calls, stats.tool_errors, dur,
            )
            _emit_trace(settings, {
                "event": "run_end", "thread_id": stats.thread_id, "ts": time.time(),
                "rounds": stats.rounds, "tool_calls": stats.tool_calls,
                "tool_errors": stats.tool_errors, "duration_s": round(dur, 2),
                "final": final,
            })
        _current.set(None)
        return None

    # ---- 模型调用 ----

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        stats = _current.get()
        round_no = (stats.rounds + 1) if stats else "?"
        msgs = request.messages or []
        input_preview = _last_message_preview(msgs)
        logger.info("🧠 LLM输入 ▸ 第{}轮 msgs={}｜最新: {}",
                    round_no, len(msgs), input_preview or "(无)")
        t0 = time.monotonic()
        response = await handler(request)
        if stats is not None:
            stats.rounds += 1
        usage = _usage_of(response)
        out_text, calls = _response_preview(response)
        logger.info("🤖 LLM输出 ▸ 第{}轮 {:.1f}s{}｜文本: {}",
                    round_no, time.monotonic() - t0,
                    f" tokens={usage}" if usage else "", out_text or "(无文本)")
        if calls:
            logger.info("🤖→🔧 计划调用 ▸ 第{}轮: {}", round_no, "; ".join(calls))
        _emit_trace(get_settings(), {
            "event": "model_call", "thread_id": stats.thread_id if stats else "unknown",
            "ts": time.time(), "round": round_no,
            "messages": len(msgs), "usage": usage,
            "input_preview": input_preview, "output": out_text, "planned_tools": calls,
            "duration_s": round(time.monotonic() - t0, 2),
        })
        return response

    # ---- 工具调用 ----

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        stats = _current.get()
        if stats is not None:
            stats.tool_calls += 1
        tc = request.tool_call
        tool = str(tc.get("name") or "tool")
        args = tc.get("args") or {}
        logger.info("🔧 工具调用 ▸ {} args={}", tool, _clip(args, _TOOL_ARGS_CLIP))
        t0 = time.monotonic()
        settings = get_settings()

        try:
            result = await handler(request)
        except Exception as exc:  # 硬异常：工具抛出
            if stats is not None:
                stats.tool_errors += 1
            logger.error("❌ 工具异常 ▸ {} args={}｜err={}", tool, _clip(args, 200), exc)
            _emit_trace(settings, {
                "event": "tool_error", "thread_id": stats.thread_id if stats else "unknown",
                "ts": time.time(), "tool": tool, "args": _clip(args, 200),
                "error": str(exc), "kind": "exception",
                "duration_s": round(time.monotonic() - t0, 2),
            })
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

        record = {
            "event": "tool_result", "thread_id": stats.thread_id if stats else "unknown",
            "ts": time.time(), "tool": tool, "args": _clip(args, 200),
            "status": kind, "error": err_code or None,
            "result": _clip(_tool_text(result), 300),
            "duration_s": round(time.monotonic() - t0, 2),
        }
        if tool == "send_greeting":  # 无人值守自动发送时，完整留存话术供事后审计
            record["greeting"] = (args or {}).get("text") if isinstance(args, dict) else None
        _emit_trace(settings, record)
        return result

    def _bump_streak(self, stats: _RunStats, key: str, settings: Settings) -> bool:
        """连续同类软错误计数；达阈值返回 True（用于升级告警）。"""
        if key == stats.last_err_key:
            stats.err_streak += 1
        else:
            stats.last_err_key = key
            stats.err_streak = 1
        return stats.err_streak >= settings.trace_error_streak


# ---------------------------------------------------------------------------
# 工具结果解析：区分「成功」/「软错误」（结果体里 ok:false）/「异常状态」
# ---------------------------------------------------------------------------

def _tool_text(result: Any) -> str:
    content = getattr(result, "content", result)
    return _content_text(content)


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
    # 非 JSON：以 ToolMessage.status 兜底判定
    if status_attr == "error":
        return "error", _clip(text, 120)
    return "ok", ""


def _usage_of(response: Any) -> dict[str, int] | None:
    """从 ModelResponse 里抽取最后一次 AIMessage 的 token 用量。"""
    try:
        messages = getattr(response, "result", None) or []
        for m in reversed(list(messages)):
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


__all__ = ["RunTraceMiddleware", "init_tracing"]
