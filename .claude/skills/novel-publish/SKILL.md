---
name: novel-publish
description: >-
  上架阶段。Use when the user wants to 上架/发布/完本/导出投稿包/生成书名+简介/检查能否发书
  (publish, finish, export, generate title+synopsis, pre-publish check)。
  流程：补元数据（generate_book_meta，需第 1 章）→ publish_check 报告 → 用户决策
  → publish_book / mark_finished / export_book。前置：已有第 1 章正文。
---
# 上架阶段（novel-publish）

## 前置检查（必做）
1. `mcp__novel-engine__get_book_detail(book_id)`：看 `status`、`current_chapter`、`synopsis`（简介）。
2. 无第 1 章正文 → 提示先跑 `novel-write`。
3. 有第 1 章但无 synopsis / 书名不佳 → 先 `generate_book_meta`。

## 决策点
- `publish_check` 报告出来后：全部通过 → 直接 `publish_book`；有不过项 → **问用户**「force 强发 or 先修问题」。
- 用户想完本 → `mark_finished`；想导出投稿包 → `export_book`。
- 可选打磨：`review_text` 审单章、`deai_text` 去 AI 味、`diagnose_retention` 看追读。

## 批处理
1. `mcp__novel-engine__generate_book_meta(book_id)`（缺 synopsis 时）：基于第 1 章生成书名+简介，落 `book.json` / `storyline.json` / `outline.json`。
2. `mcp__novel-engine__publish_check(book_id)` → 5 项规则报告（书名/简介/字数/审查/完本）。
3. 决策后：
   - `mcp__novel-engine__publish_book(book_id, force=False)` → 通过检查直接上架；未过需 `force=True` 或先修。
   - `mcp__novel-engine__mark_finished(book_id)` → 完本标记。
   - `mcp__novel-engine__export_book(book_id)` → 逐章 txt + 合集 + zip，返回 manifest（含 zip_path）。

## 退出状态
- `publish_book` → `status=published`；`mark_finished` → `status=finished`；`export_book` → manifest（zip_path）。
- 可 `navigate`（url=`/publish`）让用户看到上架页结果。

## 失败处置
- 检查未过且未 force → 逐项列问题让用户先修（缺简介→`generate_book_meta`；字数不足→`novel-write` 续写）。
- `publish_book` 未过检查又没 force → 会报错，需 `force=True` 或先修完再发。
- `BookBusyError` → 稍后重试。
