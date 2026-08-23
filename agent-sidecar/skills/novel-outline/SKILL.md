---
name: novel-outline
description: 大纲阶段。生成大纲/排故事线/选桥段/一键完整大纲/续写/扩写。流程:确认世界观 → agent 自主生成大纲+桥段 → save_outlines 落盘 → fill_gags 到 ready。前置:phase=config 且 basic_info 充实。退出:phase=ready。
---

# 大纲阶段（novel-outline）— 侧栏版（agent 自主生成）

> 核心：大纲/桥段由你自主生成（上下文连续），调薄工具 `save_outlines` 落盘。不要调旧 LLM 工具
> （generate_full_outline/generate_outlines ai，已废弃）。

## 前置检查
1. `mcp__novelengine__get_book_detail` 看 phase：config 且设定充实 → 可生成；outlines/plots → 问重做/续写/去写作；ready → 续写或去写作。
2. 无书/设定不充实 → 先 novel-build。

## 上下文组装
`get_book_detail` → 世界观/主角/基调；`get_storyline` → 已有大纲（续写读末尾弧）。

## 决策点
`outline_material_candidates` 拿候选 → 让用户挑模板/桥段偏好（不选则你自主排布）。

## 生成 → 落盘
1. **你自主生成**：`outlines`（{name,start_chapter,end_chapter,stages,predecessor,successor,transition_type}）、`plots`（{name,outline_id,stage_index,order,category,thread_id,resolves_plot_id?,roles?}）、`threads`、`themes`。
2. `mcp__novelengine__save_outlines(book_id, outlines=..., plots=..., threads=..., themes=..., mode="replace")` 落盘（phase=plots）。
3. `mcp__novelengine__fill_gags(book_id)` 规则挂内涵 → phase=ready。
4. `get_book_detail` 确认 ready。

## 续写
读 get_storyline 末尾弧 → 你自主生成下一弧+桥段 → `save_outlines(mode="append")` → fill_gags。

## 退出状态
phase=ready；`navigate(/books/<id>)` 可视化。

## 失败处置
phase 不过 → 引导前置。BookBusyError → 稍后重试。结构不合法 → 补全后重调。fill_gags 前先 save_outlines。
