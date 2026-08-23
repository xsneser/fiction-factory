---
name: novel-write
description: 写作阶段。开始写/开写/写正文/写下一章/继续写/写桥段/一键写完整章/写完整章/一键完整章节/完整章节构建。流程:渐进式披露组装上下文 → agent 自主生成桥段正文 → save_bridge_draft 落盘 → 章满 save_chapter_text → 规则质检。前置:phase=ready。
---

# 写作阶段（novel-write）— 侧栏版（agent 自主生成）

> 核心：正文由你自主生成（完整上下文连续），生成后调薄工具 `save_bridge_draft`/`save_chapter_text` 落盘。
> 不要调内部跑 LLM 的旧工具（write_next_bridge/write_chapter，已废弃）。

## 前置检查（必做）
1. `mcp__novelengine__get_book_state(book_id)`：`phase != ready` → 先 novel-outline；看 current_chapter 与草稿定位续写点。
2. `mcp__novelengine__get_storyline(book_id)` → 下一个待写桥段（plot_id/名称/章号）。

## 上下文组装
1. `get_book_detail(book_id)` → 风格/基调/目标读者。
2. `get_storyline(book_id)` → 当前桥段 + 出场角色。
3. `get_book_state(book_id)` → 最近 1-2 章（接续语气）。
4. 整理成「写哪个桥段 + 出场角色 + 风格 + 前文语气」，**自己生成正文**。

## 写作规则（生成时内嵌）
一致性铁律（人名/绑定/数值不冲突、呼应伏笔）；视角统一（默认第三人称）；禁 AI 味句式（仿佛/似乎/不禁/只见 堆叠）；前三章首句强钩/三章内出爽点/章末留钩；每章约 words_per_chapter 字、桥段 800-2500 字。

## 生成 → 落盘（逐桥段）
1. 找下一个未写桥段（written_chapter==0）→ **你自主生成正文**。
2. `mcp__novelengine__save_bridge_draft(book_id, chapter_num=N, plot_id=..., plot_name=..., text=...)` 落草稿。
3. 每桥段后自我核查（语气/伏笔/错词），有问题就地重写再落盘。

## 章满收尾 → save_chapter_text
- 字数达标 → 你生成章节摘要（80-150 字），拼全文，调
  `mcp__novelengine__save_chapter_text(book_id, chapter_num=N, text=全文, title=「第N章」, summary=你生成的摘要, bridge_segments=[{plot_id,plot_name,text},...])`
  （它负责规则去AI/审查/角色状态/承诺台账/进度并落盘，无 LLM）。

## 完整章节构建流水线
1. 写：逐桥段「生成 → save_bridge_draft」→ save_chapter_text。
2. 门禁：`mcp__novelengine__chapter_quality_gate(book_id)` 跑五项。
3. 汇报：`summary` + `checks.*.passed` + `decision_points`；passed=false → 逐条列给用户，只报告不修复。

## 退出状态
桥段写完：save_bridge_draft 落草稿；整章写完：save_chapter_text 返回 chapter/word_count/review。全书完 → novel-publish。

## 失败处置
phase 不过 → 引导 novel-outline。BookBusyError → 稍后重试。内容为空/报错 → 检查 api.json 与 max_tokens。save_chapter_text 失败 → 草稿还在（save_bridge_draft 已落），检查后重试。
