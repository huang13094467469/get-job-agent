/**
 * Service Worker（扩展薄壳中继）
 * - 仅做扩展内部消息路由 + 状态缓存；不持有常驻 WS（规避 MV3 30s 休眠）
 * - 转发页面 Content Script 状态、提供 /health 探测、向页内 CS 下发指令
 */
const SERVER_HEALTH = 'http://127.0.0.1:8791/health'
const PAGE_ID = 'zhipin'

// 点击扩展图标 → 直接打开控制侧边栏（MV3 仅此手势可稳定触发 sidePanel 打开，
// popup 内按钮调用 chrome.sidePanel.open() 会被忽略，故不再用 popup 作为入口）
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {})

// 缓存最近一次页面状态
const pageStatus = { connected: false, at: null }

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message) return
  switch (message.type) {
    case 'page_status':
      // 来自 Content Script 的连接状态回执
      pageStatus.connected = Boolean(message.payload?.ok)
      pageStatus.at = new Date().toISOString()
      sendResponse({ ok: true })
      break

    case 'health_check':
      // 探测 Agent Server 连通性
      fetch(SERVER_HEALTH)
        .then((r) => r.json())
        .then((data) => sendResponse({ ok: true, data }))
        .catch((e) => sendResponse({ ok: false, error: String(e) }))
      return true // 保持通道以支持异步响应

    case 'send_dom':
      // 向页内 CS 下发 DOM 指令
      chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
        if (!tab?.id) return sendResponse({ ok: false, error: 'no_tab' })
        chrome.tabs
          .sendMessage(tab.id, message.payload)
          .then(sendResponse)
          .catch((e) => sendResponse({ ok: false, error: String(e) }))
      })
      return true

    case 'get_status':
      sendResponse({ ok: true, data: { pageStatus } })
      break

    default:
      sendResponse({ ok: false, error: 'unknown_message' })
  }
})