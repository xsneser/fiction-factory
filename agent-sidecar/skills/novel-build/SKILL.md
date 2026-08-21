---
name: novel-build
description: 建书步 2（挑完候选后的建书）：用户在步 2 已选定世界观候选并点了「已挑选完毕」，本 skill 从步 3 开始**分阶段**构建世界观（core_conflict→factions→characters→rest_world，每段带前面上下文）→ submit 建书 → 校验 → 生成完整大纲。护栏：create_book/delete_book 不在 MCP 面。不做候选生成（那是 novel-build-candidates）。
---

# 建书步 2：补全世界观并建书（novel-build）— dsh 侧车版

> **护栏**：`create_book` / `delete_book` 是 web-only，绝不用——本 skill 只能 `drive_ui` 驱动浏览器向导建书（机制见 CLAUDE.md）。
> **headless 是一次性任务**：写作方向/笔名/种子/标签/候选等决策由任务指令给全（候选已在步 2 选定），不做交互问答。
> **触发**：用户在步 2 点「已挑选完毕」后，页面自动把本任务发给 agent（也可在侧栏继续说「继续建书」）。
> **硬规则**：本 skill **不再生成候选**——绝不 `world_candidates` / `set_candidates` / `pick_candidate` / 步 1→2 的 `next`；候选已由用户在步 2 选定，直接从步 3 开始。
> **drive_ui 是两参工具**：必须同时传 `cmd` 与 `args`（如 `drive_ui(cmd="set_world", args={world_building:{core_conflict:"…"}})`），`cmd` 必填不可省——漏传会报 `cmd Field required`。
> **无前置检查**：task2 触发时页面已保证 `_picked=true`、向导在步 3、书必不存在——**不要** `get_build_status` / `list_books`。

## 步 3 = 内容构建工作台（分阶段，agent 自主驱动，不询问）
浏览器不再自动一键补全（`show(3)` 的自动补全已被页面抑制，等 agent 分阶段填）。按顺序逐段构建，每段经 `drive_ui` 落进表单：
- ① **核心矛盾**：`mcp__novelengine__generate_core_conflict(idea=世界观简述, world_brief=候选简述, tags, pen_name)` → `drive_ui(set_world, {world_building:{core_conflict:"..."}})`（返回 genre 供后续）。
- ② **开篇大纲+桥段（省略）**：不 `query_structures` / `query_plots` / `set_picks`——`generate_full_outline` 会自动选材；如用户后续要自定义大纲材料，走 novel-outline 或 `drive_ui(set_picks)` 补充。
- ③ **势力**：`mcp__novelengine__generate_factions(idea, world_brief, core_conflict=①, tags, genre)` → `drive_ui(set_world, {world_building:{factions:[...]}})`。
- ④ **主要人物**：`mcp__novelengine__generate_characters(idea, title=候选标题, tags, genre, core_conflict=①, factions=③)`（**不 `query_characters`**——不传 archetype_ids 时按 tags→genre→原型自动回退）→ `drive_ui(set_characters, {characters:[全 14 字段列表]})`——`name/identity/personality/catchphrase/importance/golden_finger/relation/archetype_id/gender/brief/title/age/death_year/role`（主角 importance=1、配角补 relation；gender/brief/title/age/death_year 原样透传，详情页可编辑）。
- ⑤ **其余世界观维度**：`mcp__novelengine__generate_rest_world(idea, world_brief, core_conflict=①, factions=③, tags, genre, pen_name)` → `drive_ui(set_world, {world_building:{era,power_system,geography,culture,history,social_structure,rules,world_summary}, tone, target_audience, pov, era_language})`。
- 失败/跳过：任一段失败重试一次，仍失败跳过该段继续（已填内容保留、部分构建可提交）；⑤ 未做则 `_world_generated` 不置位，submit 后 `generate_full_outline` Phase 1 自动补齐剩余维度。

## submit
无需等一键补全，随时 `drive_ui(submit)` → **系统** `POST /books/start` 建书（phase=config）——各段内容已随 submit 落库，浏览器跳书详情。

## submit 后：校验 + 生成完整大纲
1. **`mcp__novelengine__get_build_status()`** 拿 `book_id`（浏览器建书成功会把 `bookId` 回写到 `storage/build_status.json`；`created=true` 才算建成，未建成先等片刻再查，仍无 → 重试 `drive_ui(submit)` 或如实汇报）。
2. `get_book_detail(book_id)` 看世界观充实度（步 3 已落库，通常充实；单薄才 `generate_world(book_id, mode="one", idea=...)` 兜底）。
3. **保真度校验（必做）**：核对 `genre` / `sub_genre` / `tags` 与任务设定一致；漂移 → `navigate(url="/books/start")` + `drive_ui(reset)` + 重填 set_field/set_tags 后重新走建书流程（最多重试 1 次，仍漂移则如实汇报停止）。
4. **必须调** `mcp__novelengine__generate_full_outline(book_id)`（阻塞数分钟，逐步落盘，**内部自动选材**——本 skill 未设 `_outline_picks`；世界观充实自动跳过 Phase 1 故事分析）。
5. `get_book_detail` 确认 `phase=="ready"` → `navigate(url="/books/<book_id>/continue")` 交棒写作台写前三章。

## 删书（护栏：外部 agent 无 delete_book）
- 用户要求删书 → `navigate(url="/books")` + 告知「请在书库页点该书旁的删除按钮（有确认弹窗）」。agent 不做删除动作。

## 前三章开篇钩子（指令层，本 skill 内置）
前三章是吸睛关键，进入写作台后按此规则把握：
- **第一章第一句必须是强钩子**：从冲突/悬念/奇观/反常起手，不要环境描写或背景介绍开场（例：主角被系统判死刑的瞬间，而不是「这是一个修真世界…」）。
- **前三章内给出第一个爽点**：金手指觉醒/打脸/危机反转，让读者在第三章结束前吃到第一次回报。
- **每章章末留钩子**：结尾停在疑问/危机/反转上，制造追读欲。
- **落地检查**：写完章节后用 `mcp__novelengine__review_text`（章末钩子评分）和 `mcp__novelengine__diagnose_retention`（掉读风险）核验；不合格 → 引导到 novel-write 重写该章。不加新工具，靠现有规则层检查。

## 退出状态
成功：世界观 + 大纲完成，`phase=ready`，浏览器停在写作台。下一步：novel-write（写前三章）。

## 失败处置
- `drive_ui` 后浏览器没反应 → `navigate(url="/books/start")` 再试。
- 向导卡步 4/5 → `get_book_detail` 确认书已建、phase 到哪，若已 ready 直接 navigate 写作台。
- `BookBusyError` → 稍后重试。`generate_full_outline` 中途断 → 逐步落盘可重跑续接。
