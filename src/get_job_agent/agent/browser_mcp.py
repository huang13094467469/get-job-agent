"""Playwright MCP 浏览器接入：把官方 MCP 服务器暴露的浏览器工具接入 DeepAgent。

设计（对齐「官方标准替代自研手搓」）：
- 通过 langchain-mcp-adapters 把 Playwright MCP 的工具包装成 langchain 工具；
- **按需选子集**：第一批只接入求职闭环需要的核心 8 个工具（工具多则 token 贵、易误调）；
  后期要扩展新功能时，把对应的 MCP 工具名加进 ALLOWED_MCP_TOOLS 即可；
- 部署形态 `--extension`：MCP 通过 Playwright 官方扩展连接用户已登录的浏览器标签页，
  复用登录态/SSO/2FA，无需自研 Content Script 桥。

连接生命周期：lifespan 启动时初始化一次（工具列表进程内缓存复用），进程退出自然终止
stdio 子进程；close_browser_mcp 供 shutdown 时兜底释放。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.logs import logger

# MCP 连接重试：--extension 模式握手很吃"浏览器恰好就绪"的时机（扩展 service worker 未常驻、
# npx 拉包慢都会导致列工具阶段偶发 TaskGroup 失败）。失败后延迟重连，能大幅压低随机失败率。
_MCP_CONNECT_ATTEMPTS: int = 3          # 总尝试次数（含首次）
_MCP_CONNECT_RETRY_DELAY: float = 3.0   # 每次失败后重试前的等待秒数（幂等等待）
_MCP_CONNECT_TIMEOUT: float = 60.0      # 单次握手/取工具超时

# 接入的 MCP 工具：Playwright MCP 的**完整核心工具集**（core，始终启用，见官方
# introduction.mdx「Available Tools → Core」）。按用途分组，覆盖：导航 / 快照定位 /
# 点击输入 / 表单 / 键盘鼠标 / 截图 / 标签页 / 对话框 / 文件上传 / 控制台 / 网络 / 执行 / 等待。
# 后续若需要 network/storage/testing/vision/pdf/devtools 等能力组，需在 _MCP_SERVERS 的
# args 里加对应 --caps，并在此追加对应工具名（能力组默认关闭，按需开启以控 token 成本）。
ALLOWED_MCP_TOOLS: frozenset[str] = frozenset({
    # ---- 导航 ----
    "browser_navigate",         # 跳转 URL
    "browser_navigate_back",    # 后退
    # ---- 看页面 / 定位 ----
    "browser_snapshot",         # 可访问性快照（元素 ref + 文本），决策前「看」页面
    "browser_find",             # 大页面按文本/正则找目标元素子树（省 token）
    # ---- 点击 / 输入 ----
    "browser_click",            # 点击元素（ref 或 selector）
    "browser_hover",            # 悬停元素
    "browser_drag",             # 元素间拖拽
    "browser_drop",             # 向元素投放文件 / MIME 数据
    "browser_type",             # 向输入框输入文本
    "browser_press_key",        # 按键（Enter/Tab 等）
    # ---- 表单 ----
    "browser_fill_form",        # 一次填多个表单字段
    "browser_select_option",    # 选择下拉选项
    # ---- 截图 ----
    "browser_take_screenshot",  # 截图（视觉兜底，需配合视觉模型）
    # ---- 标签页 ----
    "browser_tabs",             # 列出/新建/关闭/切换标签页
    # ---- 对话框 / 上传 ----
    "browser_handle_dialog",    # 接受/关闭系统对话框
    "browser_file_upload",      # 向文件选择器上传文件
    # ---- 控制台 / 网络 ----
    "browser_console_messages", # 读取控制台输出
    "browser_network_requests", # 列出网络请求
    "browser_network_request",  # 查看单个请求详情
    # ---- 执行 ----
    "browser_evaluate",         # 在页面/元素上执行 JS（滚动、读 SPA 状态等）
    "browser_run_code_unsafe",  # 执行一段 Playwright 代码片段
    # ---- 等待 / 收尾 ----
    "browser_wait_for",         # 等待文本/时间
    "browser_close",            # 关闭当前页面
    "browser_resize",           # 调整浏览器窗口大小
})

# Playwright MCP 服务器配置：npx 启动 + --extension 连接用户已登录浏览器（扩展模式）。
# 本机扩展装在 Edge 的 Default profile：用 --browser=msedge 强制落 Edge，--profile-dir-name=Default
# 指定 profile（= edge://version 中“配置文件路径”的最后一个目录名，这里是 Default），
# 避免扩展模式误去 Chrome 目录找扩展。若日后换了浏览器/profile，同步改这两个值即可。
_MCP_SERVERS: dict[str, dict[str, Any]] = {
    "playwright": {
        "transport": "stdio",
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--extension", "--browser=msedge", "--profile-dir-name=Default"],
    },
}

_tools_cache: list[Any] | None = None
_client: Any = None


async def _connect_once() -> list[Any]:
    """单次连接 Playwright MCP 并返回全部（未过滤）工具；失败抛异常由调用方重试。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    global _client
    client = MultiServerMCPClient(_MCP_SERVERS)
    _client = client
    # 加整体超时：MCP 服务器启动/握手慢（npx 拉包、扩展等待）时不能阻塞 server 启动，
    # 超时按「未就绪」降级，浏览器能力缺失但 server 照常服务。
    return await asyncio.wait_for(client.get_tools(), timeout=_MCP_CONNECT_TIMEOUT)


async def init_browser_mcp_tools() -> list[Any]:
    """连接 Playwright MCP 并返回按需选定的工具列表（进程内缓存）。

    连接失败（未装 Playwright 扩展 / 浏览器未开 / npx 拉包失败等）时降级返回空列表，
    由调用方决定：无浏览器能力的 agent 仍可做聊天/简历类任务。

    --extension 握手偶发失败，故失败后延迟重试几次（每次新建 client），再降级。
    """
    global _tools_cache, _client
    if _tools_cache is not None:
        return _tools_cache

    all_tools: list[Any] = []
    last_exc: Exception | None = None
    for attempt in range(1, _MCP_CONNECT_ATTEMPTS + 1):
        try:
            all_tools = await _connect_once()
            last_exc = None
            if all_tools:
                break
        except TimeoutError as exc:
            last_exc = exc
            logger.warning(
                "Playwright MCP 连接超时（{}s，第 {}/{} 次）", _MCP_CONNECT_TIMEOUT,
                attempt, _MCP_CONNECT_ATTEMPTS,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "Playwright MCP 连接失败（第 {}/{} 次）: {}", attempt,
                _MCP_CONNECT_ATTEMPTS, exc,
            )
        if attempt < _MCP_CONNECT_ATTEMPTS:
            logger.info("Playwright MCP 将于 {}s 后重试...", _MCP_CONNECT_RETRY_DELAY)
            await asyncio.sleep(_MCP_CONNECT_RETRY_DELAY)

    if last_exc is not None:
        # 全部尝试均失败：清理当前 client，避免留存损坏的 stdio 子进程句柄。
        await close_browser_mcp()
        logger.warning("Playwright MCP 全部 {} 次连接失败，浏览器能力降级为不可用",
                       _MCP_CONNECT_ATTEMPTS)
        all_tools = []
    elif not all_tools:
        # 连上了但没拿到任何工具（理论兜底），同样按不可用处理。
        all_tools = []

    picked = [t for t in all_tools if getattr(t, "name", "") in ALLOWED_MCP_TOOLS]
    missing = ALLOWED_MCP_TOOLS - {getattr(t, "name", "") for t in picked}
    if missing:
        logger.warning("Playwright MCP 未提供以下工具（检查 --caps 配置）: {}", sorted(missing))
    _tools_cache = picked
    logger.info(
        "Playwright MCP 就绪：接入 {} 个浏览器工具 {}", len(picked),
        sorted(t.name for t in picked),
    )
    return _tools_cache


def get_cached_browser_tools() -> list[Any]:
    """返回已初始化的 MCP 浏览器工具（未初始化/失败时为空列表）。"""
    return list(_tools_cache or [])


async def close_browser_mcp() -> None:
    """释放 MCP 连接（shutdown 兜底；正常进程退出时 stdio 子进程随父进程终止）。"""
    global _client
    if _client is None:
        return
    try:
        sess = getattr(_client, "session", None)
        sessions = sess.values() if isinstance(sess, dict) else ([sess] if sess else [])
        for s in sessions:
            if hasattr(s, "aclose"):
                await s.aclose()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _client = None


__all__ = ["ALLOWED_MCP_TOOLS", "init_browser_mcp_tools", "get_cached_browser_tools",
           "close_browser_mcp"]
