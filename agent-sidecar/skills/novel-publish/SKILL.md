---
name: novel-publish
description: 上架阶段。上架/发布/完本/导出投稿包/生成书名+简介/检查能否发书。流程:generate_book_meta → publish_check → 决策 → publish_book/mark_finished/export_book。前置:已有第 1 章正文。
---

# 上架阶段（novel-publish）— dsh 侧车版

## 前置检查（必做）
1. `mcp__novelengine__get_book_detail(book_id)`：看 `status`、`current_chapter`、`synopsis`。
2. 无第 1 章 → 先 novel-write。有第 1 章但无 synopsis/书名不佳 → 先 `generate_book_meta`。

## 决策点
- `publish_check` 报告：全部通过 → 直接 `publish_book`；有不过项 → **报告问题让用户决策**（force 强发 or 先修），headless 不擅自 force。
- **可选打磨**（上架前质量提升，规则层零成本）：`review_text`（章节规则审查）/ `deai_text`（去 AI 味）/ `diagnose_retention`（掉读诊断）——用户要求打磨时先跑，再 `publish_check`。
- 完本 → `mark_finished`；导出投稿包 → `export_book`。

## 批处理
1. `mcp__novelengine__generate_book_meta(book_id)`（缺 synopsis 时）。
2. `mcp__novelengine__publish_check(book_id)` → 5 项规则报告（书名/简介/字数/审查/完本）。
3. 决策后：`publish_book(book_id, force=False)` / `mark_finished(book_id)` / `export_book(book_id)`。

## 退出状态
`published` / `finished` / manifest（zip_path）；`navigate(url="/publish")` 切上架总览可视化。

## 失败处置
- 检查未过且未 force → 逐项列问题。`publish_book` 未过检查又没 force → 会报错，需 force 或先修。
- `BookBusyError` → 稍后重试。
