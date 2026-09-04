---
name: novel-publish
description: 上架阶段。上架/发布/完本/导出/生成书名简介/检查能否发书。流程:自主生成书名+简介 save_book_meta(需第1章) → publish_check 报告 → 用户决策 → publish_book/mark_finished/export_book。前置:已有第1章正文。
---

# 上架（novel-publish）

> 前置：已有第 1 章正文（由写作阶段 `novel-story` 完成至少一章）。

## 流程
1. **生成元数据**：自主生成书名+简介 → `save_book_meta(book_id, title, synopsis)`（需第 1 章；书名写 book.title + storyline.book_title，简介写 outline.json 的 synopsis）。
2. **上架检查**：`publish_check(book_id)` → 5 项免费规则报告（书名/简介/字数/审查/完本）。
3. **用户决策**：报告问题列给用户，由用户定夺；通过后 → `publish_book(book_id)`（未过上架检查需 `force=True`）/ `mark_finished(book_id)` / `export_book(book_id)`（导出投稿包，逐章 txt + 合集 + zip）。

## 退出状态
- 已上架（`published`）/ 完本（`finished`）/ 导出投稿包（`export_book` 返回 manifest 含 zip_path）。

## 失败处置
- `publish_check` 未过 → 列明缺项，补齐（书名/简介/字数/审查/完本）后再试。
- `publish_book` 被拦 → 明确告知用户，需 `force` 时由用户确认。
