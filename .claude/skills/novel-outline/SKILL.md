---
name: novel-outline
description: >-
  大纲阶段。Use when the user wants to 生成大纲/排故事线/选桥段/一键完整大纲/续写/扩写/规划剧情
  (plan the storyline, pick structure templates and plot beats, extend the book)。
  流程：确认世界观/主角 → outline_material_candidates 拿候选 → 用户选模板/桥段（picks）→
  generate_full_outline 一键落库（或分步 generate_outlines → confirm_outlines → fill_plots → fill_gags）。
  前置：phase=config 且 basic_info 充实。退出：phase=ready。不做正文（那是 novel-write）。
---
# 大纲阶段（novel-outline）

## 前置检查（必做）
1. `mcp__novel-engine__get_book_detail` 看 `phase`：
   - `config` 且世界观/主角充实（确认过 `confirm_world`）→ 可生成。
   - `outlines/plots` → 已有大纲，问用户：重做 / 续写（`extend_outline`） / 直接去写作。
   - `ready` → 已就绪，问续写还是去写作。
2. 无书 → 提示先跑 `novel-build`。世界观/主角不充实 → 提示先跑 `novel-build` 补设定。

## 决策点（选材，必须让用户参与）
1. `mcp__novel-engine__outline_material_candidates(book_id)` → 返回 `{templates, plots}` 候选池。
2. 让用户挑：
   - **模板**：候选 `templates` 里的 `id` 列表 → `picks["templates"]`（想用的排前，可选 1-N 个）。
   - **桥段**：候选 `plots` 里的 `id` **扁平优先序列表**（想先出现的排前）→ `picks["plots"]`。
     注意：预选 id 必须来自 candidates；失效 id 会被管线静默忽略（见失败处置）。
   - 用户不选 → 不传 picks，走管线内 AI/规则选材。
3. 确认后进批处理。

## 批处理（二选一）
- **一键（推荐，LLM）**：`mcp__novel-engine__generate_full_outline(book_id, picks={"templates": [...], "plots": [...]})`。
  阻塞数分钟（6 阶段：分析→大纲→桥段→内涵→吸睛→一致性），完成后 `get_book_detail` 确认 `phase=ready`。
- **分步（无 LLM 或想逐步确认）**：`generate_outlines(mode="rule")` → `confirm_outlines`（phase→plots）→ `fill_plots` → `fill_gags`（phase 到 ready）。

## 续写
- 已 ready 想加剧情 → `mcp__novel-engine__extend_outline(book_id, mode="ai")`（末尾追加新弧+桥段，bump 章节总数）。

## 退出状态
- `phase=ready`，`get_book_detail` 可见 outlines 与 plots。
- 可 `navigate`（url=`/books/generator` 或 `/books/<book_id>`）让用户可视化查看故事线。

## 失败处置
- picks 里的 id 失效 → 管线静默回退 AI（设计如此），但向用户说明哪些 id 未命中、用了默认选材。
- `BookBusyError` → 另一进程在操作，稍后重试。
- 分步流程中 `fill_plots` 前必须先 `confirm_outlines`，否则 phase 不对会报错。
- `generate_full_outline` 中途断 → 因逐步落盘已保存已产出部分，可重跑续接。
