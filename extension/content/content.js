/**
 * Content Script（页内执行器）
 * - 唯一读写 Boss DOM 的执行器，复用页面登录态
 * - 作为 WS 客户端连接 Agent Server 127.0.0.1:8791/ws，接收/回报 DOM 指令
 * - WS 存活期 = 页面，规避 Service Worker 30s 休眠
 */
(() => {
  const SERVER_WS = 'ws://127.0.0.1:8791/ws'
  const PAGE_ID = 'zhipin'

  /** @type {WebSocket|null} */
  let ws = null
  let reconnectTimer = null
  let stopped = false

  // 存活看门狗：记录“最后一次收到 server 任意报文(含 pong)”的时刻。
  // 半开 socket 时 ws.readyState 仍为 OPEN、不触发 onclose，故靠超时主动断开重连来自愈。
  let lastServerSeen = 0
  let watchdogTimer = null
  const KEEPALIVE_MS = 15000   // 心跳发送间隔
  const LIVENESS_TIMEOUT_MS = 40000 // 超过此时长没收到 server 任何回复即判死
  const WATCHDOG_MS = 10000

  // ---- 岗位采集去重缓存（按 job 链接）----
  const seenJobs = new Set()

  // ---- 可配置选择器（已按真实 Boss DOM 校准：推荐页 rec-job-list / 搜索页 job-list-box 兼容）----
  let SELECTORS = {
    jobCard: 'li.job-card-box, .job-card-wrapper, .job-list-box .job-card-wrapper, li[ka*="custompage"]',
    jobName: 'a.job-name, .job-title .name, .name',
    companyName: '.boss-name, a.boss-info .boss-name, .company-name, .company-text',
    salary: '.job-salary, .salary',
    jobArea: '.company-location, .job-area',
    tags: 'ul.tag-list > li, .tag-list li, .tag-list span',
    experience: 'ul.tag-list > li:nth-child(1), .tag-list li:nth-of-type(1)',
    education: 'ul.tag-list > li:nth-child(2), .tag-list li:nth-of-type(2)',
    jobLink: 'a.job-name, a[href*="/job_detail/"]',
    filterBar: '.search-filter, .job-filter, .filter-options',
    nextPageBtn: '.ui-pager li.next a, .ui-icon-arrow-right, .btn-next-page, li.next a',
    resumeBox: '.user-resume, .resume-card, .resume-info',
    // ---- 过滤条件（M3 结构化读取）----
    filterTab: '.job-filter, .search-filter, .job-filter-btns ul, .filter-options',
    filterCity: '.region-filter, .city-filter, .job-filter .region, .job-filter p:nth-of-type(1)',
    filterExperience: '.experience-filter, .job-filter p:nth-of-type(2)',
    filterEducation: '.education-filter, .job-filter p:nth-of-type(3)',
    filterSalary: '.salary-filter, .job-filter p:nth-of-type(4)',
    // ---- 在线简历字段 ----
    resumeName: '.user-info .name, .resume-head .name, .base-info .name',
    resumeInfo: '.user-info, .resume-head, .base-info, .user-stat, .resume-baseInfo',
    resumeWork: '.work-experience, .workInfo-box, .experience-box',
    resumeEdu: '.education, .edu-expect-box, .education-box',
    resumeSkill: '.skill, .skill-box, .project-box',
    // ---- M7 SOP：打招呼输入框 / 发送按钮（按 Boss 常见 DOM 校准，可经 set_selectors 覆盖）----
    greetingInput: 'textarea[placeholder*="打招呼"], textarea.chat-input, .chat-input textarea, .chat-op textarea, textarea[name="msg"], [contenteditable="true"].chat-input',
    greetingSend: '.btn-send, .chat-op .btn-send, button[class*="btn-send"], .send-btn, button[class*="send"], .chat-input .btn-send, [class*="chat"] [class*="send"], .chat-input-send, .chat-send, .geek-send',
    // ---- M7 SOP：投递沟通（岗位卡/详情页）与「已沟通」标记 ----
    chatNowBtn: 'button.btn-start, a.btn-start, .btn-start, button[class*="btn-start"], a[ka*="im"], .job-btn .btn-start, .job-banner-box .btn-start',
    contactedBadge: '.contacted, .already-contact, .has-contact, [class*="contacted"], [class*="already"]',
  }

  // ---- DOM 操作（M3 采集）----
  const domHandlers = {
    /** 探活 / 读取当前页面基本信息 */
    ping: () => ({
      url: location.href,
      title: document.title,
      ready: document.readyState,
    }),

    /** 覆盖选择器配置 */
    set_selectors: (params) => {
      if (params && typeof params === 'object') {
        SELECTORS = { ...SELECTORS, ...params }
        return { ok: true, updated: Object.keys(params) }
      }
      return { ok: false, error: 'set_selectors 需要对象参数' }
    },

    /** T3.1 岗位列表解析 */
    job_list: () => collectJobs(),

    /** T3.3 过滤条件读取 */
    filter_read: () => readFilters(),

    /** T3.4 翻页 */
    next_page: () => clickNextPage(),

    /** T3.2 在线简历读取 */
    resume_read: () => readResume(),

    /** 校准：读出真实 DOM 结构，供回填 config.yaml → selectors */
    calibrate: () => calibrate(),

    /** M5 computer-use：可访问性快照（看页面 → 决策） */
    snapshot: (params) => snapshot(params),

    /** M5 computer-use：执行一步浏览器操作（决策 → 操作 → 再看） */
    act: (params) => act(params),

    /** M5：等待 URL/元素/文本出现或页面稳定后，再返回快照 */
    wait_for: async (params) => {
      const met = await waitFor(params || {})
      const snap = snapshot(params)
      snap.wait_met = met
      return snap
    },

    /** M7 SOP：发送打招呼话术（仅服务器调用，用户确认后） */
    send_greeting: (params) => sendGreeting(params),

    /** 高层复合动作：一步进入某岗位的聊天页（点【立即沟通】→必要时点【继续沟通】→轮询到聊天态） */
    start_chat: (params) => startChat(params),
  }

  // ---- 解析实现 ----
  function qsa(selector) {
    try {
      return Array.from(document.querySelectorAll(selector))
    } catch {
      return []
    }
  }
  function textOf(el, selectors) {
    if (!el) return ''
    for (const s of selectors) {
      const n = el.querySelector(s)
      if (n && n.textContent.trim()) return n.textContent.trim()
    }
    return ''
  }
  function firstOf(selectorList) {
    for (const s of selectorList) {
      const n = document.querySelector(s)
      if (n) return n
    }
    return null
  }

  function linkHref(el, selectors) {
    for (const s of selectors) {
      const a = el.querySelector(s)
      if (a && a.href && a.href.includes('job_detail')) return a.href
    }
    return ''
  }

  /** 卡片是否已沟通过：带「已沟通」标记，或投递按钮显示「继续沟通/已沟通」 */
  function cardContacted(card) {
    const badge = card.querySelector(SELECTORS.contactedBadge)
    if (badge && /已沟通|沟通过|继续沟通/.test(badge.textContent)) return true
    const btn = card.querySelector(SELECTORS.chatNowBtn)
    if (btn && /继续沟通|已沟通/.test(btn.textContent)) return true
    return false
  }

  /** 首个「可见」的匹配元素（多个逗号选择器一并生效）；取不到返回 null */
  function firstVisibleSel(selector) {
    try {
      return Array.from(document.querySelectorAll(selector)).find((el) => isVisible(el)) || null
    } catch {
      return null
    }
  }

  /** 当前页是否存在可见的聊天/打招呼输入框（用于识别 job_detail 等“当前页弹框”也是聊天态） */
  function hasChatInput() {
    return !!firstVisibleSel(SELECTORS.greetingInput)
  }

  /** 当前页面类型：聊天态(专面页 URL 或弹框输入框) / 职位详情 / 岗位列表 / 其他 */
  function pageKind() {
    const u = location.href
    if (u.includes('/web/geek/chat')) return 'chat'
    if (hasChatInput()) return 'chat' // job_detail 等页点【立即沟通】弹在当前页的对话框，也算聊天态
    if (u.includes('/job_detail/')) return 'detail'
    if (u.includes('/web/geek/jobs')) return 'list'
    if (document.querySelector(SELECTORS.jobCard)) return 'list'
    return 'other'
  }

  function collectJobs(dedupe) {
    let cards = qsa(SELECTORS.jobCard)
    // 若第一条命中失败，退化为逐条 jobLink 向上找卡片结构
    if (!cards.length) {
      const links = qsa('a[href*="/job_detail/"]')
      cards = links
        .map((a) =>
          a.closest('li.job-card-box, .job-card-wrapper, li[ka], .job-card-left, .job-list-box li') ||
          a.parentElement)
        .filter(Boolean)
    }

    const jobs = []
    for (const card of cards) {
      const name = textOf(card, [SELECTORS.jobName])
      const company = textOf(card, [SELECTORS.companyName])
      const salary = textOf(card, [SELECTORS.salary])
      const area = textOf(card, [SELECTORS.jobArea])
      // 标签必须限定在卡片内，否则多卡时每张卡都拿到全页标签
      const tags = Array.from(card.querySelectorAll(SELECTORS.tags))
        .map((t) => t.textContent.trim())
        .filter(Boolean)
      const link = linkHref(card, [SELECTORS.jobLink])
      // 经验/学历优先按位置选择器，退化为 tags 第 1/2 项
      const experience = textOf(card, [SELECTORS.experience]) || tags[0] || ''
      const education = textOf(card, [SELECTORS.education]) || tags[1] || ''

      if (!name && !link) continue

      if (link && dedupe !== false) {
        if (seenJobs.has(link)) continue
        seenJobs.add(link)
      }

      const job = { name, company, salary, area, experience, education, tags, link, contacted: cardContacted(card) }
      jobs.push(job)
    }
    return { total: jobs.length, jobs }
  }

  function readFilters() {
    const bar = firstOf([SELECTORS.filterTab])
    // 结构化读取各项过滤条件；取不到具体项时退化为整条文本
    const pick = (selList) => {
      const el = firstOf(selList)
      if (!el) return ''
      return el.innerText.replace(/\s+/g, ' ').replace(/\s*\|\s*/g, ' | ').trim()
    }
    const filters = {
      city: pick([SELECTORS.filterCity]),
      experience: pick([SELECTORS.filterExperience]),
      education: pick([SELECTORS.filterEducation]),
      salary: pick([SELECTORS.filterSalary]),
    }
    const filled = Object.values(filters).some(Boolean)
    if (!bar && !filled) return { filters: {}, ok: true }
    const raw = (bar ? bar.innerText.replace(/\s+/g, ' ').trim() : '') || String(Object.values(filters).filter(Boolean).join(' '))
    return { filters, raw, ok: true }
  }

  function clickNextPage() {
    const btn = firstOf([SELECTORS.nextPageBtn])
    if (!btn) return { ok: false, error: '未找到下一页按钮' }
    btn.click()
    return { ok: true }
  }

  function readResume() {
    const box = firstOf([SELECTORS.resumeBox])
    if (!box) return { ok: false, error: '未找到简历区域' }
    // 结构化抽取常用字段，取不到则退回整块文本
    const pick = (selList) => {
      const el = firstOf(selList.map((s) => box.querySelector(s)).filter(Boolean))
      if (!el) return ''
      return el.innerText.replace(/\s+/g, ' ').trim()
    }
    const resume = {
      name: pick([SELECTORS.resumeName]),
      base: pick([SELECTORS.resumeInfo]),
      work: pick([SELECTORS.resumeWork]),
      education: pick([SELECTORS.resumeEdu]),
      skill: pick([SELECTORS.resumeSkill]),
    }
    const filled = Object.values(resume).some(Boolean)
    if (!filled) {
      const raw = box.innerText.replace(/\s+/g, ' ').trim()
      return { ok: Boolean(raw), resume: raw ? { raw } : {} }
    }
    return { ok: true, resume }
  }

  // ---- 校准：真实 DOM 结构探查（用于回填 config.yaml → selectors）----
  function stableSelector(el) {
    // 从 body 向下生成含 tag + 首个 class 的稳定路径
    const parts = []
    let node = el
    while (node && node !== document.body) {
      const tag = node.tagName.toLowerCase()
      const cls = node.className && typeof node.className === 'string'
        ? String(node.className).split(/\s+/).filter(Boolean)[0] || ''
        : ''
      parts.unshift(cls ? `${tag}.${cls}` : tag)
      node = node.parentElement
    }
    return parts.join(' > ')
  }

  function calibrate() {
    // 用候选卡片选择器找首卡；否则以首个 job 链接向上定位
    let card = qsa(SELECTORS.jobCard)[0]
    if (!card) {
      const a = qsa('a[href*="/job_detail/"]')[0]
      if (a) card = a.closest('.job-card-wrapper, li[ka], .job-card-left, .job-list-box li') || a.closest('li') || a.parentElement
    }
    if (!card) return { ok: false, error: '未定位到岗位卡片，无法校准' }

    const html = card.outerHTML.slice(0, 6000)
    const link = (card.querySelector('a[href*="/job_detail/"]')?.href || '').split('?')[0]

    return {
      ok: true,
      url: location.href,
      cardPath: stableSelector(card),
      cardLink: link,
      html,
      // 候选字段选择器在首卡内的命中情况
      probes: {
        name: textOf(card, [SELECTORS.jobName]),
        company: textOf(card, [SELECTORS.companyName]),
        salary: textOf(card, [SELECTORS.salary]),
        area: textOf(card, [SELECTORS.jobArea]),
        tags: qsa(SELECTORS.tags).slice(0, 10).map((t) => t.textContent.trim()),
        nextPage: !!firstOf([SELECTORS.nextPageBtn]),
      },
    }
  }

  function register() {
    send({ from: 'page', id: PAGE_ID, type: 'register', payload: { url: location.href } })
  }

  // ================= M5 computer-use 原语：snapshot / act =================

  const MAX_SNAPSHOT_TEXT = 60

  // 上一次快照的元素签名集合，用于「本步新增元素」标注（帮模型聚焦页面变化）
  let lastSigSet = new Set()

  /** 交互元素的精简 label：自身直接文本 + aria/title/placeholder，不含整棵子树（降噪省 token） */
  function _label(el) {
    const aria = el.getAttribute
      ? (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt') || '')
      : ''
    let own = ''
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent
    own = own.trim().replace(/\s+/g, ' ')
    if (!own && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) own = String(el.value || '').trim()
    const a = String(aria || '').trim()
    const t = a ? (own ? `${own} (${a.slice(0, 30)})` : a) : own
    return t.slice(0, MAX_SNAPSHOT_TEXT)
  }

  /** 统一「是否纳入快照」判定（collectElements 与 locateRef 回退共用，保证 ref 序号一致） */
  function snapRelevant(el) {
    if (!el || el.nodeType !== 1) return false
    if (NO_TEXT_TAGS.test(el.tagName)) return false
    if (!isVisible(el)) return false
    if (isInteractive(el)) return _label(el).length > 0
    if (!el.firstElementChild) {
      const t = (el.textContent || '').trim().replace(/\s+/g, ' ')
      return t.length > 0 && t.length <= 80
    }
    return false
  }

  /** 由叶子文本节点上溯找「可点击」目标：自身/祖先里 interactive 或 class 含 btn/button/confirm 者 */
  function _clickableSelfOrAncestor(el) {
    let node = el
    for (let i = 0; node && node !== document.body && i < 6; i++, node = node.parentElement) {
      if (isInteractive(node)) return node
      const cls = typeof node.className === 'string' ? node.className : ''
      if (/btn|button|confirm|submit/i.test(cls)) return node
    }
    return el
  }

  /** 祖先链是否含 within_text 卡片文案（用于 within_text 锁定） */
  function _withinCard(el, w) {
    if (!w) return true
    let anc = el
    while (anc && anc !== document.body) {
      if (String(anc.textContent || '').toLowerCase().includes(w)) return true
      anc = anc.parentElement
    }
    return false
  }

  /** 按文本稳健定位可点击元素：命中 match_text，可限定含 within_text 的祖先卡片，取第 occurrence 个 */
  function findByText(match, within, occurrence) {
    const m = String(match || '').trim().toLowerCase()
    if (!m) return null
    const w = String(within || '').trim().toLowerCase()
    const cands = []
    const seen = new Set()
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT)
    let node = walker.nextNode()
    // 第一遍：严格命中「可见 + 语义可交互」的元素
    while (node) {
      const el = node
      if (isVisible(el) && isInteractive(el) && _label(el).toLowerCase().includes(m) && _withinCard(el, w)) {
        cands.push(el)
        seen.add(el)
      }
      node = walker.nextNode()
    }
    // 兜底：弹框里的【继续沟通】等常是非语义的 div/span（addEventListener 绑定），
    // 第一遍会漏。再扫可见的短文本叶子，命中后上溯到可点击祖先。
    if (!cands.length) {
      const walker2 = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT)
      let n2 = walker2.nextNode()
      while (n2) {
        const el = n2
        const own = String(el.textContent || '').trim().replace(/\s+/g, ' ')
        if (!el.firstElementChild && own && own.length <= 30 && own.toLowerCase().includes(m)) {
          const target = _clickableSelfOrAncestor(el)
          if (target && isVisible(target) && !seen.has(target) && _withinCard(target, w)) {
            cands.push(target)
            seen.add(target)
          }
        }
        n2 = walker2.nextNode()
      }
    }
    if (!cands.length) return null
    const oi = Number.isFinite(occurrence) ? occurrence : 0
    return cands[oi] || cands[0] || null
  }

  /** 等页面「沉降」：body 无 DOM 变动 idle 毫秒即返回，max 毫秒硬上限（替代固定 sleep） */
  function waitForSettle(idle, max) {
    idle = idle || 300
    max = max || 2500
    return new Promise((resolve) => {
      let timer = null
      let done = false
      const finish = () => {
        if (done) return
        done = true
        try { obs.disconnect() } catch { /* noop */ }
        if (timer) clearTimeout(timer)
        clearTimeout(cap)
        resolve()
      }
      const obs = new MutationObserver(() => {
        if (done) return
        if (timer) clearTimeout(timer)
        timer = setTimeout(finish, idle)
      })
      try { obs.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true }) } catch { /* noop */ }
      const cap = setTimeout(finish, max)
      timer = setTimeout(finish, idle)
    })
  }

  /** 主动等待：轮询直到 URL/选择器/文本出现或超时（timeout 上限 15s） */
  async function waitFor(params) {
    const timeout = Math.min(Number(params.timeout || 6000), 15000)
    const start = Date.now()
    const sel = params.selector
    const urlc = params.url_contains
    const txt = String(params.text || '').toLowerCase()
    while (Date.now() - start < timeout) {
      if (urlc && location.href.includes(urlc)) return true
      if (sel && document.querySelector(sel)) return true
      if (txt) {
        const els = document.querySelectorAll('a, button, [role], [class*="btn"], .job-name, .name')
        for (const e of els) {
          if (_label(e).toLowerCase().includes(txt) || String(e.textContent || '').toLowerCase().includes(txt)) return true
        }
      }
      await sleep(250)
    }
    return false
  }

  /** 是否「可见且可交互」或「可作为点击目标」的候选 */
  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false
    const st = window.getComputedStyle(el)
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false
    if (el.getAttribute('aria-hidden') === 'true') return false
    if (el.disabled) return false
    const r = el.getBoundingClientRect()
    return r.width > 0 && r.height > 0
  }

  /** 由 tag / aria / type 推导 role 与是否可点击 */
  function roleOf(el) {
    const aria = el.getAttribute('role')
    if (aria) return aria
    const tag = el.tagName.toLowerCase()
    if (['button', 'a'].includes(tag)) return tag === 'a' ? 'link' : 'button'
    if (tag === 'input') return el.type === 'checkbox' || el.type === 'radio' ? 'input' : 'textbox'
    if (tag === 'textarea') return 'textbox'
    if (tag === 'select') return 'listbox'
    return null
  }
  function isInteractive(el) {
    if (roleOf(el)) return true
    const tag = el.tagName.toLowerCase()
    return tag === 'button' || tag === 'a' || tag === 'input' || tag === 'select' ||
      tag === 'textarea' || el.hasAttribute('onclick') || el.hasAttribute('contenteditable')
  }

  const NO_TEXT_TAGS = /^(script|style|noscript|template)$/i

  /**
   * 收集可见可交互元素并分配确定性的 DOM 顺序索引作为 ref；
   * 同时给无 id/name 者打一次性 data-bs-ref 锚点，供 act 重定位（避免快照间 DOM 微变）。
   */
  function collectElements(max) {
    const out = []
    const curSigSet = new Set()
    let idx = 0
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT)
    let node = walker.nextNode()
    while (node && out.length < max) {
      const el = node
      // 降噪：交互元素取精简 label，非交互仅保留「无子元素的短文本叶子」，跳过重复文本的祖先容器
      if (snapRelevant(el)) {
        const label = isInteractive(el)
          ? _label(el)
          : (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, MAX_SNAPSHOT_TEXT)
        const ref = `e${idx++}`
        if (!el.id && !el.name) el.setAttribute('data-bs-ref', ref)
        const sig = `${el.tagName.toLowerCase()}|${roleOf(el) || ''}|${label}`
        // 瞦身：只留定位/决策必需字段。act 靠 ref/text 定位，从不吃坐标，
        // 故去掉 bounds/visible/disabled:false 等冗余（既省 token、又免二次 getBoundingClientRect）。
        const item = {
          ref,
          tag: el.tagName.toLowerCase(),
          role: roleOf(el),
          text: label,
        }
        if (el.disabled) item.disabled = true
        if (!lastSigSet.has(sig)) item.new = 1 // 标注本步相对上一步新增的元素
        curSigSet.add(sig)
        out.push(item)
      }
      node = walker.nextNode()
    }
    lastSigSet = curSigSet
    return out
  }

  function snapshot(params) {
    const max = params && params.max ? Number(params.max) : 80
    const out = {
      url: location.href,
      title: document.title,
      page: pageKind(),
      scroll: { y: window.scrollY, h: document.body.scrollHeight },
      elements: collectElements(max),
    }
    // 岗位列表页附上结构化岗位清单（含已沟通标记），供 Agent 过滤与选择
    if (out.page === 'list') {
      const c = collectJobs(false)
      out.jobs = c.jobs.slice(0, 48).map((j) => {
        // tags 前两项常就是经验/学历（collectJobs 的退化取值），去重以免重复占 token；
        // tags 是技能比对的主要依据，不能省
        const tags = (j.tags || [])
          .filter((t) => t && t !== j.experience && t !== j.education)
          .slice(0, 6)
        return {
          name: j.name, company: j.company, salary: j.salary, area: j.area,
          experience: j.experience, education: j.education, tags, contacted: j.contacted,
        }
      })
    }
    return out
  }

  /** 按 ref 定位：先 `[data-bs-ref=ref]`，退化到「按 DOM 顺序第 idx 个候选」 */
  function locateRef(ref) {
    if (!ref) return null
    const anchored = document.querySelector(`[data-bs-ref="${ref}"]`)
    if (anchored && isVisible(anchored)) return anchored
    const idx = parseInt(String(ref).replace(/^e/, ''), 10)
    if (!Number.isFinite(idx)) return null
    const max = 50000
    let count = 0
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT)
    let node = walker.nextNode()
    while (node && count <= idx) {
      // 与 collectElements 用同一判定，保证按序补位的 ref 序号一致
      if (snapRelevant(node)) {
        if (count === idx) return node
        count += 1
      }
      node = walker.nextNode()
    }
    return null
  }

  /** 输入类元素原生赋值并派发 input/change */
  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype :
      el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set
    setter.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new Event('change', { bubbles: true }))
  }

  const sleep = (ms) => new Promise((res) => setTimeout(res, ms))

  /**
   * 安全点击：若目标是 href 为 javascript:/空 的 <a>，抑制其默认导航
   * （否则站点 CSP 会报 “Running the JavaScript URL”）；其自身 addEventListener 仍正常触发。
   */
  function _safeClick(el) {
    const a = el && el.closest ? el.closest('a') : null
    const href = a && a.getAttribute ? a.getAttribute('href') : null
    if (a && href && /^\s*(javascript:|data:|vbscript:)\s*/i.test(href)) {
      a.addEventListener('click', (e) => { e.preventDefault() }, { once: true, capture: true })
    } else if (a && (!href || href.trim() === '#' || href.trim() === '')) {
      a.addEventListener('click', (e) => { e.preventDefault() }, { once: true, capture: true })
    }
    el.click()
  }

  /** 找岗位列表的内层可滚动容器（无限加载发生在它内部）；取不到返回 null（回退 window 滚动） */
  function findJobListScroller() {
    let best = null
    let bestCards = 0
    try {
      for (const el of document.querySelectorAll('div, section, ul')) {
        const st = window.getComputedStyle(el)
        if (st.overflowY !== 'auto' && st.overflowY !== 'scroll') continue
        if (el.scrollHeight <= el.clientHeight + 24) continue
        const cards = el.querySelectorAll(SELECTORS.jobCard).length
        if (cards > bestCards) { bestCards = cards; best = el }
      }
    } catch { /* noop */ }
    return best
  }

  /** 把页面/列表滚到底，反复几轮触发无限加载，直到卡片数不再增长。 */
  async function loadMoreJobs(params) {
    const scroller = findJobListScroller()
    const before = qsa(SELECTORS.jobCard).length
    const maxRounds = Math.min(Number((params && params.rounds) || 3), 6)
    let last = before
    for (let i = 0; i < maxRounds; i++) {
      if (scroller) scroller.scrollTop = scroller.scrollHeight
      else window.scrollTo(0, document.body.scrollHeight)
      await waitForSettle(500, 2500)
      const now = qsa(SELECTORS.jobCard).length
      if (now <= last) break // 无新增→可能到底
      last = now
    }
    return {
      ok: true, element: { ref: null, op: 'load_more' },
      before, after: last, added: last - before, bottom: last === before, updated: snapshot(),
    }
  }

  async function act(params) {
    const op = params && params.op
    // 页面级操作：后退 / 跳转。先即时回执（dom_result 会在旧页销毁前发出，避免 20s
    // page_action_timeout），再把导航排到下一 tick；导航后页面会重连，Agent 下一步 snapshot 见新页。
    if (op === 'go_back' || op === 'navigate') {
      if (op === 'go_back') {
        setTimeout(() => { try { window.history.back() } catch { /* noop */ } }, 0)
        return { ok: true, element: { ref: null, op: 'go_back' }, navigating: true }
      }
      // navigate：只允许 http(s)，拦截空串与 javascript:/data: 等危险 scheme
      const raw = String(params.text || '').trim()
      if (!raw) {
        return { ok: false, error: 'navigate_url_empty', updated: snapshot() }
      }
      let target
      try {
        target = new URL(raw, location.href)
      } catch {
        return { ok: false, error: 'navigate_url_invalid', updated: snapshot() }
      }
      if (target.protocol !== 'http:' && target.protocol !== 'https:') {
        return { ok: false, error: `navigate_scheme_forbidden:${target.protocol}`, updated: snapshot() }
      }
      const href = target.href
      setTimeout(() => { try { window.location.href = href } catch { /* noop */ } }, 0)
      return { ok: true, element: { ref: null, op: 'navigate' }, navigating: true }
    }
    // 主动等待：等 URL/选择器/文本出现或页面稳定后再返回快照
    if (op === 'wait') {
      const met = await waitFor(params || {})
      const snap = snapshot()
      snap.wait_met = met
      return { ok: true, element: { ref: null, op: 'wait' }, updated: snap }
    }
    // 页面级滚动/加载（无需 ref）：Boss 列表是内层容器无限加载，window 滚动不驱动它。
    if (op === 'scroll') {
      const scroller = findJobListScroller()
      const before = qsa(SELECTORS.jobCard).length
      const up = params.direction === 'up' || params.text === 'up'
      const amt = Math.abs(Number(params.amount || 600)) * (up ? -1 : 1)
      if (scroller) scroller.scrollTop += amt
      else window.scrollBy(0, amt)
      await waitForSettle(300, 1500)
      const after = qsa(SELECTORS.jobCard).length
      return { ok: true, element: { ref: null, op: 'scroll' }, added: after - before, updated: snapshot() }
    }
    if (op === 'load_more') {
      return await loadMoreJobs(params)
    }

    const ref = params && params.ref
    let el = ref ? locateRef(ref) : null
    // ref 定位不到且给了 match_text：按文本稳健定位（抗 SPA 重渲染，命中【立即沟通】/岗位卡）
    if (!el && params && params.match_text) {
      const found = findByText(params.match_text, params.within_text, Number(params.occurrence || 0))
      if (found) el = found
    }
    if (!el) {
      return {
        ok: false,
        error: params && params.match_text ? 'text_not_found' : 'ref_not_found',
        match_text: (params && params.match_text) || null,
        updated: snapshot(),
      }
    }

    try {
      switch (op) {
        case 'click': {
          // 导航安全：点击可能触发整页跳转（如【继续沟通】→聊天页），销毁本页致 dom_result 丢失、
          // 服务端 20s 超时。用 pagehide + 短暂探测：一旦开始跳转就即时回执、不等快照，下一步 snapshot 见新页。
          const startHref = location.href
          let navStarted = false
          const onHide = () => { navStarted = true }
          window.addEventListener('pagehide', onHide, true)
          try {
            _safeClick(el)
            await sleep(280)
            if (navStarted || location.href !== startHref) {
              return { ok: true, element: { ref, op: 'click' }, navigating: true }
            }
            await waitForSettle(350, 2500)
          } finally {
            window.removeEventListener('pagehide', onHide, true)
          }
          if (navStarted || location.href !== startHref) {
            return { ok: true, element: { ref, op: 'click' }, navigating: true }
          }
          break
        }
        case 'type':
          el.focus()
          setNativeValue(el, String(params.text || ''))
          break
        case 'select':
          el.focus()
          setNativeValue(el, String(params.value || params.text || ''))
          break
        case 'press':
          el.dispatchEvent(new KeyboardEvent('keydown', { key: params.key || 'Enter', bubbles: true }))
          el.dispatchEvent(new KeyboardEvent('keyup', { key: params.key || 'Enter', bubbles: true }))
          break
        default:
          return { ok: false, error: `unknown_op:${op}`, updated: snapshot() }
      }
    } catch (e) {
      return { ok: false, error: String(e), updated: snapshot() }
    }
    // 关键：操作后必带最新快照，闭环「看→做→看」
    return { ok: true, element: { ref, op }, updated: snapshot() }
  }

  /** 向输入框派发 Enter（Boss 聊天多为 Enter 发送） */
  function pressEnter(el) {
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }
    el.dispatchEvent(new KeyboardEvent('keydown', opts))
    el.dispatchEvent(new KeyboardEvent('keypress', opts))
    el.dispatchEvent(new KeyboardEvent('keyup', opts))
  }

  /** 是否已在真正的聊天面：URL 到 /web/geek/chat，或页面已判为 chat。 */
  function onChatSurface() {
    if (location.href.includes('/web/geek/chat')) return true
    return pageKind() === 'chat'
  }

  /** M7 SOP：定位输入框 → 填话术 → 点【发送】（选择器→文本→Enter 兵底）逐字不改写 */
  async function sendGreeting(params) {
    const text = String((params && params.text) || '').trim()
    if (!text) return { ok: false, error: 'greeting_text_empty' }
    // 硬校验：必须先真正进入聊天页才能发。避免在列表/详情/“已向BOSS发送消息”弹框上
    // 误发并留下假成功（会错标“已打招呼”、污染过目清单）。
    const input = firstVisibleSel(SELECTORS.greetingInput)
    if (!onChatSurface() || !input) {
      return {
        ok: false,
        error: 'not_on_chat_page',
        hint: '先点【立即沟通】；若弹出【已向BOSS发送消息】就点其中的【继续沟通】进入聊天页，再调 send_greeting。',
      }
    }
    try {
      input.focus && input.focus()
      if (input.isContentEditable) {
        input.innerText = text
        input.dispatchEvent(new Event('input', { bubbles: true }))
      } else {
        setNativeValue(input, text)
      }
      await sleep(400) // 等输入生效再发送

      // 发送：优先点【发送】按钮（选择器→文本），失败再 Enter 兑底；每步后校验“已发出”（输入框被清空）。
      const sentCleared = () => {
        const cur = firstVisibleSel(SELECTORS.greetingInput)
        if (!cur) return true // 输入框已消失（页面切换）视为已发出
        const v = (cur.isContentEditable ? cur.innerText : cur.value) || ''
        return v.trim().length === 0
      }
      const btn = firstVisibleSel(SELECTORS.greetingSend) || findByText('发送', '', 0)
      const tried = []
      if (btn && isVisible(btn)) { _safeClick(btn); tried.push('button') }
      await sleep(500)
      if (!sentCleared()) { pressEnter(input); tried.push('enter'); await sleep(500) }
      let sent = sentCleared()
      if (!sent && btn && isVisible(btn)) { _safeClick(btn); await sleep(500); sent = sentCleared(); tried.push('button2') }

      if (sent) return { ok: true, sent: text, via: tried.join('+') || 'button' }
      return {
        ok: false,
        error: 'send_not_confirmed',
        via: tried.join('+'),
        hint: '话术已填入但未确认发出（输入框未清空）。别当作已发送、别 go_back；请重试 send_greeting 或在页面上手动点发送。',
      }
    } catch (e) {
      return { ok: false, error: String(e) }
    }
  }

  /** 高层复合动作：确定性进入聊天页，把“点立即→处理随机弹框→到聊天页”从模型手里收回来。 */
  async function startChat(params) {
    const within = params && params.within_text
    const occ = Number((params && params.occurrence) || 0)
    let navStarted = false
    const onHide = () => { navStarted = true }
    window.addEventListener('pagehide', onHide, true)
    try {
      if (onChatSurface()) {
        return { ok: true, reached_chat: true, phase: 'already_chat', updated: snapshot() }
      }
      const btn = findByText('立即沟通', within, occ)
      if (!btn) {
        return { ok: false, error: 'no_immediate_chat_button', within_text: within || null, updated: snapshot() }
      }
      _safeClick(btn)
      const deadline = Date.now() + 3500
      while (Date.now() < deadline) {
        // 导航中：即时回执，不等会随旧页销毁的快照（下一步 snapshot 见新页）
        if (navStarted || location.href.includes('/web/geek/chat')) {
          return { ok: true, reached_chat: location.href.includes('/web/geek/chat'), phase: 'navigating',
            hint: '页面在跳转，下一步 browser_snapshot 确认已到聊天页再 send_greeting' }
        }
        if (onChatSurface()) {
          return { ok: true, reached_chat: true, phase: 'chat', updated: snapshot() }
        }
        const cont = findByText('继续沟通', '', 0) // 弹框【已向BOSS发送消息】里的继续沟通
        if (cont && isVisible(cont)) {
          _safeClick(cont)
          // 继续沟通常触发整页跳转：立即同步回执（不 await，赶在页面销毁前发出 dom_result），避免 20s 超时
          return { ok: true, reached_chat: false, confirm_clicked: true, phase: 'navigating',
            hint: '已点【继续沟通】，页面在跳转；下一步 browser_snapshot 确认到聊天页再 send_greeting（别因跳转判定无按钮而跳过本岗位）' }
        }
        await sleep(250)
      }
      return { ok: false, reached_chat: false, phase: 'stuck', error: 'no_chat_and_no_confirm', updated: snapshot() }
    } finally {
      window.removeEventListener('pagehide', onHide, true)
    }
  }

  // ---- WS 连接与协议 ----
  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg))
    }
  }

  function connect() {
    try {
      ws = new WebSocket(SERVER_WS)
    } catch {
      scheduleReconnect()
      return
    }
    ws.onopen = () => {
      lastServerSeen = Date.now()
      register()
      notifyBridge({ type: 'page_connected', ok: true })
    }
    ws.onclose = () => {
      if (ws && ws._hbTimer) clearInterval(ws._hbTimer)
      notifyBridge({ type: 'page_connected', ok: false })
      scheduleReconnect()
    }
    ws.onerror = () => ws?.close()
    ws.onmessage = (ev) => {
      lastServerSeen = Date.now() // 收到 server 任意报文（含 pong）即视为存活
      let msg
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      if (msg.to && msg.to !== PAGE_ID) return

      if (msg.type === 'pong') return // 心跳回执，无需处理

      if (msg.type === 'dom') {
        dispatch(msg.payload?.action, msg.payload?.params).then((res) => {
          send({
            from: 'page',
            id: PAGE_ID,
            type: 'dom_result',
            idem: msg.idem,
            result: res,
          })
        })
      }
    }
    // 静默心跳：连接存活期每 KEEPALIVE_MS 发一次，server 单播回 pong；供看门狗判活
    ws._hbTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        send({ from: 'page', id: PAGE_ID, type: 'keepalive' })
      }
    }, KEEPALIVE_MS)
  }

  // 看门狗：长时间没收到 server 任何回复（含 pong）即判定半开，主动断开重连（重连会重新 register）。
  function startWatchdog() {
    if (watchdogTimer !== null) return
    watchdogTimer = setInterval(() => {
      if (stopped || !ws || ws.readyState !== WebSocket.OPEN) return
      if (Date.now() - lastServerSeen > LIVENESS_TIMEOUT_MS) {
        try { ws.close() } catch { /* onclose 已兜底重连 */ }
      }
    }, WATCHDOG_MS)
  }

  async function dispatch(type, payload) {
    const handler = domHandlers[type]
    if (typeof handler !== 'function') {
      return { ok: false, error: `unknown_dom_type:${type}` }
    }
    try {
      return { ok: true, data: await handler(payload) }
    } catch (e) {
      return { ok: false, error: String(e) }
    }
  }

  function scheduleReconnect() {
    if (stopped || reconnectTimer !== null) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 2000)
  }

  function notifyBridge(payload) {
    try {
      chrome.runtime.sendMessage({ type: 'page_status', payload }).catch(() => {})
    } catch {
      /* 无扩展环境时忽略 */
    }
  }

  connect()
  startWatchdog()
})()