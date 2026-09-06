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
}

function setServer(ok, label) {
  $('dot-server').className = 'dot ' + (ok ? 'ok' : ok === null ? 'idle' : 'fail')
  $('txt-server').textContent = label
}

function setPage(ok, label) {
  $('dot-page').className = 'dot ' + (ok ? 'ok' : ok === null ? 'idle' : 'fail')
  $('txt-page').textContent = label
}

function appendLog(text, cls) {
  const div = document.createElement('div')
  div.className = cls || ''
  div.textContent = text
  $('output').appendChild(div)
  $('output').scrollTop = $('output').scrollHeight
}

function clearOutput() {
  $('output').innerHTML = ''
}

function genIdem() {
  return 'sp_' + Math.random().toString(36).slice(2, 10)
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
    appendLog('Server 未连接，无法确认。', 'err')
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
    appendLog('话术为空，请先修改再发送。', 'err')
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
      appendLog('连接已断开，运行中断。将自动重连…', 'err')
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
        appendLog('💭 ' + (e.text || ''), 'think')
      } else if (e.kind === 'tool_call') {
        appendLog('🔧 第' + (e.round || '?') + '轮 · 调用 ' + (e.tool || '')
          + (e.args ? '\n    参数: ' + e.args : ''), 'tool')
      } else if (e.kind === 'tool_result') {
        const tag = e.ok === false ? '✖ 失败' : e.ok === true ? '✔ 完成' : '→ 返回'
        appendLog('    ' + tag + ' ' + (e.tool || '') + (e.summary ? '  [' + e.summary + ']' : ''),
          e.ok === false ? 'err' : 'toolres')
      } else if (e.kind === 'interrupt') {
        // Agent 拟调用 send_greeting → 暂停；展示话术让用户批准/修改/拒绝
        appendLog('⏸ 已生成打招呼话术，等待你确认…', 'final')
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
        appendLog((p.stopped ? '已停止: ' : '执行失败: ') + (p.error || '未知错误'), 'err')
        break
      }
      setStatus('已完成', true)
      // 运行过程已通过 agent_event 实时流式显示，此处不再重复倾倒 steps
      appendLog('—— 最终结果 ——', 'final')
      appendLog(String(p.output || '(无输出)'))
      break
    }
    case 'reset_agent_result': {
      setStatus('空闲')
      running = false
      currentIdem = null
      $('btn-go').disabled = false
      $('btn-stop').disabled = true
      hideGreetingConfirm()
      clearOutput()
      appendLog('会话已清空。此后每次「执行」均为全新一轮对话。', 'ok')
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
    appendLog('请输入目标。', 'err')
    return
  }
  if (!connected) {
    appendLog('Server 未连接，无法执行。', 'err')
    return
  }
  if (running) {
    appendLog('已有任务在运行，请先停止或等待完成。', 'err')
    return
  }
  const idem = genIdem()
  running = true
  currentIdem = idem
  $('btn-go').disabled = true
  $('btn-stop').disabled = false
  clearOutput()
  appendLog('▶ 执行目标：' + goal)
  setStatus('运行中…（思考/规划）')
  ws.send(JSON.stringify({ from: 'panel', type: 'agent_go', idem, payload: { goal } }))
}

function stop() {
  if (!running || !currentIdem || !ws) return
  setStatus('正在停止…')
  ws.send(JSON.stringify({ from: 'panel', type: 'stop_agent', idem: currentIdem }))
}

function resetSession() {
  if (!connected) {
    appendLog('Server 未连接，无法清空。', 'err')
    return
  }
  const idem = genIdem()
  ws.send(JSON.stringify({ from: 'panel', type: 'reset_agent', idem }))
  setStatus('清空中…')
  appendLog('⏹ 请求清空会话…')
}

function rescan() {
  if (!connected) {
    appendLog('Server 未连接，无法扫描。', 'err')
    return
  }
  const idem = genIdem()
  ws.send(JSON.stringify({ from: 'panel', type: 'resume_build_dir', idem }))
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
  $('btn-go').addEventListener('click', start)
  $('btn-stop').addEventListener('click', stop)
  $('btn-reset').addEventListener('click', resetSession)
  $('btn-rescan').addEventListener('click', rescan)
  // 原生 HITL：用户对打招呼话术的三种确认（批准/修改后发/拒绝）
  $('btn-greet-approve').addEventListener('click', approveGreeting)
  $('btn-greet-edit').addEventListener('click', editSendGreeting)
  $('btn-greet-reject').addEventListener('click', rejectGreeting)
  // 心跳：保持 WS 与 server 侧活跃探测
  setInterval(() => {
    if (connected) ws.send(JSON.stringify({ from: 'panel', type: 'keepalive' }))
  }, 20000)
})