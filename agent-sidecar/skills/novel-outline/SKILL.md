---
name: novel-outline
description: 大纲阶段。生成大纲/排故事线/选桥段/一键完整大纲/续写扩写。流程:outline_material_candidates 拿候选 → generate_full_outline 落库。前置:phase=config 且 basic_info 充实。退出:phase=ready。
---

# 大纲阶段（novel-outline）— 侧栏版

## 前置检查（必做）
1. `mcp__novelengine__get_book_detail` 看 `phase`：
   - `config` 且世界观/主角充实 → 可生成。
   - `outlines/plots` → 已有大纲，问用户：重做 / 续写（`extend_outline`）/ 直接写作。
   - `ready` → 已就绪，转写作。
2. 无书 → 先 novel-build。世界观/主角不充实 → 先 novel-build 补设定（已生成设定但未打标可 `mcp__novelengine__confirm_world` 确认，后续大纲跳过 Phase 1 分析）。

## 决策点（选材，任务指令里给偏好；无则走管线内 AI/规则）
1. `mcp__novelengine__outline_material_candidates(book_id)` → `{templates, plots}` 候选池。
2. 若有偏好：`picks["templates"]` = 模板 id 列表（想用的排前）；`picks["plots"]` = 桥段 id **扁平优先序列表**（想先出现的排前）。预选 id 必须来自 candidates。
3. 无偏好 → 不传 picks，走管线内 AI/规则选材。

## 批处理（二选一）
- **一键（推荐）**：`mcp__novelengine__generate_full_outline(book_id, picks={"templates":[...], "plots":[...]})`。阻塞数分钟（6 阶段），完成后 `get_book_detail` 确认 `phase=ready`。
- **分步（无 LLM/逐步确认）**：`generate_outlines(mode="rule")` → `confirm_outlines`（phase→plots）→ `fill_plots` → `fill_gags`（phase→ready）。

## 续写
- 已 ready 想加剧情 → `mcp__novelengine__extend_outline(book_id, mode="ai")`。

## 退出状态
`phase=ready`，`get_book_detail` 可见 outlines 与 plots；`navigate(url="/books/generator")` 切大纲页可视化。

## 失败处置
- picks 里 id 失效 → 管线静默回退 AI（设计如此），向用户说明。
- `BookBusyError` → 稍后重试。分步流程中 `fill_plots` 前必须先 `confirm_outlines`。
- `generate_full_outline` 中途断/超时 → **逐步落盘，可重跑续接**：`get_book_detail` 看已产出的 outlines/plots，缺哪段重跑哪段，不必整本重来。
