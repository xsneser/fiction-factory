---
name: novel-outline
description: >-
  弧阶段。Use when the user wants to 生成弧/排故事线/选桥段/一键完整弧/续写/扩写/规划剧情
  (plan the storyline, generate outline arcs and plot beats, extend the book)。
  流程：确认世界观/主角 → **agent 自主生成弧 + 桥段**（保持上下文连续）→ save_outlines 落盘
  → fill_gags 挂内涵到 ready。前置：phase=config 且 basic_info 充实。退出：phase=ready。不做正文（那是 novel-write）。
---
# 弧阶段（novel-outline）— agent 自主生成

> **核心原则**：弧/桥段由你（agent）**自主生成**——你带着世界观/主角设定/题材，自己规划故事弧、
> 章节区间、桥段列表，然后调用**薄工具** `save_outlines` 落盘。**不要**调用内部跑 LLM 的旧工具
> （generate_full_outline / generate_outlines mode=ai，已废弃留档）。

## 前置检查（必做）
1. `mcp__novel-engine__get_book_detail` 看 `phase`：
   - `config` 且世界观/主角充实（`confirm_world` 过）→ 可生成。
   - `outlines/plots` → 已有弧，问用户：重做 / 续写 / 直接去写作。
   - `ready` → 已就绪，问续写还是去写作。
2. 无书 → 提示先跑 `novel-build`。设定不充实 → 先跑 `novel-build` 补。

## 上下文组装
1. `get_book_detail(book_id)` → 世界观、主角、基调、目标读者、题材。
2. `get_storyline(book_id)` → 已有弧（续写时读末尾弧）。

## 决策点（选材，让用户参与）
1. `mcp__novel-engine__outline_material_candidates(book_id)` → `{templates, plots}` 候选池。
2. 让用户挑模板/桥段偏好（参考候选；不选则你自主排布）。
3. 确认后进入生成。

## 生成 → 落盘
1. **你自主生成**（你的 LLM 直接产出，上下文连续）：
   - `outlines`：弧列表，每项 `{name, start_chapter, end_chapter, stages:[{name,min_ch,max_ch,events}], predecessor?, successor?, transition_type}`。
   - `plots`：桥段列表，每项 `{name, outline_id, stage_index, order, category, thread_id, resolves_plot_id?, roles?}`。
   - `threads`：叙事线程 `[{id,name,desc}]`；`themes`：内涵 `[str]`。
2. 调用 `mcp__novel-engine__save_outlines(book_id, outlines=..., plots=..., threads=..., themes=..., mode="replace")` 落盘（返回 outlines/plots 计数，phase=plots）。
3. `mcp__novel-engine__fill_gags(book_id)` —— 规则挂内涵/吸睛，phase→ready。
4. `get_book_detail` 确认 `phase=ready`。

## 续写
- 已 ready 想加剧情 → 读 `get_storyline` 末尾弧 → **你自主生成下一弧 + 桥段** → `save_outlines(book_id, outlines=[新弧], mode="append")` → `fill_gags`。

## 退出状态
- `phase=ready`，`get_book_detail` 可见 outlines 与 plots。
- 可 `navigate`（url=`/books/<book_id>`）让用户可视化查看故事线。

## 失败处置
- phase 不过 → 引导对应前置阶段。
- `BookBusyError` → 稍后重试。
- 生成结构不合法（缺 start/end_chapter 等）→ 补全后重调 save_outlines。
- `fill_gags` 前必须先 save_outlines（phase=plots），否则 phase 不对报错。
