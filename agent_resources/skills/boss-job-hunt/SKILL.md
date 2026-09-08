---
name: boss-job-hunt
description: Boss 直聘逐岗求职的完整 SOP 与异常处置手册。当用户明确要找工作/投递/逐岗沟通时，必须先 read_file 加载本 skill，并按其中「逐岗求职主流程」执行；页面出现登录失效/验证码/风控弹窗/找不到【立即沟通】/岗位卡片结构异常，或在撰写话术、判断是否投递时，也参考本手册对应章节。
---

# Boss 直聘求职 — 完整 SOP 与异常处置

本 skill 为**按需加载**：只有识别到「找工作/投递」意图才读取。主流程 SOP 在此完整定义，
AGENTS.md 只做意图分流；未加载本文件时**不要**进入逐岗投递流程。

浏览器操作一律走 **Playwright MCP 官方工具**（元素靠快照里的 `ref` 定位）：
`browser_snapshot`（看页面）、`browser_find`（按文本定位）、`browser_click`/`browser_type`/
`browser_press_key`/`browser_hover`/`browser_drag`（交互）、`browser_fill_form`/`browser_select_option`
（填表/选下拉）、`browser_navigate`/`browser_navigate_back`（跳转）、`browser_tabs`（标签页）、
`browser_handle_dialog`（弹框）、`browser_file_upload`（上传）、`browser_take_screenshot`（截图）、
`browser_console_messages`/`browser_network_requests`/`browser_network_request`（控制台/网络排查）、
`browser_evaluate`（执行 JS：滚动无限列表、读 SPA 状态）、`browser_wait_for`（等待）、
`browser_close`/`browser_resize`（收尾）。**没有 browser_act / start_chat / send_greeting 这类自研
工具**——进入聊天页、发送打招呼都由这些官方工具组合完成。

## 零、开工前：确认求职意向（不可跳过）

用户说「帮我找工作」但没给条件时，**先提问澄清**，齐了再动手：

- 目标岗位方向（如 Python 后端 / 前端 / 数据分析）
- 城市（如 深圳 / 远程）
- 期望薪资（可选，用于排序与过滤）

条件齐备、用户确认后，才进入第 1 步搜岗。若用户中途只给了部分条件，先做能做的部分
（如先按岗位+城市搜），同时说明还缺什么。

## 一、关键页面地址（导航与状态校验的权威依据）

- **岗位列表页**：`https://www.zhipin.com/web/geek/jobs`（可带 `?query=关键词&city=城市码` 等筛选参数）
  —— `browser_snapshot` 的 `page=='list'`。回列表找下一个岗位时 `browser_navigate` 到此。
- **聊天页**：`https://www.zhipin.com/web/geek/chat`（点【立即沟通】→弹框【继续沟通】后跳转至此）
  —— `browser_snapshot` 的 `page=='chat'`。**只有到了这一页才能生成并发送话术。**

## 二、自主执行原则（无人值守，重要）

- 你的目标是**替用户逐岗跑完「沟通→打招呼→发送」的完整闭环**，不是只产出一份筛选清单。
  **绝不要列完符合要求的岗位就停下等指示**。
- 对每一个达标岗位，按「主流程」第 3~8 步逐一执行完再进入下一个。
- **列表是内层容器无限加载（不是分页按钮）**：当前可见岗位都处理完/被过目清单过滤空了、但还没到“确无更多”时，
  滚动列表容器到底触发加载下一页——优先用 `browser_evaluate` 执行 JS 把内层可滚动容器
  （`document.querySelectorAll('div,section,ul')` 里 `overflowY` 为 auto/scroll 且 `scrollHeight>clientHeight` 者）
  的 `scrollTop` 置为其 `scrollHeight`；再 `browser_wait_for` + `browser_snapshot` 看是否新增 `jobs`
  （兜底可用 `browser_press_key` 向下滚动）；直到新增岗位为 0 才说明真的到底。
- **只有当（含翻页）确无更多达标岗位时**，才在回复末尾单独输出一行：`【求职任务结束】`，并简要总结已投递岗位。
  这是系统判定「任务完成」的信号；没做完就不要提前输出。
- 若 `check_greeting` 返回 `greeting_cap_reached`（已达本轮投递上限），立即停止投递，输出 `【求职任务结束】` 并总结。
- 发送模式由服务端配置决定：unattended 自动发送不暂停（confirm 模式会在 `check_greeting` 后暂停等你确认）。

## 三、任务规划（write_todos）

你有 `write_todos` 工具来维护一份持久任务清单，**必须用它约束整轮投递、防跳步/遗忘/乱套**：

- **规划**：读完岗位列表并比对后，用 `write_todos` 建立清单——每个「达标且未沟通」的岗位一条 todo
  （如「沟通岗位：<岗位名>@<公司>」），初始 status=pending。
- **推进**：开始处理某岗位时把它标 in_progress；走完「立即沟通→继续沟通→进聊天页→发送并登记」
  并拿到工具返回后，再标 completed，然后下一条。一次只保持**一条** in_progress。
- **收口**：清单里仍有 pending 岗位时，**不要**输出 `【求职任务结束】`、不要停下只汇报清单；继续逐条做完。
  仅当（含翻页）确无更多达标岗位、或 `check_greeting` 返回 `greeting_cap_reached` 时，才收尾并输出哨兵。
- `write_todos` 是**整体覆盖式**提交：每次都要传当前完整列表（含刚更新的 status），不是只传改动项。

## 四、逐岗求职主流程（SOP）

对每一个候选岗位，按序执行：

1. **准备背景**：搜岗或判断匹配前，先调用一次 `get_resume_summary()` 作为简历背景。
2. **搜岗**：根据确认好的求职意向，`browser_navigate` 到岗位列表页（可带 query/city 参数），
   `browser_wait_for` 等页面稳定，`browser_snapshot` 看列表与筛选区；需要筛选用 `browser_click`
   点城市/薪资等筛选项，再 `browser_snapshot` 确认列表。
3. **读 JD**：优先直接用 `browser_snapshot` 返回列表里该岗位的结构化对象 `jobs[i]`（岗位名/薪资/年限/学历/技能/城市/职责）；
   仅当列表缺职责/技能等关键字段时，才 `browser_click` 点进详情页再 `browser_snapshot` 补全。
4. **比对**：把 `jobs[i]` 那个对象**原样**作为 `job` 参数传给 `compare_job_with_resume`（不要自行重拼字段名，服务端自动兼容键名），
   拿到逐模块比对与匹配分。若结论提示「岗位字段缺口过大」，说明列表信息不够：点进详情页拿到完整 JD 后重新比对，不要凭残缺信息投递。
5. **决策**：仅当匹配分达标（结论「符合，可进入沟通环节」）时才沟通；否则跳过，回到第 3 步看下一个岗位。
6. **进入聊天页（用官方工具组合，替代原 start_chat）**：确定要沟通某岗位后：
   - `browser_snapshot`（或 `browser_find`）定位该岗位卡片上的【立即沟通】按钮；
   - `browser_click` 点它；`browser_wait_for`（等待跳转/弹框）；
   - `browser_snapshot` 检查：若出现【已向BOSS发送消息】弹框 → `browser_click` 点弹框【继续沟通】；
     `browser_wait_for` 再确认；
   - `browser_snapshot` 验证已到聊天页（`page=='chat'` 且有输入框）。未到则再等再验，**不要反复乱点**；
     若确认该岗位无【立即沟通】按钮/已沟通，跳过换下一个。
7. **聊天页硬门槛（发送前必过）**：必须 `browser_snapshot` 确认 `page=='chat'` **且**能看到消息输入框才算到位。
   `page=='chat'` 可能是专页 URL `/web/geek/chat`，也可能是当前页弹框（URL 不变也算）；不要死等 URL。
   **未达此门槛绝不发送话术。** 一旦已到聊天页，必须马上完成发送；在发送成功（或确认进不去）之前，绝不离开/转下一个岗位。
8. **生成话术并发送（标准链路）**：
   - 在聊天页结合岗位要求与简历匹配点，拟一条简短、真诚、突出匹配点的话术（规范见「六、话术撰写」）；
   - 先调 `check_greeting(text)` 预检护栏（confirm 模式会暂停等你确认，unattended 直接返回 ok）；
   - 预检 `ok:true` 后发送：`browser_click` 聚焦输入框 → `browser_type` 输入话术 → `browser_press_key` Enter；
   - **发送后用 `browser_snapshot` 确认输入框已清空（消息发出）**，再调 `confirm_greeting_sent(text)` 登记
     （计数 +1、话术指纹、岗位升级为已打招呼）；
   - 若输入框未清空（发送未成功）：重试发送或在页面手动点发送，确认发出前**不要 go_back/换岗位**。
9. **下一个岗位**：按发送结果行动。若需继续，`browser_navigate` 回岗位列表页
   `https://www.zhipin.com/web/geek/jobs`（或 `browser_press_key` 后退）；回列表后 `browser_wait_for` 等页面稳定，
   再 `browser_snapshot` 重看列表，跳过已沟通过（`contacted=true` / 带【已沟通】标记）的岗位，回到第 3 步寻找下一个匹配达标岗位；
   没有更多符合岗位时，向用户简要说明已完成的投递并结束。

> ⚠️ 区分两个【继续沟通】（很关键）：
> - **弹框里的【继续沟通】**＝首次沟通确认按钮 → **要点它**进入聊天页（见第 6 步）。
> - **岗位卡片上的【继续沟通】/【已沟通】标记**（`contacted=true`）＝该岗位此前已沟通过 → **必须跳过**，不要重复打扰。
> 判据：处于确认对话框/遮罩弹层内的才是「点它继续」；处于列表卡片按钮位置的是「已沟通，跳过」。

## 五、异常处置（优先保命，宁可停下也不要乱点）

- **页面未连接**：`browser_snapshot` 等工具报连接失败/无浏览器时，提示用户确认浏览器已打开、
  Playwright 扩展已连接、且当前标签页是 Boss 直聘页面（`https://www.zhipin.com/web/geek/jobs`），不要反复重试。
- **操作超时**：页面可能卡顿，先 `browser_wait_for` 再 `browser_snapshot` 重新确认状态；
  同一操作连续失败两次即停下向用户说明。
- **登录失效 / 需要验证码 / 风控弹窗**：一旦快照里出现「登录」「扫码」「验证码」「安全验证」等，
  **立即停止一切自动操作**，把情况如实告知用户，等待人工处理。绝不尝试填写验证码或模拟登录。
- **ref 失效点不到目标**：SPA 列表重渲染后 ref 常失效（报 `Ref not found`）。**先 `browser_snapshot` 拿新 ref**
  或用 `browser_find` 按文本重新定位目标（如「立即沟通」+ 岗位名），不要拿着旧 ref 硬点。
- **点【立即沟通】没进聊天页**：可能跳到中间页或弹【继续沟通】。`browser_wait_for` 等页面稳定后重新
  `browser_snapshot`；出现【继续沟通】就 `browser_click` 点进去，直到 `browser_snapshot` 显示聊天页输入框。
- **岗位卡片字段缺失**：列表里拿不到职责/技能时不要凭残缺信息投递，点进详情页补全后再
  `compare_job_with_resume`。

## 六、打招呼话术撰写规范

话术经 `check_greeting` 预检后由浏览器工具发送（confirm 模式会先经你确认）。撰写要求：

- **短**：2~4 句、控制在 60~120 字，HR 一眼能读完。
- **真诚、不谄媚**：不要「贵公司是最好的」这类空话。
- **突出与该岗位的匹配点**：从 `compare_job_with_resume` 命中的技能/经历里挑 1~2 个最相关的点明。
- **带一个轻量行动召唤**：如「方便的话想进一步沟通，我的简历已发～」，不要施压。
- **结构模板**（示意，勿照抄）：
  1. 问候 + 对岗位的兴趣（点名岗位名/方向）
  2. 一句最硬的匹配点（技能或项目经历，对齐 JD 关键要求）
  3. 一句礼貌的行动召唤
- **禁止**：编造未写在简历里的经历/技能；出现薪资谈判、离职原因等敏感话题（这是后续沟通阶段的事）。

## 七、是否值得投递的判断

- 以 `compare_job_with_resume` 的结论为准：仅「符合，可进入沟通环节」才投递。
- 「匹配偏弱，谨慎沟通」或「岗位字段缺口过大」时：前者可结合职责主观斟酌，后者必须先补全 JD 再判。
- 已沟通过（`contacted=true` / 按钮【继续沟通】/ 带【已沟通】标记）的岗位一律跳过，避免重复打扰。
- 单轮会话有系统级投递上限；`check_greeting` 返回 `greeting_cap_reached` 时，总结已投递岗位并结束，不要重试。
