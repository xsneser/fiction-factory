---
name: novel-build
description: 建书阶段。开新书/写设定/构思世界观。建书必须走系统向导 UI(drive_ui),护栏:create_book/delete_book 不在 MCP 面。前置:书不存在或 phase=config。
---

# 建书阶段（novel-build）— dsh 侧车版

> **护栏（必须遵守）**：`create_book` / `delete_book` 是 web-only，不在 MCP 面——本 skill 绝不调用它们。
> 建书只能驱动系统向导（`drive_ui` 写意图队列，浏览器 `/books/start` 轮询消费后由系统 `POST /books/start` 创建）。

## 前置检查（只读）
1. `mcp__novelengine__list_books` 看目标书是否存在。
2. 已存在 → `mcp__novelengine__get_book_detail` 看 `phase`：`config` → 补设定即可；`outlines/plots/ready` → 已过建书，转 novel-outline / novel-write。
3. 不存在 → `mcp__novelengine__navigate(url="/books/start")`（浏览器切到向导页）。

## 决策点
- 写作方向/笔名/一句话种子/题材标签：来自用户的任务指令（headless 无交互问答，任务里给全）。
- **世界观候选（submit 硬前置）**：headless 用**路线 B**——`mcp__novelengine__world_candidates(book_id="", idea=种子, genre=方向, tags=标签)` 取候选 → `drive_ui(pick_candidate, {idx, candidate:{title, world_brief, one_liner}})` 注入（候选内嵌，不依赖浏览器点选）。

## 批处理（驱动向导 UI，由系统建书）
1. `navigate(url="/books/start")`。
2. `drive_ui(set_field, {field:"idea", value:种子})` + `drive_ui(set_field, {field:"pen", value:笔名})` + `drive_ui(set_tags, {tags:[题材标签]})`（同批推送，浏览器按序应用）。
3. **世界观候选**：`world_candidates` → `drive_ui(pick_candidate, {...})` → `drive_ui(next)`（步 2「已挑选完毕」进步 3）。
4. **步 3 世界观补全自动**（浏览器 `world-complete` 补 12 维 + 基调，约 30-60s）；必要时 `drive_ui(fill_world)` 重触发。
5. **角色**：`query_characters` → `generate_characters(idea, title, tags, genre, archetype_ids)` → `drive_ui(set_characters, {characters:[全 14 字段列表]})`（name/identity/personality/catchphrase/importance/golden_finger/relation/archetype_id/gender/brief/title/age/death_year/role）。
6. **等世界观补全完成再 submit**（进入步 3 后等待约 45-60s）→ `drive_ui(submit)` → 系统建书（phase=config）。

## submit 后：生成完整大纲
1. `list_books` 定位新书 `book_id` → `get_book_detail(book_id)` 看世界观充实度（步 3 已落库，通常充实；单薄才 `generate_world(book_id, mode="one", idea=...)` 兜底）。
2. **必须调** `mcp__novelengine__generate_full_outline(book_id)`（阻塞数分钟，逐步落盘，向导第 4 步 Gantt 轮询填充）。
3. `get_book_detail` 确认 `phase=="ready"` → `navigate(url="/books/<book_id>/continue")` 交棒写作。

## 退出状态
成功：世界观 + 大纲完成，`phase=ready`。下一步：novel-write（写前三章）。

## 失败处置
- `drive_ui` 后浏览器没反应 → `navigate(url="/books/start")` 再试。
- 向导卡步 4/5 → `get_book_detail` 确认书已建、phase 到哪，若已 ready 直接 navigate 写作台。
- `BookBusyError` → 稍后重试。
