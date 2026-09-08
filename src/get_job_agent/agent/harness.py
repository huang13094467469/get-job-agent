"""Deep Agents harness 封装：按 deepagents 标准构建可持久化、带原生 HITL 的浏览器 Agent。

对齐 deepagents 标准的关键能力：
- checkpointer + thread_id：会话状态持久化，支持跨轮对话、summarization、断点续跑；
- interrupt_on：打招呼发送 send_greeting 走框架原生 human-in-the-loop（approve/edit/reject），
  不再用「文本标记块 + 外部发送」手搓 HITL；
- memory(AGENTS.md) + skills(SKILL.md)：SOP 常驻记忆 + 按需加载的进阶打法，不再把
  整段 SOP 硬塞进 system_prompt；
- 单例编译图：一个 graph 服务多个 thread，状态由 checkpointer 按 thread_id 隔离。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tiktoken
from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware import SummarizationMiddleware, create_summarization_tool_middleware
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import MemorySaver

from ..core.config import Settings
from ..core.logs import logger
from .observability import RunTraceMiddleware
from .state import JobAgentState
from .tool_retry import BrowserToolRetryMiddleware

# tiktoken cl100k_base 近似统计（DeepSeek / OpenAI 兼容接口均可用作估算）
_ENC = tiktoken.get_encoding("cl100k_base")

# ---- 长期记忆 / 技能目录：随仓库落盘的 agent_resources，经 FilesystemBackend 读取 ----
# harness.py 位于 src/get_job_agent/agent/，parents[3] 即项目根目录。
_SERVER_DIR = Path(__file__).resolve().parents[3]  # 项目根目录
AGENT_RESOURCES_DIR = _SERVER_DIR / "agent_resources"
MEMORY_FILES = ["/AGENTS.md"]  # 常驻注入：身份 + SOP + 约束（操作经验已停用）
SKILL_DIRS = ["/skills/"]              # 按需加载：进阶打法与异常处置

# 写权限全局禁写：AGENTS.md、skills 与记忆均只读，防改坏 SOP（操作经验/自更新记忆已停用）。
PERMISSIONS = [
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

# ---- 工具面收窄 + 无 subagent 的正规开关（对齐 subagents.md / profiles.md）----
# 保留 ls/read_file：SkillsMiddleware 需要 read_file 按需读 SKILL.md 正文（level-2）。
# 保留 write_file/edit_file：随工具集保留（见 PERMISSIONS，写权限已全局禁写）。
# 排除 delete/glob/grep/execute：本 Agent 不需删文件/搜索/执行。
# P1-4：「无 subagent」改用官方正规开关
#   general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False) + 不传同步 subagents
#   → SubAgentMiddleware（与 task 工具）根本不装配，省掉永不使用的子代理图编译与提示注入。
#   task 因此**移出** excluded_tools（隐藏工具属非官方路径，且子代理图仍会被编译）。
# P2-7：profile 注册在 provider 级 "openai" 键，会与内置 OpenAI profile **合并**（非替换，
#   见 profiles.md Merge semantics）。本地 Qwen / DeepSeek 都经 init_chat_model("openai:...")
#   命中此合并；excluded_tools 取集合并、general_purpose_subagent 字段级合并，
#   enabled=False 不会被内置 profile 顶掉。
register_harness_profile(
    "openai",
    HarnessProfile(
        excluded_tools=frozenset({"delete", "glob", "grep", "execute"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)

# 精简系统提示词：身份/意图分流/SOP/过目清单/风控由常驻记忆 AGENTS.md 承载（MemoryMiddleware 必然注入），
# 系统提示词只保留浏览器工具清单（AGENTS.md 未承载）+ 一行指向，避免重复。
# 浏览器操作经 Playwright MCP 官方工具（browser_snapshot/click/type/press_key/navigate/wait_for/find）。
BROWSER_SYSTEM_PROMPT = (
    "你是 Get-Job 求职助手。身份、意图分流（找工作/投递 vs 聊天/咨询/分析）、逐岗 SOP、"
    "过目清单与风控约束均以常驻记忆 AGENTS.md 为准——先判断意图再行动。\n"
    "浏览器操作由 Playwright MCP 官方工具驱动，元素用 ref 定位："
    "browser_snapshot 看页面（元素带 ref）、browser_find 在长列表中按文本定位、"
    "browser_click/type/hover/drag/press_key 交互、browser_fill_form/select_option 填表/选下拉、"
    "browser_navigate/navigate_back 跳转、browser_tabs 管理标签页、"
    "browser_handle_dialog 处理弹框、browser_file_upload 上传、browser_take_screenshot 截图、"
    "browser_console_messages/network_requests/network_request 读控制台/网络、"
    "browser_evaluate/run_code_unsafe 执行 JS（如滚动无限列表、读 SPA 状态）、"
    "browser_wait_for 等待、browser_close/resize 收尾。"
)


_LMSTUDIO_PLACEHOLDER_KEY = "lm-studio"  # 本地服务常忽略 key，用占位避免 OpenAI 客户端报空


# ---- 上下文体积观测（中间件式回调，打印每次模型调用送入的上下文规模）----

def _content_to_text(content: Any) -> str:
    """把消息 content（str 或内容块列表）拍平成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input") or {}, ensure_ascii=False))
                elif "text" in block:
                    parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts)
    return str(content or "")


def _message_to_text(m: Any) -> str:
    """消息全文（content + tool_calls 的 name/args），用于统计。兼容 BaseMessage 与裸 dict。"""
    if isinstance(m, dict):
        content = m.get("content") or ""
        parts = [_content_to_text(content)]
        for tc in m.get("tool_calls") or []:
            parts.append(str(tc.get("name") or ""))
            parts.append(json.dumps(tc.get("args", {}), ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    parts = [_content_to_text(m.content)]
    for tc in getattr(m, "tool_calls", None) or []:
        parts.append(str(tc.get("name") or ""))
        parts.append(json.dumps(tc.get("args", {}), ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _flatten_msgs(messages: Any) -> list:
    """on_chat_model_start 的 messages 可能是 List[Message] 或 List[List[Message]]。"""
    if messages and isinstance(messages[0], (list, tuple)):
        out: list = []
        for group in messages:
            out.extend(group)
        return out
    return list(messages or [])


def estimate_tokens(text: str) -> int:
    try:
        return len(_ENC.encode(text))
    except Exception:  # noqa: BLE001
        return max(1, len(text) // 2)


class ContextSizeHandler(BaseCallbackHandler):
    """在每次 ChatModel 被调用时打印送入的完整上下文规模（消息 + 工具定义）。"""

    def on_chat_model_start(
        self, serialized: dict, messages: list, *, run_id: str, **kwargs: Any
    ) -> None:
        try:
            msgs = _flatten_msgs(messages)
            per_role: dict[str, tuple[int, int]] = {}
            for m in msgs:
                txt = _message_to_text(m)
                if isinstance(m, dict):
                    role = str(m.get("type", "unknown"))
                else:
                    role = str(getattr(m, "type", "unknown"))
                ch, tk = per_role.get(role, (0, 0))
                per_role[role] = (ch + len(txt), tk + estimate_tokens(txt))
            msg_chars = sum(c for c, _ in per_role.values())
            msg_tokens = sum(t for _, t in per_role.values())

            # 工具定义随请求一起送入模型，但不在 messages 里，需单独统计
            # （deepagents 经 bind_tools 传递，落在 invocation_params.tools）
            tool_tokens, tool_chars = 0, 0
            ip = kwargs.get("invocation_params") or {}
            tools = ip.get("tools") if isinstance(ip, dict) else None
            if not tools:
                tools = kwargs.get("tools")
            for t in tools or []:
                s = json.dumps(t, ensure_ascii=False) if not isinstance(t, (str, bytes)) else str(t)
                tool_tokens += estimate_tokens(s)
                tool_chars += len(s)

            breakdown = " | ".join(f"{r}:{tk}tok/{ch}ch" for r, (ch, tk) in per_role.items())
            logger.info(
                "LLM上下文 msgs={} 消息≈{}tok/{}ch 工具{}个≈{}tok/{}ch 合计≈{}tok [{}]",
                len(msgs), msg_tokens, msg_chars,
                len(tools or []), tool_tokens, tool_chars,
                msg_tokens + tool_tokens, breakdown,
            )
        except Exception as exc:  # noqa: BLE001  观测失败不影响主流程
            logger.warning("上下文统计失败: {}", exc)


def build_model(settings: Settings):
    """构建对话/工具调用模型（本地 Qwen，走 OpenAI 兼容接口，无 api-key）。"""
    api_key = settings.llm_api_key or _LMSTUDIO_PLACEHOLDER_KEY
    return init_chat_model(
        f"openai:{settings.llm_model}",
        api_key=api_key,
        base_url=settings.llm_base_url,
    )


def ctx_config(
    thread_id: str | None = None,
    recursion_limit: int = 80,
    *,
    run_name: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """为 agent 运行传入的 config：上下文观测回调 + 会话 thread_id + LangSmith 归类元数据。

    同一 thread_id 的多次 invoke/astream 共享 checkpointer 里的状态，实现跨轮对话、
    原生 HITL 的暂停/恢复、以及 SummarizationMiddleware 的历史压缩。
    metadata/tags/run_name 会随 LangSmith trace 上报，便于按会话检索与评估。
    """
    cfg: dict[str, Any] = {"callbacks": [ContextSizeHandler()], "recursion_limit": recursion_limit}
    configurable: dict[str, Any] = {}
    if thread_id:
        configurable["thread_id"] = thread_id
        cfg["metadata"] = {"thread_id": thread_id, "agent": "get-job-browser"}
        cfg["tags"] = [*(tags or []), "get-job-agent"]
        cfg["run_name"] = run_name or "get_job_browser_agent"
    if configurable:
        cfg["configurable"] = configurable
    return cfg


# 需要人工确认的写类环节：confirm 模式下「打招呼预检」后暂停，允许用户批准/修改话术/拒绝。
# （发送动作已由模型用 MCP 浏览器工具完成；check_greeting 是发送前的确认挂点）
INTERRUPT_ON: dict[str, Any] = {
    "check_greeting": {"allowed_decisions": ["approve", "edit", "reject"]},
}

# 编译图跨会话复用（图本身无会话状态，状态全在 checkpointer 按 thread_id 隔离）。
_AGENT_CACHE: dict[str, Any] = {}
# 兜底 checkpointer：仅当持久 Postgres saver 未初始化（离线测试 / Postgres 不可达降级）时使用。
_FALLBACK_CHECKPOINTER: MemorySaver | None = None


def get_checkpointer():
    """返回持久 checkpointer（Postgres AsyncPostgresSaver，由 lifespan 初始化）。

    对齐 going-to-production.md Durability：持久 checkpoint 支撑崩溃续跑、time travel、HITL
    暂停点跨重启精确恢复。未初始化时兜底进程级 MemorySaver（易失，仅保证可运行）。
    """
    from ..infra.checkpoint import get_checkpointer as _get_pg_saver

    saver = _get_pg_saver()
    if saver is not None:
        return saver
    global _FALLBACK_CHECKPOINTER
    if _FALLBACK_CHECKPOINTER is None:
        _FALLBACK_CHECKPOINTER = MemorySaver()
        logger.warning("持久 checkpointer 未初始化，兜底 MemorySaver（易失，重启丢状态）")
    return _FALLBACK_CHECKPOINTER


# ---- 旧对话自动压缩（防上下文膨胀）----
# 阈值必须高于单步基线（system 常驻 ~7.5k + 一页快照 delta 常有 5–9k），否则几乎每步都
# 触发重摘要（本地模型慢、还会把“刚进聊天页就要发”的当轮意图摘要掉→误 go_back）。
# 因此只在真正逼近窗口时压一次；keep 多留几条以保住当前岗位的近几轮上下文。
_SUMMARY_TRIGGER = [("tokens", 30000)]
_SUMMARY_KEEP_MESSAGES = 14


def _build_summarization(model, backend) -> SummarizationMiddleware:
    """自定义阈值的 SummarizationMiddleware；与 deepagents 默认同名→就地替换。

    用同一个（本地）模型做摘要，trim 上限防止摘要本身过大。
    """
    return SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=_SUMMARY_TRIGGER,
        keep=("messages", _SUMMARY_KEEP_MESSAGES),
    )


# ---- P0-3 失控循环封顶：官方限额中间件（替代 ws.py 手搓 break）----
# run_limit 每次 invoke（每段 astream）重置；外层「自动续跑」据此干净地开启下一段。
# ModelCallLimit 对齐原单段 40 轮模型调用；ToolCallLimit 取每轮多个浏览器工具的合理上界，
# 对「单 DOM 执行器、每轮多工具」的闭环，工具调用数是更直接的失控信号。
_MODEL_CALL_LIMIT = 40
_TOOL_CALL_LIMIT = 160


async def _on_tool_error(exc: Exception, request: Any) -> str | None:
    """ToolErrorMiddleware 回调（P2-9）：把已知可恢复的工具异常转成 error ToolMessage 让模型自愈。

    浏览器工具的失败是结构化软错误（ok:false，由 BrowserToolRetryMiddleware 处理），不抛异常；
    这里主要兜非浏览器工具（简历解析/比对等）真抛出的异常，避免整段 halt。仅收敛「可恢复」类
    （值/键/超时/连接），其余返回 None 让其冒泡——不吞真正的 bug（fault-tolerance.md Unexpected）。
    """
    try:
        name = str((request.tool_call or {}).get("name") or "tool")
    except Exception:  # noqa: BLE001
        name = "tool"
    if isinstance(exc, (ValueError, KeyError, TimeoutError, ConnectionError)):
        return f"工具 `{name}` 执行失败：{type(exc).__name__}: {exc}。请检查输入后重试或换一步。"
    return None


def _build_backend() -> CompositeBackend:
    """CompositeBackend：default 用 FilesystemBackend 承载根级 memory/skills，产物走 StateBackend。

    修正（此前 P1-5 用 default=StateBackend + 把 /AGENTS.md 路由到 fs 会运行时报错
    "Failed to download /AGENTS.md: is_directory"）：

    CompositeBackend 是「挂载」语义——每个 route 前缀是一个挂载点，绑定的 backend 的根即该挂载点，
    因此**只能按目录挂载**，无法对单个根级文件 /AGENTS.md 建路由。根级 AGENTS.md / memories /
    skills 必须由 default（真实磁盘 FilesystemBackend）承载，否则 memory 中间件的
    download_files('/AGENTS.md') 会落到空的 StateBackend 而失败（is_directory / file_not_found）。

    故把内部产物 /conversation_history/ 与 /large_tool_results/ 显式路由到 ephemeral
    StateBackend：产物随 thread checkpoint 持久、不写进 agent_resources 磁盘、不污染仓库
    （对齐 backends.md：offloaded 大结果与会话历史写进 default backend 的告警）。
    """
    fs = FilesystemBackend(root_dir=str(AGENT_RESOURCES_DIR), virtual_mode=True)
    return CompositeBackend(
        default=fs,
        routes={
            "/conversation_history/": StateBackend(),
            "/large_tool_results/": StateBackend(),
        },
    )


def _browser_tools() -> list:
    """Agent 可见工具 = Playwright MCP 浏览器工具 + 求职业务工具。

    浏览器工具来自 lifespan 初始化的 MCP 缓存（见 agent.browser_mcp）；MCP 未就绪
    （扩展未连/失败）时仅业务工具，agent 仍可做聊天/简历类任务，浏览器操作会缺。
    """
    from .browser_mcp import get_cached_browser_tools
    from .tools import BROWSER_TOOLS

    return [*get_cached_browser_tools(), *BROWSER_TOOLS]


def _build_browser_agent(settings: Settings):
    """按 deepagents 标准构建浏览器 Agent（持久 checkpointer + HITL + memory + skills）。"""
    from deepagents import create_deep_agent

    model = build_model(settings)
    backend = _build_backend()
    tools = _browser_tools()
    # unattended（无人值守）不挂发送前暂停，check_greeting 直接通过；confirm 才启用 HITL。
    unattended = str(getattr(settings, "agent_mode", "confirm")).lower() != "confirm"
    interrupt_on = None if unattended else INTERRUPT_ON
    logger.info(
        "构建 DeepAgent 工具={} mode={} interrupt={} memory={} skills={} backend=Composite res={}",
        len(tools), "unattended" if unattended else "confirm",
        bool(interrupt_on), MEMORY_FILES, SKILL_DIRS, AGENT_RESOURCES_DIR,
    )
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=BROWSER_SYSTEM_PROMPT,
        checkpointer=get_checkpointer(),
        interrupt_on=interrupt_on,
        backend=backend,
        permissions=PERMISSIONS,
        memory=MEMORY_FILES,
        skills=SKILL_DIRS,
        # P0-2：custom state schema —— 计数/去重/暂存岗位随 thread checkpoint 持久（替代全局 dict）
        state_schema=JobAgentState,
        middleware=[
            # 全链路观测 + 任务规划
            RunTraceMiddleware(),
            TodoListMiddleware(),
            # P0-3：官方限额中间件成对封顶失控循环（替代 ws.py 手搓 break）；run_limit 每段重置，
            # 超限是干净结束信号，由外层「自动续跑」捕获后开启下一段。
            ModelCallLimitMiddleware(run_limit=_MODEL_CALL_LIMIT),
            ToolCallLimitMiddleware(run_limit=_TOOL_CALL_LIMIT),
            # 浏览器工具软错误（ok:false）瞬时退避重试（自研，正当）
            BrowserToolRetryMiddleware(),
            # P2-9：非浏览器工具的可恢复异常转 error ToolMessage 让模型自愈，未知异常冒泡不吞
            ToolErrorMiddleware(aon_error=_on_tool_error),
            # 旧对话自动压缩（同名实例就地替换默认）+ P2-11 主动压缩工具 compact_conversation
            _build_summarization(model, backend),
            create_summarization_tool_middleware(model, backend),
        ],
    )


def get_browser_agent(settings: Settings):
    """返回按模型+模式缓存的单例编译图（一个 graph 服务多个 thread）。

    图本身无会话状态，状态全在 checkpointer（按 thread_id 隔离），故可安全复用。
    不同 agent_mode（confirm/unattended）对应不同图（是否挂 HITL），分开缓存。
    """
    key = f"{settings.llm_provider}:{settings.llm_model}:{str(settings.agent_mode).lower()}"
    agent = _AGENT_CACHE.get(key)
    if agent is None:
        agent = _build_browser_agent(settings)
        _AGENT_CACHE[key] = agent
    return agent


def build_browser_agent(settings: Settings):
    """向后兼容别名：等价于 get_browser_agent（返回单例）。"""
    return get_browser_agent(settings)


__all__ = [
    "build_model",
    "build_browser_agent",
    "get_browser_agent",
    "get_checkpointer",
    "ctx_config",
    "BROWSER_SYSTEM_PROMPT",
]