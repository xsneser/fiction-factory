---
name: novel-build
description: >-
  建书阶段向导。Use when the user wants to 开新书/创建小说/新建一本/构思世界观/写人物设定/借鉴已有书/选题材标签/生成书名/写开头几章
  (start a new novel, build world and characters, borrow from an existing book, pick a title)。
  流程：navigate 建书页 → 一句话设定+笔名 → create_book → 世界观候选 → 用户挑 → generate_world
  → save_basic_info 微调/标签 → confirm_world 锁定。内含前三章开篇钩子规则（指令层）。
  前置：书不存在或 phase=config。不做大纲（那是 novel-outline）。
---
# 建书阶段（novel-build）

## 前置检查（必做，只读工具）
1. `mcp__novel-engine__list_books` 看目标书是否已存在。
2. 已存在 → `mcp__novel-engine__get_book_detail` 看 `phase`：
   - `config` → 继续本 skill（已有基本盘，补设定即可，跳过已完成的步骤）。
   - `outlines/plots/ready` → 已过建书阶段，引导到 `novel-outline` / `novel-write`，不要重复建书。
3. 不存在 → `mcp__novel-engine__navigate`（url=`/books/start`）让用户看到建书页。

## 决策点（必须停下问用户，不要替用户决定）
1. **一句话设定 + 笔名**：问用户想写什么故事（一句话/流派/主角大概），以及笔名。拿不到流派时让用户从「都市/玄幻/悬疑/科幻…」里挑一个。
2. **世界观方向**：三选一，让用户定：
   - 从零构思 → `mcp__novel-engine__world_candidates(book_id, idea=用户一句话)` 出 2-3 个差异化方向 → 把候选贴给用户挑。
   - 用户已有想法 → `generate_world(mode="one", idea=...)`。
   - 借鉴已有书 → `mcp__novel-engine__borrow_preview(source_book_id=...)` 预览会借鉴哪些设定 → 用户确认 → `generate_world(mode="borrow", source_book_id=...)`。
3. **微调设定 / 题材标签**：生成后把世界观摘要+主角摆给用户看，问要不要改（改哪个字段用 `save_basic_info` 只覆盖那一项）；题材标签（`world_building.tags`）让用户确认。

## 批处理
1. `mcp__novel-engine__create_book(title, pen_name, genre, sub_genre, ...)` → 记下返回的 `book_id`。
2. `world_candidates` / `generate_world` 产出 `basic_info`。
3. `mcp__novel-engine__save_basic_info(book_id, basic_info=...)` 落库（深合并，只覆盖要改的字段）。
4. `mcp__novel-engine__confirm_world(book_id)` → 返回 `world_generated=true` 才算锁定过关；`false` 说明 basic_info 不够充实（世界观维度或主角缺失），补设定后重试。

## 前三章开篇钩子（指令层，本 skill 内置）
前三章是吸睛关键，无论是否立即进入写作，都按此规则把握：
- **第一章第一句必须是强钩子**：从冲突/悬念/奇观/反常起手，不要环境描写或背景介绍开场（例：主角被系统判死刑的瞬间，而不是「这是一个修真世界…」）。
- **前三章内给出第一个爽点**：金手指觉醒/打脸/危机反转，让读者在第三章结束前吃到第一次回报。
- **每章章末留钩子**：结尾停在疑问/危机/反转上，制造追读欲。
- **落地检查**：写完章节后用 `mcp__novel-engine__review_text`（章末钩子评分）和 `mcp__novel-engine__diagnose_retention`（掉读风险）核验；不合格 → 引导到 `novel-write` 重写该章。不加新工具，靠现有规则层检查。

## 退出状态
- 成功：`phase=config` + `_world_generated=true`。用 `get_book_detail` 复核（主角名 + 世界观至少 4 个维度非空）。
- 下一步自然衔接：`novel-outline`（生成大纲）→ `novel-write`（写前三章）。

## 失败处置
- 「LLM 未配置」→ 提示到设置页或 `api.json` 配 key 后重试。
- `world_candidates` 空 → 重试一次；仍空改 `generate_world` 一句话直出。
- 已确认过世界观 → 立即 `confirm_world` 打标，避免重复生成。
- `BookBusyError` → 另一进程在操作此书，稍后重试。
