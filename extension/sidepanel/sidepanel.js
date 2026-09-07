/**
 * Side Panel 逻辑：直连 Agent Server WS（/ws），承载最基础的 Agent 控制。
 * - 连接状态：Server / 当前 Get-Job 页
 * - 目标执行：agent_go → 后台跑浏览器操作 Agent
 * - 简历：手动触发 resume_build_dir 重扫
 */
const $ = (id) => document.getElementById(id)

const WS_URL = 'ws://127.0.0.1:8791/ws'

let ws = null
let connected = false
let running = false
let currentIdem = null // 当前运行中的 agent_go idem，用于「停止」

function setStatus(text, ok) {
  const el = $('txt-status')
  el.textContent = text
  el.style.color = ok === true ? 'var(--ok)' : ok === false ? 'var(--fail)' : 'inherit'
  // 聊天框顶栏状态点：空闲=灰 / 运行中=蓝 / 成功=绿 / 失败=红
  const dot = $('dot-agent')
  if (dot) {
    const idle = ['空闲', '未连接', '未知']
    dot.className = 'dot ' + (ok === true ? 'ok' : ok === false ? 'fail'
      : (idle.includes(text) ? 'idle' : 'running'))
  }
}

function setServer(ok, label) {
  $('dot-server').className = 'dot ' + (ok ? 'ok' : ok === null ? 'idle' : 'fail')
  $('txt-server').textContent = label
}

function setPage(ok, label) {
  $('dot-page').className = 'dot ' + (ok ? 'ok' : ok === null ? 'idle' : 'fail')
  $('txt-page').textContent = label
}

// ---- 聊天式渲染：用户/Agent 气泡，Agent 消息含正文 + 工具调用标签 + 可折叠思考过程 ----
let curAgent = null // 当前 Agent 消息容器 {el, body, tools, procBody, queue}

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
  body.textContent = '正在处理…'
  const tools = document.createElement('div')
  tools.className = 'tools'
  wrap.appendChild(body)
  wrap.appendChild(tools)
  $('chat').appendChild(wrap)
  curAgent = { el: wrap, body, tools, queue: [] }
  scrollChat()
  return curAgent
}

function setAgentBody(text) {
  if (!curAgent) newAgentMsg()
  curAgent.body.textContent = text
  scrollChat()
}

function appendToolCall(name, args, round) {
  if (!curAgent) newAgentMsg()
  const tag = document.createElement('span')
  tag.className = 'tool-tag pending'
  tag.textContent = '🔧 ' + (name || 'tool')
  if (args) tag.title = args
  curAgent.tools.appendChild(tag)
  curAgent.queue.push({ tag })
  scrollChat()
}

function resolveToolResult(name, ok, summary) {
  if (!curAgent || !curAgent.queue.length) return
  const item = curAgent.queue.shift() // 工具调用与结果按序成对出现，顺序匹配
  const tag = item.tag
  tag.classList.remove('pending')
  if (ok === false) tag.classList.add('fail')
  else if (ok === true) tag.classList.add('ok')
  if (summary) tag.textContent += ' [' + summary + ']'
  scrollChat()
}

function finishAgentMsg(text, sent, ok) {
  if (!curAgent) newAgentMsg()
  const a = curAgent
  if (text) a.body.textContent = text
  if (ok === false) a.el.classList.add('fail')
  if (sent !== undefined && sent !== null) {
    const line = document.createElement('div')
    line.className = 'sent-line'
    line.textContent = '本轮已投递打招呼: ' + sent + ' 个'
    a.body.appendChild(line)
  }
  if (!a.tools.childNodes.length) a.tools.remove() // 无工具调用则不显示空标签行
  curAgent = null
  scrollChat()
}

function clearChat() {
  $('chat').innerHTML = ''
  curAgent = null
}

function genIdem() {
  return 'sp_' + Math.random().toString(36).slice(2, 10)
}

// ---- 运行配置：无人值守开关 + 投递上限（localStorage 持久，随 agent_go 透传给 Server）----
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
  } catch (e) { /* 无本地记录，走 Server 默认 */ }
  // 首次使用：以 Server 全局配置为默认
  fetch('http://127.0.0.1:8791/agent/run-config')
    .then((r) => r.json())
    .then((d) => {
      $('cfg-unattended').checked = String(d.agent_mode || '').toLowerCase() !== 'confirm'
      $('cfg-max-greetings').value = Math.max(0, parseInt(d.max_greetings_per_run, 10) || 0)
    })
    .catch(() => { /* Server 未连接时保持 0/默认，执行前会有连接提示 */ })
}

function getRunConfig() {
  return {
    mode: $('cfg-unattended').checked ? 'unattended' : 'confirm',
    max_greetings: Math.max(0, parseInt($('cfg-max-greetings').value, 10) || 0),
  }
}

// ---- 原生 HITL：Agent 调用 send_greeting 前会暂停，server 推 kind=interrupt 事件待确认 ----
function showGreetingConfirm(greeting) {
  const section = $('greeting-section')
  $('greeting-text').value = greeting
  $('greeting-result').classList.add('hidden')
  $('greeting-result').textContent = ''
  section.classList.remove('hidden')
  section.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}

function hideGreetingConfirm() {
  $('greeting-section').classList.add('hidden')
}

// 以确认结果恢复被暂停的会话（approve/edit/reject），server 用 Command(resume) 续跑同一 thread
function resumeWithDecision(decision) {
  if (!connected || !ws) {
    addSysMsg('Server 未连接，无法确认。')
    return
  }
  const el = $('greeting-result')
  el.classList.remove('hidden')
  el.style.color = 'inherit'
  el.textContent = decision.type === 'reject' ? '已拒绝，Agent 继续下一个岗位…' : '已确认，正在发送…'
  // 续跑使用新的 idem，并接管为当前任务（后续事件按此 idem 流式回传）
  const idem = genIdem()
  currentIdem = idem
  running = true
  $('btn-go').disabled = true
  $('btn-stop').disabled = false
  hideGreetingConfirm()
  ws.send(JSON.stringify({ from: 'panel', type: 'agent_resume', idem, payload: { decision } }))
}

function approveGreeting() {
  resumeWithDecision({ type: 'approve' })
}

function editSendGreeting() {
  const text = $('greeting-text').value.trim()
  if (!text) {
    addSysMsg('话术为空，请先修改再发送。')
    return
  }
  resumeWithDecision({ type: 'edit', text })
}

function rejectGreeting() {
  resumeWithDecision({ type: 'reject', message: '用户拒绝发送该话术，请跳过本岗位、继续寻找下一个岗位或结束。' })
}

// 面板→Server 的 WS：带指数退避的自动重连（Server 重启/网络抖动/MV3 挂起后能自动恢复）
let wsReconnectTimer = null
let wsBackoff = 1000

function scheduleReconnect() {
  if (wsReconnectTimer !== null) return
  setServer(null, '重连中…')
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null
    connect()
  }, wsBackoff)
  wsBackoff = Math.min(wsBackoff * 2, 10000) // 1s → 2s → 4s → 8s → 封顶 10s
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
    wsBackoff = 1000 // 连上即重置退避
    setServer(true, '已连接')
    ws.send(JSON.stringify({ from: 'panel', type: 'register' }))
    drainQueue() // 断线期间排队的消息，重连后自动处理
    // 注册后 Server 会回放当前页面连接态，无需刷新页面即可显示「已连接」
  })

  ws.addEventListener('message', (evt) => {
    let msg
    try {
      msg = JSON.parse(evt.data)
    } catch {
      return
    }
    handleMessage(msg)
  })

  ws.addEventListener('close', () => {
    connected = false
    setServer(null, '已断开')
    if (running) {
      running = false
      $('btn-go').disabled = false
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
      if (msg.idem !== currentIdem && currentIdem) break // 忽略其他任务的事件
      const e = msg.payload || {}
      if (e.kind === 'assistant') {
        setAgentBody(e.text || '') // 模型输出（含正常回复）直接作为 Agent 正文显示
      } else if (e.kind === 'tool_call') {
        appendToolCall(e.tool, e.args, e.round)
      } else if (e.kind === 'tool_result') {
        resolveToolResult(e.tool, e.ok, e.summary)
      } else if (e.kind === 'interrupt') {
        // Agent 拟调用 send_greeting → 暂停；展示话术让用户批准/修改/拒绝
        addSysMsg('⏸ 已生成打招呼话术，等待你确认…')
        setStatus('等待你确认话术')
        showGreetingConfirm(e.greeting || '')
      }
      break
    }
    case 'agent_status': {
      if (msg.idem !== currentIdem && currentIdem) break // 忽略其他任务的状态
      const s = (msg.payload || {}).status
      if (s === 'operating') setStatus('正在操作浏览器: ' + (msg.payload.tool || '…'))
      else if (s === 'running') setStatus('运行中…（思考/规划）')
      else if (s === 'awaiting_confirmation') setStatus('等待你确认话术')
      else if (s === 'stopping') setStatus('正在停止…')
      else if (s === 'stopped') setStatus('已停止', false)
      else if (s === 'done') setStatus('已完成', true)
      else if (s === 'failed') setStatus('失败', false)
      else if (s === 'idle') setStatus('空闲')
      break
    }
    case 'agent_go_result': {
      running = false
      currentIdem = null
      $('btn-go').disabled = false
      $('btn-stop').disabled = true
      const p = msg.payload || {}
      if (!p.ok) {
        setStatus(p.stopped ? '已停止' : '失败', false)
        finishAgentMsg(p.stopped ? ('已停止: ' + (p.error || '')) : ('执行失败: ' + (p.error || '未知错误')),
          null, false)
        drainQueue()
        break
      }
      setStatus('已完成', true)
      // 一轮完整对话：正文 = 最终回复，工具调用标签已流式展示，sent 汇总投递数
      finishAgentMsg(String(p.output || '(无输出)'), p.sent, true)
      drainQueue()
      break
    }
    case 'reset_agent_result': {
      setStatus('空闲')
      running = false
      currentIdem = null
      $('btn-go').disabled = false
      $('btn-stop').disabled = true
      hideGreetingConfirm()
      clearChat()
      addSysMsg('会话已清空。此后每次「执行」均为全新一轮对话。')
      break
    }
    case 'resume_build_dir_result': {
      const p = msg.payload || {}
      const el = $('resume-result')
      el.classList.remove('hidden')
      if (!p.ok) {
        el.textContent = '扫描失败: ' + (p.error || '未知错误')
        break
      }
      // 注意：数组为空时显示（无），避免优先级/空串造成"卡住"假象
      const fmt = (arr) => (Array.isArray(arr) && arr.length ? arr.join('、') : '（无）')
      el.textContent =
        '扫描目录: ' + p.directory +
        '\n成功: ' + fmt(p.scanned) +
        '\n跳过: ' + fmt(p.skipped) +
        '\n失败: ' + (p.failed && p.failed.length ? p.failed.map((f) => f.file + '(' + f.reason + ')').join('、') : '（无）')
      break
    }
    default:
      break
  }
}

function start() {
  const goal = $('goal-input').value.trim()
  if (!goal) {
    addSysMsg('请输入目标。')
    return
  }
  if (!connected) {
    addSysMsg('Server 未连接，无法执行。')
    return
  }
  if (running) {
    // 运行中不再拦截：新消息排队，当前任务结束后自动发送（支持连续追问）
    pendingQueue.push(goal)
    $('goal-input').value = ''
    addSysMsg('当前任务运行中，已加入队列（' + pendingQueue.length + ' 条待发送）。')
    return
  }
  sendGoal(goal)
}

function sendGoal(goal) {
  saveRunConfig() // 每次执行前落盘当前开关/上限
  const cfg = getRunConfig()
  const idem = genIdem()
  running = true
  currentIdem = idem
  $('btn-go').disabled = true
  $('btn-stop').disabled = false
  // 聊天式展示：用户气泡 + Agent 气泡占位 + 模式提示
  addUserMsg(goal)
  newAgentMsg()
  addSysMsg('模式: ' + (cfg.mode === 'unattended' ? '无人值守' : '需确认')
    + (cfg.max_greetings > 0 ? ' | 投递上限: ' + cfg.max_greetings + ' 个' : ' | 投递不限'))
  setStatus('运行中…（思考/规划）')
  ws.send(JSON.stringify({ from: 'panel', type: 'agent_go', idem, payload: { goal, ...cfg } }))
}

// 任务结束后：自动发送队列中的下一条消息（连接断开时保留队列，重连后继续）
function drainQueue() {
  if (!connected || running || !pendingQueue.length) return
  const next = pendingQueue.shift()
  addSysMsg('开始处理队列中的下一条消息…')
  sendGoal(next)
}

function stop() {
  if (!running || !currentIdem || !ws) return
  setStatus('正在停止…')
  ws.send(JSON.stringify({ from: 'panel', type: 'stop_agent', idem: currentIdem }))
}

function resetSession() {
  if (!connected) {
    addSysMsg('Server 未连接，无法清空。')
    return
  }
  pendingQueue = [] // 清空会话同时丢弃排队消息
  const idem = genIdem()
  ws.send(JSON.stringify({ from: 'panel', type: 'reset_agent', idem }))
  setStatus('清空中…')
  addSysMsg('⏹ 请求清空会话…')
}

function rescan() {
  if (!connected) {
    addSysMsg('Server 未连接，无法扫描。')
    return
  }
  const idem = genIdem()
  ws.send(JSON.stringify({ from: 'panel', type: 'resume_build_dir', idem }))
}

// 简历上传：直连 Server REST（扩展已授予 127.0.0.1:8791 host 权限），multipart 上传后由后端解析入库
async function uploadResume() {
  const input = $('resume-file')
  const el = $('upload-result')
  const file = input.files && input.files[0]
  el.classList.remove('hidden')
  if (!file) {
    el.textContent = '请先选择简历文件（PDF/DOCX）。'
    return
  }
  el.textContent = '上传中…（上传后后端解析，同名文件将替换更新）'
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
    input.value = '' // 清空选择，便于再次选择同名文件
    loadResumeStatus()
  } catch (e) {
    el.textContent = '上传失败: ' + String(e && e.message ? e.message : e)
  }
}

// 简历状态：直连 Server REST（扩展已授予 127.0.0.1:8791 host 权限），实时显示解析情况
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
    txt.textContent = '已解析 ✓'
    detail.textContent =
      'id: ' + attach.id +
      ' | 文件: ' + attach.file_name +
      '\n姓名: ' + (p.name || '') +
      '\n意向: ' + (intent.position || '') +
      ' | 年限: ' + (p.years || '') + '年 | 学历: ' + (p.education || '')
  } catch (e) {
    txt.textContent = '读取失败'
    detail.textContent = String(e)
  }
}

document.addEventListener('DOMContentLoaded', () => {
  connect()
  setServer(null, '连接中…')
  setPage(null, '未知')
  loadResumeStatus()
  setInterval(loadResumeStatus, 30000)
  loadRunConfig()
  $('cfg-unattended').addEventListener('change', saveRunConfig)
  $('cfg-max-greetings').addEventListener('change', saveRunConfig)
  $('btn-go').addEventListener('click', start)
  $('goal-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault()
      start()
    }
  })
  $('btn-stop').addEventListener('click', stop)
  $('btn-reset').addEventListener('click', resetSession)
  $('btn-rescan').addEventListener('click', rescan)
  $('btn-upload').addEventListener('click', uploadResume)
  // 原生 HITL：用户对打招呼话术的三种确认（批准/修改后发/拒绝）
  $('btn-greet-approve').addEventListener('click', approveGreeting)
  $('btn-greet-edit').addEventListener('click', editSendGreeting)
  $('btn-greet-reject').addEventListener('click', rejectGreeting)
  // 心跳：保持 WS 与 server 侧活跃探测
  setInterval(() => {
    if (connected) ws.send(JSON.stringify({ from: 'panel', type: 'keepalive' }))
  }, 20000)
})