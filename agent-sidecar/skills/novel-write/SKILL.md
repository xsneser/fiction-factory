---
name: novel-write
description: 写作阶段。开始写/开写/写正文/写下一章/继续写/写桥段/一键写完整章/写完整章/一键完整章节/完整章节构建。流程:渐进式披露组装上下文 → write_next_bridge 逐桥段推进 → chapter_quality_gate 质量门禁（默认执行，只报告不修复）。前置:phase=ready。
---

# 写作阶段（novel-write）— 侧栏版

## 前置检查（必做）
1. **确认 LLM 已配置**：`api.json` 有 `api_key`（deepseek-v4-flash）——无配置写作会空内容/报错，先提示配置再继续。
2. `mcp__novelengine__get_book_state(book_id)`：`phase != ready` → 先 novel-outline；看 `current_chapter` 与草稿定位续写点。

## 上下文组装（渐进式披露 — 不要把整本书灌进上下文）
1. `get_book_detail(book_id)` → 书元数据/写作风格/基调。
2. `get_storyline(book_id)` → 当前大纲弧、下一个待写桥段。
3. 只取**当前桥段出场角色**信息。
4. 已写最近 1-2 章（接续语气），非全书。
5. 整理成一句「接下来写哪个桥段 + 出场角色 + 风格要求」，交给批处理——逐段写作与自评由管线内部处理。

## 完整章节构建流水线（一键写完整章 / 完整章节构建）
1. **写**：循环 `mcp__novelengine__write_next_bridge(book_id)` 直到 `status=chapter_done`（满字数自动切章）；或 `mcp__novelengine__write_chapter(book_id)` 整章重写。
2. **门禁（默认执行，规则层零成本）**：`mcp__novelengine__chapter_quality_gate(book_id, chapter_num=0)` 一次跑 审查/连续性/追读/伏笔/爽点 五项，返回 `passed`/`complete`/`summary`/`checks.*.passed`/`decision_points`。
3. **汇报（只报告不修复）**：读 `summary` + 各 `checks.*.passed` + `decision_points`；`passed=false` → 逐条列决策点给用户定夺，**不擅自重写正文**（改可用 `review_text`/`deai_text`/`outline_agent`）。

## 批处理（循环 write_next_bridge）
1. 循环 `mcp__novelengine__write_next_bridge(book_id)` 直到章数达标 / `budget_paused` / `complete`。
2. 返回值 `status` 对照：
   - `bridge_written` → 桥段写完未切章，继续下一桥段。
   - `chapter_done` → 本章写完（word_count/review），**跑完整章节构建流水线的门禁**再汇报。
   - `budget_paused` → 预算/额度触发，**停下报告**，不要硬续。
   - `noop` / `complete` → 无待写桥段或全书完成，转 novel-publish。
3. 写第 1-3 章应用 novel-build 的「前三章开篇钩子」规则：首句强钩子、三章内出第一个爽点、章末留钩。

## 按需深挖（门禁之外，规则层零成本）
门禁已覆盖前两项；需更细数据再单独调：`diagnose_retention(book_id, recent_n=5)`（门禁已含精简版）/ `tag_punch_points(book_id, chapter_num=0)`（落盘 tags.json，门禁只读不落盘）/ `review_text(text, target_words)` / `deai_text(text, style)`。

## 退出状态
桥段写完：`bridge_written`；整章写完：`chapter_done` + `word_count` + `review`；`navigate(url="/books/<book_id>/continue")` 切写作台可视化。全书完 → novel-publish。

## 失败处置
- phase 不过 → 引导 novel-outline。`BookBusyError` → 稍后重试。`budget_paused` → 停，报告。
- 写作内容为空/报错 → 检查 api.json 模型配置与 max_tokens 余量（推理型模型需留足）。
- `chapter_quality_gate` 返回 `complete=false`（某单项 skipped）→ 单独报告该项异常，不中断，也不据此认定整章质量失败。
