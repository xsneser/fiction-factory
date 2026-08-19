---
name: novel-write
description: >-
  写作阶段。Use when the user wants to 写正文/写下一章/继续写/写桥段/把第 N 章写出来/写开头几章
  (write the next bridge or chapter, continue drafting)。
  流程：渐进式披露地组装上下文（书详情→故事线→当前桥段出场角色→最近已写）→ write_next_bridge
  逐桥段推进（内部已逐段思考+有界自评）或 write_chapter 整章重写 → 质量反馈。前置：phase=ready。
---
# 写作阶段（novel-write）

## 前置检查（必做）
1. `mcp__novel-engine__get_book_state(book_id)`：`phase != ready` → 提示先跑 `novel-outline`；
   看 `current_chapter` 与进行中草稿定位续写点。
2. 确认 LLM 已配置（写作走 `api.json` 配置的 deepseek-v4-flash）。

## 上下文组装（渐进式披露 — 不要把整本书灌进上下文）
按需逐层取，只取「接下来要写的桥段」真正需要的信息：
1. `mcp__novel-engine__get_book_detail(book_id)` → 书元数据、写作风格、目标读者、基调。
2. `mcp__novel-engine__get_storyline(book_id)` → 当前所在大纲弧、下一个待写桥段（写什么、在哪一章）。
3. 只取**当前桥段出场角色**的信息（主角/配角名字、性格、当前处境）——不要取全书所有角色。
4. 已写内容：最近 1-2 章（供接续语气与进度），而非全书。
5. 把这些整理成一句「接下来写哪个桥段 + 出场角色 + 风格要求」，交给下面的批处理即可——逐段写作与自评由管线内部处理。

## 决策点
- 逐桥段（默认，粒度细）：`mcp__novel-engine__write_next_bridge(book_id)`。
- 整章重写：`mcp__novel-engine__write_chapter(book_id, chapter_num=N)`（0=下一章）。
- 每章后可问用户：继续写 / `review_text` 审校 / `deai_text` 去 AI 味 / `diagnose_retention` 看掉读。

## 批处理
1. 循环 `write_next_bridge` 直到返回 `status=chapter_done`（满字数自动切章）或用户喊停。
2. 返回值 `status` 对照：
   - `bridge_written` → 桥段写完未切章，继续下一桥段。
   - `chapter_done` → 本章写完（有 word_count/review），可问用户是否审校/继续。
   - `budget_paused` → 预算/额度触发，**停下问用户**，不要硬续。
   - `noop` / `complete` → 无待写桥段或全书完成，转 `novel-publish` 或告知用户。
3. 写第 1-3 章时应用 `novel-build` 里的「前三章开篇钩子」规则（首句强钩子、三章内出第一个爽点、章末留钩）。

## 质量反馈（可选，规则层零成本）
- `mcp__novel-engine__diagnose_retention(book_id, recent_n=5)`：最近 N 章掉读风险 + 章级建议。
- `mcp__novel-engine__tag_punch_points(book_id, chapter_num=0)`：单章爽点标注（打脸/升级/伏笔回收/装逼/甜宠/反转）。
- `mcp__novel-engine__review_text(text, target_words)`：单段/整章审校（字数/AI痕迹/节奏/对话占比/章末钩子）。
- `mcp__novel-engine__deai_text(text, style)`：去 AI 味（词替换+段落节奏）。

## 退出状态
- 桥段写完：`bridge_written` + `draft` 草稿；整章写完：`chapter_done` + `word_count` + `review`。
- 全书写完 → 引导 `novel-publish`（generate_book_meta → publish_check → publish_book）。

## 失败处置
- phase 不过 → 引导 `novel-outline`，勿硬写。
- `BookBusyError` → 稍后重试。`budget_paused` → 停，问用户。
- 写作内容为空/报错 → 检查 `api.json` 模型配置与 max_tokens 余量（推理型模型需留足）。
