/**
 * Side Panel — 聊天页：直连 Agent Server WS（/ws），与 Agent 对话并实时查看浏览器工具执行。
 * - 连接状态：Server / 当前 Get-Job 页
 * - 对话：agent_go → 后台跑浏览器操作 Agent，事件流式回传（正文 + 工具卡 + 状态）
 * - 简历：上传(PDF/DOCX) / 重扫目录 / 状态展示
 * - 运行配置：无人值守开关 + 投递上限（localStorage 持久，随 agent_go 透传）
 * - 原生 HITL：confirm 模式下 send_greeting 前弹层确认
 *
 * 无消息队列：运行中再发送会直接提示，不排队（避免后端无回执时消息卡死在队列里）。
 */
const $ = (id) => document.getElementById(id)

const WS_URL = 'ws://127.0.0.1:8791/ws'

let ws = null
let connected = false
let running = false
let currentIdem = null

// 持久客户端标识：绑定会话线程，面板刷新/扩展重载后不变（连续对话）
function getClientId() {
  let id = localStorage.getItem('gja.clientId')
  if (!id) {
    id = 'c_' + Math.random().toString(36).slice(2, 12)
    try { localStorage.setItem('gja.clientId', id) } catch (e) { /* 忽略 */ }
  }
  return id
}

/* ============================ 轻量 Markdown 渲染 ============================ */

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function inline(t) {
  t = t.replace(/`([^`\n]+)`/g, (m, c) => '<code>' + c + '</code>')
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>')
  return t
}

function splitRow(line) {
  let l = String(line).trim()
  if (l.startsWith('|')) l = l.slice(1)
  if (l.endsWith('|')) l = l.slice(0, -1)
  return l.split('|').map((c) => c.trim())
}

function md(src) {
  if (!src) return ''
  const codeBlocks = []
  let s = String(src).replace(/```[^\n]*\n?([\s\S]*?)\n?```/g, (m, code) => {
    const i = codeBlocks.push(code) - 1
    return '\u0000CODE' + i + '\u0000'
  })
  s = esc(s)
  const lines = s.split('\n')
  const out = []
  let listType = null
  let listBuf = []
  let para = []

  const flushPara = () => {
    if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = [] }
  }
  const flushList = () => {
    if (listBuf.length) { out.push('<' + listType + '>' + listBuf.join('') + '</' + listType + '>'); listBuf = []; listType = null }
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trimEnd()
    const line = raw.trim()
    if (/^(---|\*\*\*|___)\s*$/.test(line)) { flushPara(); flushList(); out.push('<hr>'); continue }
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) { flushPara(); flushList(); const lv = h[1].length; out.push('<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>'); continue }
    if (/^>\s?/.test(line)) { flushPara(); flushList(); out.push('<blockquote>' + inline(line.replace(/^>\s?/, '')) + '</blockquote>'); continue }
    // 表格：当前行含 |，且下一行是纯 | - : 组成的分隔行
    const next = i + 1 < lines.length ? lines[i + 1].trim() : ''
    if (line.includes('|') && next.includes('|') && /^[\s|:\-]+$/.test(next)) {
      flushPara(); flushList()
      const body = []
      i++ // 跳过分隔行
      while (i + 1 < lines.length) {
        const nl = lines[i + 1].trim()
        if (nl.includes('|')) { body.push(nl); i++ } else break
      }
      let t = '<table><thead><tr>'
      splitRow(line).forEach((c) => { t += '<th>' + inline(c) + '</th>' })
      t += '</tr></thead><tbody>'
      body.forEach((b) => {
        t += '<tr>'
        splitRow(b).forEach((c) => { t += '<td>' + inline(c) + '</td>' })
        t += '</tr>'
      })
      out.push(t + '</tbody></table>')
      continue
    }
    if (/^[-*+]\s+/.test(line)) { flushPara(); if (listType !== 'ul') { flushList(); listType = 'ul' } listBuf.push('<li>' + inline(line.replace(/^[-*+]\s+/, '')) + '</li>'); continue }
    if (/^\d+[.)]\s+/.test(line)) { flushPara(); if (listType !== 'ol') { flushList(); listType = 'ol' } listBuf.push('<li>' + inline(line.replace(/^\d+[.)]\s+/, '')) + '</li>'); continue }
    if (line === '') { flushPara(); flushList(); continue }
    para.push(raw)
  }
  flushPara()
  flushList()

  let html = out.join('\n')
  html = html.replace(/\u0000CODE(\d+)\u0000/g, (m, idx) => {
    const code = codeBlocks[Number(idx)]
    return '<pre><code>' + esc(code) + '</code></pre>'
  })
  return html
}

function prettyJson(s) {
  if (!s) return ''
  try {
    return JSON.stringify(JSON.parse(s), null, 2)
  } catch {
    return String(s)
  }
}

/* ============================ 状态指示 ============================ */

function setStatus(text, ok) {
  $('txt-status').textContent = text
  const dot = $('dot-agent')
  const idle = ['空闲', '未连接', '未知']
  dot.className = 'dot ' + (ok === true ? 'ok' : ok === false ? 'fail'
    : (idle.includes(text) ? '' : 'running'))
}

function setServer(ok, label) {
  $('dot-server').className = 'dot ' + (ok ? 'ok' : ok === null ? '' : 'fail')
  $('txt-server').textContent = label
}

function setPage(ok, label) {
  $('dot-page').className = 'dot ' + (ok ? 'ok' : ok === null ? '' : 'fail')
  $('txt-page').textContent = label
}

/* ============================ 聊天渲染 ============================ */

let curAgent = null

function scrollChat() {
  const el = $('chat')
  el.scrollTop = el.scrollHeight
}

function addSysMsg(text) {
  const div = document.createElement('div')
  div.className = 'msg sys'
  div.textContent = text
  $('chat').appendChild(div)
  scrollChat()
}

function addUserMsg(text) {
  const div = document.createElement('div')
  div.className = 'msg user'
  div.textContent = text
  $('chat').appendChild(div)
  scrollChat()
}

function newAgentMsg() {
  const wrap = document.createElement('div')
  wrap.className = 'msg agent'
  const body = document.createElement('div')
  body.className = 'body'
  const tools = document.createElement('div')
  tools.className = 'tools'
  wrap.appendChild(body)
  wrap.appendChild(tools)
  $('chat').appendChild(wrap)
  curAgent = { el: wrap, body, tools, queue: [] }
  showTyping()
  scrollChat()
  return curAgent
}

function ensureAgentMsg() {
  return curAgent || newAgentMsg()
}

function showTyping() {
  if (curAgent) {
    curAgent.body.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>'
  }
}

function setAgentBody(text) {
  const a = ensureAgentMsg()
  a.body.innerHTML = text ? md(text) : ''
  scrollChat()
}

function appendToolCall(name, args) {
  const a = ensureAgentMsg()
  const card = document.createElement('div')
  card.className = 'tool open'
  card.innerHTML =
    '<div class="tool-head">' +
      '<span class="tool-status pending"></span>' +
      '<span class="tool-name"></span>' +
      '<span class="tool-summary"></span>' +
      '<span class="tool-toggle">▾</span>' +
    '</div>' +
    '<div class="tool-detail">' +
      '<div class="k">参数</div><pre class="v-args"></pre>' +
      '<div class="k">结果</div><pre class="v-result">等待中…</pre>' +
    '</div>'
  card.querySelector('.tool-name').textContent = name || 'tool'
  card.querySelector('.v-args').textContent = prettyJson(args) || '（无参数）'
  card.querySelector('.tool-head').addEventListener('click', () => card.classList.toggle('open'))
  a.tools.appendChild(card)
  a.queue.push({ card, name })
  scrollChat()
  return card
}

function resolveToolResult(name, ok, summary) {
  const a = curAgent
  if (!a || !a.queue.length) return
  const item = a.queue.shift()
  const card = item.card
  const st = card.querySelector('.tool-status')
  st.classList.remove('pending')
  if (ok === false) st.classList.add('fail')
  else if (ok === true) st.classList.add('ok')
  if (summary) card.querySelector('.tool-summary').textContent = summary
  const res = card.querySelector('.v-result')
  res.textContent = summary || (ok === true ? '成功' : ok === false ? '失败' : '完成')
  res.classList.toggle('v-ok', ok === true)
  res.classList.toggle('v-fail', ok === false)
  scrollChat()
}

function finishAgentMsg(text, sent, ok) {
  const a = ensureAgentMsg()
  if (text) a.body.innerHTML = md(text)
  else a.body.textContent = ''
  if (ok === false) a.el.classList.add('fail')
  if (sent !== undefined && sent !== null) {
    const line = document.createElement('div')
    line.className = 'sent-line'
    line.textContent = '本轮已投递打招呼: ' + sent + ' 个'
    a.body.appendChild(line)
  }
  if (!a.tools.childNodes.length) a.tools.remove()
  curAgent = null
  scrollChat()
}

function clearChat() {
  $('chat').innerHTML = ''
  curAgent = null
}

/* ============================ 运行配置 ============================ */

const CFG_KEY = 'gja.runCfg'

function saveRunConfig() {
  const cfg = {
    unattended: $('cfg-unattended').checked,
    maxGreetings: Math.max(0, parseInt($('cfg-max-greetings').value, 10) || 0),
  }
  try { localStorage.setItem(CFG_KEY, JSON.stringify(cfg)) } catch (e) { /* 忽略 */ }
}

function loadRunConfig() {
  try {
    const cfg = JSON.parse(localStorage.getItem(CFG_KEY) || 'null')
    if (cfg && typeof cfg === 'object') {
      $('cfg-unattended').checked = !!cfg.unattended
      $('cfg-max-greetings').value = Math.max(0, parseInt(cfg.maxGreetings, 10) || 0)
      return
    }
  } catch (e) { /* 无本地记录 */ }
  fetch('http://127.0.0.1:8791/agent/run-config')
    .then((r) => r.json())
    .then((d) => {
      $('cfg-unattended').checked = String(d.agent_mode || '').toLowerCase() !== 'confirm'
      $('cfg-max-greetings').value = Math.max(0, parseInt(d.max_greetings_per_run, 10) || 0)
    })
    .catch(() => { /* Server 未连接 */ })
}

function getRunConfig() {
  return {
    mode: $('cfg-unattended').checked ? 'unattended' : 'confirm',
    max_greetings: Math.max(0, parseInt($('cfg-max-greetings').value, 10) || 0),
  }
}

/* ============================ 打招呼确认（HITL） ============================ */

function showGreetingConfirm(greeting) {
  $('greeting-text').value = greeting || ''
  $('greeting-result').classList.add('hidden')
  $('greeting-result').textContent = ''
  $('greeting-overlay').classList.remove('hidden')
}

function hideGreetingConfirm() {
  $('greeting-overlay').classList.add('hidden')
}

function genIdem() {
  return 'sp_' + Math.random().toString(36).slice(2, 10)
}

function resumeWithDecision(decision) {
  if (!connected || !ws) {
    addSysMsg('Server 未连接，无法确认。')
    return
  }
  const el = $('greeting-result')
  el.classList.remove('hidden')
  el.style.color = 'inherit'
  el.textContent = decision.type === 'reject' ? '已拒绝，Agent 继续下一个岗位…' : '已确认，正在发送…'
  const idem = genIdem()
  currentIdem = idem
  running = true
  $('btn-stop').disabled = false
  hideGreetingConfirm()
  ws.send(JSON.stringify({ from: 'panel', type: 'agent_resume', idem, payload: { decision } }))
}

function approveGreeting() { resumeWithDecision({ type: 'approve' }) }

function editSendGreeting() {
  const text = $('greeting-text').value.trim()
  if (!text) { addSysMsg('话术为空，请先修改再发送。'); return }
  resumeWithDecision({ type: 'edit', text })
}

function rejectGreeting() {
  resumeWithDecision({ type: 'reject', message: '用户拒绝发送该话术，请跳过本岗位、继续下一个或结束。' })
}

/* ============================ WS 连接 ============================ */

let wsReconnectTimer = null
let wsBackoff = 1000

function scheduleReconnect() {
  if (wsReconnectTimer !== null) return
  setServer(null, '重连中…')
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null
    connect()
  }, wsBackoff)
  wsBackoff = Math.min(wsBackoff * 2, 10000)
}

function connect() {
  if (wsReconnectTimer !== null) {
    clearTimeout(wsReconnectTimer)
    wsReconnectTimer = null
  }
  try {
    ws = new WebSocket(WS_URL)
  } catch (e) {
    setServer(false, '不可用')
    scheduleReconnect()
    return
  }

  ws.addEventListener('open', () => {
    connected = true
    wsBackoff = 1000
    setServer(true, '已连接')
    ws.send(JSON.stringify({ from: 'panel', type: 'register', client_id: getClientId() }))
  })

  ws.addEventListener('message', (evt) => {
    let msg
    try { msg = JSON.parse(evt.data) } catch { return }
    handleMessage(msg)
  })

  ws.addEventListener('close', () => {
    connected = false
    setServer(null, '已断开')
    if (running) {
      running = false
      clearWatchdog()
      $('btn-stop').disabled = true
      addSysMsg('连接已断开，运行中断。将自动重连…')
    }
    scheduleReconnect()
  })

  ws.addEventListener('error', () => setServer(false, '不可用'))
}

function handleMessage(msg) {
  if (!msg || typeof msg !== 'object') return
  switch (msg.type) {
    case 'registered':
      setServer(true, '已连接')
      break
    case 'page_connected':
      setPage(Boolean(msg.page_id), msg.page_id ? '已连接' : '未连接')
      break
    case 'agent_event': {
      if (msg.idem !== currentIdem && currentIdem) break
      const e = msg.payload || {}
      if (e.kind === 'assistant') setAgentBody(e.text || '')
      else if (e.kind === 'tool_call') appendToolCall(e.tool, e.args)
      else if (e.kind === 'tool_result') resolveToolResult(e.tool, e.ok, e.summary)
      else if (e.kind === 'interrupt') {
        addSysMsg('⏸ 已生成打招呼话术，等待你确认…')
        setStatus('等待你确认话术')
        showGreetingConfirm(e.greeting || '')
      }
      break
    }
    case 'agent_status': {
      if (msg.idem !== currentIdem && currentIdem) break
      const s = (msg.payload || {}).status
      if (s === 'operating') setStatus('正在操作浏览器: ' + (msg.payload.tool || '…'))
      else if (s === 'running') setStatus('运行中…')
      else if (s === 'awaiting_confirmation') setStatus('等待你确认话术')
      else if (s === 'stopping') setStatus('正在停止…')
      else if (s === 'stopped') setStatus('已停止', false)
      else if (s === 'done') setStatus('已完成', true)
      else if (s === 'failed') setStatus('失败', false)
      else if (s === 'idle') setStatus('空闲')
      break
    }
    case 'agent_go_result': {
      // 新消息会顶掉旧任务；旧任务的收尾回执(idem 不匹配)需忽略，以免误清正在运行的新一轮。
      if (currentIdem && msg.idem !== currentIdem) break
      running = false
      currentIdem = null
      $('btn-stop').disabled = true
      const p = msg.payload || {}
      if (!p.ok) {
        setStatus(p.stopped ? '已停止' : '失败', false)
        finishAgentMsg(p.stopped ? ('已停止: ' + (p.error || '')) : ('执行失败: ' + (p.error || '未知错误')), null, false)
        break
      }
      setStatus('已完成', true)
      finishAgentMsg(String(p.output || '(无输出)'), p.sent, true)
      break
    }
    case 'reset_agent_result': {
      setStatus('空闲')
      running = false
      currentIdem = null
      $('btn-stop').disabled = true
      hideGreetingConfirm()
      clearChat()
      addSysMsg('会话已清空。此后每次「发送」均为全新一轮对话。')
      break
    }
    case 'resume_build_dir_result': {
      const p = msg.payload || {}
      const el = $('upload-result')
      el.classList.remove('hidden')
      const fmt = (arr) => (Array.isArray(arr) && arr.length ? arr.join('、') : '（无）')
      if (!p.ok) {
        el.textContent = '扫描失败: ' + (p.error || '未知错误')
        break
      }
      el.textContent =
        '扫描目录: ' + p.directory + '\n成功: ' + fmt(p.scanned) +
        '\n跳过: ' + fmt(p.skipped) + '\n失败: ' +
        (p.failed && p.failed.length ? p.failed.map((f) => f.file + '(' + f.reason + ')').join('、') : '（无）')
      loadResumeStatus()
      break
    }
    default:
      break
  }
}

/* ============================ 发送 / 控制 ============================ */

function start() {
  const goal = $('goal-input').value.trim()
  if (!goal) { addSysMsg('请输入目标。'); return }
  if (!connected) { addSysMsg('Server 未连接，无法执行。'); return }
  if (running) {
    if (!$('greeting-overlay').classList.contains('hidden')) {
      addSysMsg('当前有待确认的打招呼话术，请先在弹层处理（确认/修改/拒绝）后再发送。')
      return
    }
    // 连续对话：直接发起新消息，后端会自动打断上一任务并改为运行本轮。
    addSysMsg('⏭ 已顶替上一任务，开始处理新消息…')
  }
  sendGoal(goal)
}

function sendGoal(goal) {
  saveRunConfig()
  const cfg = getRunConfig()
  const idem = genIdem()
  running = true
  currentIdem = idem
  $('btn-stop').disabled = false
  addUserMsg(goal)
  newAgentMsg()
  addSysMsg((cfg.mode === 'unattended' ? '无人值守' : '需确认')
    + (cfg.max_greetings > 0 ? ' | 投递上限 ' + cfg.max_greetings + ' 个' : ''))
  setStatus('运行中…')
  ws.send(JSON.stringify({ from: 'panel', type: 'agent_go', idem, payload: { goal, ...cfg } }))
  armWatchdog()
}

function stop() {
  if (!running || !currentIdem || !ws) return
  setStatus('正在停止…')
  ws.send(JSON.stringify({ from: 'panel', type: 'stop_agent', idem: currentIdem }))
}

function resetSession() {
  if (!connected) { addSysMsg('Server 未连接，无法清空。'); return }
  const idem = genIdem()
  ws.send(JSON.stringify({ from: 'panel', type: 'reset_agent', idem }))
  setStatus('清空中…')
  addSysMsg('⏹ 请求清空会话…')
}

/* ============================ 看门狗 ============================ */

let watchdogTimer = null

function armWatchdog() {
  clearWatchdog()
  watchdogTimer = setTimeout(() => {
    if (running) addSysMsg('⏱ 任务已运行 3 分钟仍无结果，可能卡住。可点「停止」结束，或查看后端日志。')
  }, 180000)
}

function clearWatchdog() {
  if (watchdogTimer !== null) { clearTimeout(watchdogTimer); watchdogTimer = null }
}

/* ============================ 简历 ============================ */

async function uploadResume() {
  const input = $('resume-file')
  const el = $('upload-result')
  const file = input.files && input.files[0]
  el.classList.remove('hidden')
  if (!file) { el.textContent = '请先选择简历文件（PDF/DOCX）。'; return }
  el.textContent = '上传中…（后端解析，同名文件替换更新）'
  const fd = new FormData()
  fd.append('user_key', 'default')
  fd.append('file', file)
  try {
    const res = await fetch('http://127.0.0.1:8791/resume/upload', { method: 'POST', body: fd })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      const detail = data.detail
      el.textContent = '上传失败: ' + (typeof detail === 'string' ? detail : 'HTTP ' + res.status)
      return
    }
    const v = data.validation || {}
    const warn = v.errors && v.errors.length ? '（校验告警: ' + v.errors.join('、') + '）' : ''
    el.textContent = '解析完成 ✓ 简历 id: ' + data.resume_id + '，来源: ' + data.source + warn
    input.value = ''
    loadResumeStatus()
  } catch (e) {
    el.textContent = '上传失败: ' + String(e && e.message ? e.message : e)
  }
}

async function loadResumeStatus() {
  const txt = $('txt-resume')
  const detail = $('resume-detail')
  try {
    const res = await fetch('http://127.0.0.1:8791/resume/list?user_key=default')
    if (!res.ok) throw new Error('HTTP ' + res.status)
    const data = await res.json()
    const items = Array.isArray(data.items) ? data.items : []
    const attach = items.find((it) => it.source === 'attachment')
    if (!attach) {
      txt.textContent = '未解析'
      detail.textContent = '尚未在 server/jianli 中发现已解析简历。'
      return
    }
    const p = attach.profile_json || {}
    const intent = p.intent || {}
    txt.textContent = '已解析 ✓  ' + (p.name || '')
    detail.textContent =
      'id: ' + attach.id + ' | 文件: ' + attach.file_name +
      '\n意向: ' + (intent.position || '—') +
      ' | 年限: ' + (p.years || '—') + '年 | 学历: ' + (p.education || '—')
  } catch (e) {
    txt.textContent = '读取失败'
    detail.textContent = String(e)
  }
}

/* ============================ 输入区自适应 ============================ */

function autoResize() {
  const ta = $('goal-input')
  ta.style.height = 'auto'
  ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'
}

/* ============================ 初始化 ============================ */

document.addEventListener('DOMContentLoaded', () => {
  connect()
  setServer(null, '连接中…')
  setPage(null, '未知')
  loadResumeStatus()
  setInterval(loadResumeStatus, 30000)
  loadRunConfig()

  $('btn-settings').addEventListener('click', () => {
    $('settings-drawer').classList.toggle('hidden')
  })
  $('cfg-unattended').addEventListener('change', saveRunConfig)
  $('cfg-max-greetings').addEventListener('change', saveRunConfig)
  $('btn-go').addEventListener('click', start)
  $('goal-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault()
      start()
    }
  })
  $('goal-input').addEventListener('input', autoResize)
  $('btn-stop').addEventListener('click', stop)
  $('btn-reset').addEventListener('click', resetSession)
  $('btn-rescan').addEventListener('click', () => {
    if (!connected) { addSysMsg('Server 未连接，无法扫描。'); return }
    const idem = genIdem()
    ws.send(JSON.stringify({ from: 'panel', type: 'resume_build_dir', idem }))
  })
  $('btn-upload').addEventListener('click', uploadResume)
  $('btn-greet-approve').addEventListener('click', approveGreeting)
  $('btn-greet-edit').addEventListener('click', editSendGreeting)
  $('btn-greet-reject').addEventListener('click', rejectGreeting)

  setInterval(() => {
    if (connected) ws.send(JSON.stringify({ from: 'panel', type: 'keepalive' }))
  }, 20000)
})