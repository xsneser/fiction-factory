---
name: novel-build
description: 建书步 2（挑完候选后的建书）：用户在步 2 已选定世界观候选并点了「已挑选完毕」，本 skill 从步 3 开始**分阶段**构建（core_conflict→大纲+桥段→factions→characters→rest_world，每段带前面上下文）→ submit 建书（书创建即 phase=ready）→ 校验。护栏：建书只能 drive_ui 驱动向导、删书只能书库页手动。不做候选生成（那是 novel-build-candidates）。
---

# 建书步 2：补全世界观并建书（novel-build）— 侧栏版

> **护栏**：建书只能 `drive_ui` 驱动浏览器向导，删书只能 navigate 书库页手动——直建/直删工具不在工具面（见 CLAUDE.md）。
> **headless 是一次性任务**：写作方向/笔名/种子/标签/候选等决策由任务指令给全（候选已在步 2 选定），不做交互问答。
> **触发**：用户在步 2 点「已挑选完毕」后，页面自动把本任务发给 agent（也可在侧栏继续说「继续建书」）。
> **硬规则**：本 skill **不再生成候选**——绝不 `world_candidates` / `set_candidates` / `pick_candidate` / 步 1→2 的 `next`；候选已由用户在步 2 选定，直接从步 3 开始。
> **drive_ui 是两参工具**：必须同时传 `cmd` 与 `args`（如 `drive_ui(cmd="set_world", args={world_building:{core_conflict:"…"}})`），`cmd` 必填不可省——漏传会报 `cmd Field required`。
> **无前置检查**：task2 触发时页面已保证 `_picked=true`、向导在步 3、书必不存在——**不要** `get_build_status` / `list_books`。

## 步 3 = 内容构建工作台（agent 自主生成 → drive_ui 填入，不询问）
浏览器不再自动一键补全（`show(3)` 的自动补全已被页面抑制，等 agent 分阶段填）。按顺序逐段构建，每段由**你在自己上下文自主生成**后经 `drive_ui` 落进表单：
- ① **核心矛盾**：你基于一句话设定 + 候选简述 + tags，自主生成 1-2 句核心矛盾 → `drive_ui(set_world, {world_building:{core_conflict:"..."}})`。
- ② **大纲+桥段（步3内先生成）**：你自主生成完整大纲+桥段 `{outlines, plots, threads, themes, basic_info}` → `drive_ui(set_outline, {outlines, plots, threads, themes, basic_info})`（**你在自身上下文生成，不受 8KB 裁剪**；submit 随书落库 phase=ready）。**不再省略/不 set_picks**。
- ③ **势力**（根据②桥段分析）：你自主生成 2-4 个势力 `{name, stance, desc}` → `drive_ui(set_world, {world_building:{factions:[...]}})`。
- ④ **主要人物**（依据②+③）：你自主生成角色列表（**全 14 字段**：`name/identity/personality/catchphrase/importance/golden_finger/relation/archetype_id/gender/brief/title/age/death_year/role`，主角 importance=1、配角补 relation）→ `drive_ui(set_characters, {characters:[...]})`。
- ⑤ **其余世界观维度**：你自主生成 `{world_building:{era,power_system,geography,culture,history,social_structure,rules,world_summary}, tone, target_audience, pov, era_language}` → `drive_ui(set_world, {...})`。
- 失败/跳过：任一段生成失败重试一次，仍失败跳过该段继续（已填内容保留、部分构建可提交）；② 失败 → submit 后走 novel-outline 用 `save_outlines` 补大纲；⑤ 未做则 `_world_generated` 不置位。

## submit
无需等一键补全，随时 `drive_ui(submit)` → **系统** `POST /books/start` 建书——各段内容 + ②生成的大纲+桥段（`_outline_data`）已随 submit 落库，**书创建即 phase=ready**，浏览器跳书详情。

## submit 后：校验 + 交棒写作台
1. **`mcp__novelengine__get_build_status()`** 拿 `book_id`（浏览器建书成功会把 `bookId` 回写到 `storage/build_status.json`；`created=true` 才算建成，未建成先等片刻再查，仍无 → 重试 `drive_ui(submit)` 或如实汇报）。
2. `get_book_detail(book_id)` 看世界观充实度（步 3 已落库，通常充实；单薄才你自主生成 → `save_basic_info` 兜底）。
3. **保真度校验（必做）**：核对 `tags` 与任务设定一致（无 genre 概念，只核 tags）；漂移 → `navigate(url="/books/start")` + `drive_ui(reset)` + 重填 set_field/set_tags 后重新走建书流程（最多重试 1 次，仍漂移则如实汇报停止）。
4. `get_book_detail` 确认 `phase=="ready"` 且 `outlines`/`plots` 非空（② 失败时此处走 novel-outline `save_outlines` 补生成）。
5. `navigate(url="/books/<book_id>/continue")` 交棒写作台写前三章。

## 删书（护栏：外部 agent 不能直删）
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
- `BookBusyError` → 稍后重试。`save_outlines` 中途失败 → 补全重调。
