# 页面操作经验（agent 自更新记忆 · lessons）

> 本文件由 Deep Agents 作为长期记忆**每次运行自动注入**。Agent 用 `edit_file`/`write_file`
> 在 `/memories/` 下维护它。规则：每条一行、简短且可复用；操作前先照此执行，避免重复踩坑；
> 只增删条目本身；保持精简（≤ ~30 条），过时/重复的合并或删除。把成功避开某坑的做法沉淀进来。

## 沟通链路
- 【立即沟通】用 `match_text='立即沟通'` + `within_text=岗位名` 定位；SPA 列表 ref 会漂移，别依赖 ref。
- 首次点【立即沟通】后，Boss 会自动发一条默认消息并弹出 **【已向BOSS发送消息】** 框——**必须点框里的【继续沟通】**才进入聊天页。
- 【继续沟通】会稳定出现在上一步操作的返回里（关键按钮不被去重藏掉）：看到就 `browser_act` `match_text='继续沟通'` 点它。
- **未真正进入聊天页（`page=='chat'` 且有输入框）前绝不调 send_greeting**；若 send_greeting 返回 `not_on_chat_page`，就是还没进聊天页，回到上一步先点【继续沟通】。
- **`start_chat` 返回超时/瞬断（`retry:true`/`do_not_skip:true`）≠ 该岗位无按钮**：多半是点击已生效、在跳转。先 `browser_snapshot` 看是否已到聊天页，没到再重试 start_chat ≤2 次；绝不因超时跳过已达标岗位。
- ‼️ **snapshot 一旦见 `page=='chat'` 且有输入框，就必须马上 `send_greeting`**；在发送成功（或 start_chat 重试 2 次仍进不去）前，**绝不 `go_back`/换岗位**（曾因到聊天页后又 go_back 丢了本岗位）。
- `send_greeting` 返回 `send_not_confirmed`：话术已填入但**未确认发出**（输入框未清空）。**别当作已发送、别 go_back**；重试 `send_greeting` 或在页面手动点【发送】，确认发出后再继续。
- `send_greeting` 返回 `already_greeted`/`duplicate_greeting`：该岗位/该话术**已发过**，绝不重发；直接 `go_back` 回列表处理下一个岗位（或 `load_more`）。
- 点【立即沟通】后**必须再 snapshot**：出现确认弹框就 `match_text='继续沟通'` 点进去。
- 聊天态判定：`page=='chat'` 且能看到输入框即可发送；**不必等 URL 变 `/web/geek/chat`**（job_detail 会在当前页弹框）。

## 连接与异常
- `page_not_connected`：页面 ~2s 自动重连，重试中间件会自动重试，别手动反复点。
- `go_back`/`navigate` 只回「即时回执」，返回里没有新页快照 → 需要看新页时再显式 `browser_snapshot`。
- 点【立即沟通】/【继续沟通】后若返回 `navigating:true` 或 `page_action_timeout`：多半是点击已生效、页面正在跳转，
  **别重复点/重试**，直接 `browser_snapshot` 看是否已到聊天页（`page=='chat'`）。
- 【继续沟通】这类确认按钮现在会稳定出现在操作返回里（关键按钮不被去重藏掉）：看到就点它，没看到且已是聊天页就直接发送。
- 登录失效 / 验证码 / 风控弹窗：立即停止并告知用户，绝不尝试填写任何敏感信息。

## 效率
- 操作后用返回的变化量即可，不要为"看一眼"在同一步又 `browser_act` 又 `browser_snapshot`。
- 面对列表顶部岗位前，信任已自动过滤的 `jobs`（过目清单里终态岗位已剔除），别重复评估。

## 筛选设置
- 薪资范围：点击 e22(薪资待遇) → 展开后选择对应区间；或直接在搜索框 type 薪资要求。
- 学历/年限筛选：点击 e24(学历要求) / e23(工作经验) 设置排除条件（全日制/应届等）。
- AI agent 岗位搜索：在输入框 (e17) type "AI Agent" 或 "智能体" + press Enter。
- 成都地区默认已选中，无需额外设置城市筛选。
