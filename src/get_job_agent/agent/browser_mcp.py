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
# 默认连接「最后使用过的、装有该扩展的浏览器 profile」；装有扩展的浏览器不止一个时，
# 可加 --profile-dir-name=Profile N 指定（Edge 地址栏 edge://version 可查当前 profile 名）。
# 若未装扩展，可回退 CDP 模式：args 改为 ["@playwright/mcp@latest", "--cdp-endpoint=msedge"]。
_MCP_SERVERS: dict[str, dict[str, Any]] = {
    "playwright": {
        "transport": "stdio",
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--extension"],
    },
}

_tools_cache: list[Any] | None = None
_client: Any = None


async def init_browser_mcp_tools() -> list[Any]:
    """连接 Playwright MCP 并返回按需选定的工具列表（进程内缓存）。

    连接失败（未装 Playwright 扩展 / 浏览器未开 / npx 拉包失败等）时降级返回空列表，
    由调用方决定：无浏览器能力的 agent 仍可做聊天/简历类任务。
    """
    global _tools_cache, _client
    if _tools_cache is not None:
        return _tools_cache
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(_MCP_SERVERS)
    _client = client
    try:
        # 加整体超时：MCP 服务器启动/握手慢（npx 拉包、扩展等待）时不能阻塞 server 启动，
        # 超时按「未就绪」降级，浏览器能力缺失但 server 照常服务。
        all_tools = await asyncio.wait_for(client.get_tools(), timeout=60.0)
    except TimeoutError:
        logger.warning("Playwright MCP 连接超时（60s），浏览器能力降级为不可用")
        all_tools = []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Playwright MCP 连接失败（浏览器扩展未连接？）: {}", exc)
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
