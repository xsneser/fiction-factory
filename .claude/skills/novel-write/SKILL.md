---
name: novel-write
description: >-
  写作阶段。Use when the user wants to 写正文/写下一章/继续写/写桥段/把第 N 章写出来/写开头几章/
  一键写完整章/写完整章/一键完整章节/完整章节构建
  (write the next bridge or chapter, build a complete chapter in one shot)。
  流程：渐进式披露地组装上下文（书详情→故事线→当前桥段出场角色→最近已写）→ write_next_bridge
  逐桥段推进（内部已逐段思考+有界自评）或 write_chapter 整章重写 → chapter_quality_gate 质量门禁
  （默认执行，只报告不修复，问题作决策点）→ 汇报。前置：phase=ready。
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

## 完整章节构建流水线（一键写完整章 / 完整章节构建）
把「写 → 门禁 → 汇报」串成一次完整交付，质量门禁**默认执行**：
1. **写**：循环 `mcp__novel-engine__write_next_bridge(book_id)` 直到 `status=chapter_done`（满字数自动切章）；
   或按决策点选 `mcp__novel-engine__write_chapter(book_id, chapter_num=N)` 整章重写。
2. **门禁（默认执行，规则层零成本）**：`mcp__novel-engine__chapter_quality_gate(book_id, chapter_num=0)`
   ——一次跑 审查/连续性/追读/伏笔/爽点 五项，返回统一门禁报告（`passed`/`complete`/`summary`/
   `checks.<name>.passed`/`decision_points`）。
3. **汇报（只报告不修复）**：把 `summary` + 各项 `checks.*.passed` + `decision_points` 汇报给用户。
   - `passed=true` → 汇报字数/审查评分/爽点数，问是否继续下一章。
   - `passed=false` → 逐条列出 `decision_points`（每项=决策点，含 check/severity/描述/建议），
     等用户拍板再改（改可用 `review_text`/`deai_text`/`outline_agent`），**不擅自重写正文**。

## 决策点
- 逐桥段（默认，粒度细）：`mcp__novel-engine__write_next_bridge(book_id)`。
- 整章重写：`mcp__novel-engine__write_chapter(book_id, chapter_num=N)`（0=下一章）。
- 门禁 `passed=false` 时：把 `decision_points` 列给用户定夺，勿自作主张改文。

## 批处理
1. 循环 `write_next_bridge` 直到返回 `status=chapter_done`（满字数自动切章）或用户喊停。
2. 返回值 `status` 对照：
   - `bridge_written` → 桥段写完未切章，继续下一桥段。
   - `chapter_done` → 本章写完（有 word_count/review），**跑完整章节构建流水线的门禁**再汇报。
   - `budget_paused` → 预算/额度触发，**停下问用户**，不要硬续。
   - `noop` / `complete` → 无待写桥段或全书完成，转 `novel-publish` 或告知用户。
3. 写第 1-3 章时应用 `novel-build` 里的「前三章开篇钩子」规则（首句强钩子、三章内出第一个爽点、章末留钩）。

## 按需深挖（门禁之外，规则层零成本）
门禁已覆盖以下前两项；需更细数据时再单独调：
- `mcp__novel-engine__diagnose_retention(book_id, recent_n=5)`：最近 N 章掉读风险 + 章级建议（门禁已含精简版）。
- `mcp__novel-engine__tag_punch_points(book_id, chapter_num=0)`：爽点标注**并落盘 tags.json**（门禁只读不落盘）。
- `mcp__novel-engine__review_text(text, target_words)`：单段/整章审校（门禁已含 review 精简版）。
- `mcp__novel-engine__deai_text(text, style)`：去 AI 味（词替换+段落节奏）。

## 退出状态
- 桥段写完：`bridge_written` + `draft` 草稿；整章写完：`chapter_done` + `word_count` + `review`。
- 全书写完 → 引导 `novel-publish`（generate_book_meta → publish_check → publish_book）。

## 失败处置
- phase 不过 → 引导 `novel-outline`，勿硬写。
- `BookBusyError` → 稍后重试。`budget_paused` → 停，问用户。
- 写作内容为空/报错 → 检查 `api.json` 模型配置与 max_tokens 余量（推理型模型需留足）。
- `chapter_quality_gate` 返回 `complete=false`（某单项 skipped）→ 单独报告该项异常，不中断，
  也不据此认定整章质量失败。
