---
name: novel-build
description: >-
  建书阶段向导。Use when the user wants to 开新书/创建小说/新建一本/构思世界观/写人物设定/借鉴已有书/选题材标签/写开头几章
  (start a new novel, build world and characters, borrow from an existing book, pick a title)。
  建书必须走「启动新书」界面：navigate /books/start → drive_ui 填表单 → 点下一步 → 由系统创建（护栏：
  agent 不直建书，只能驱动向导）。内含前三章开篇钩子规则（指令层）。
  前置：书不存在或 phase=config。步 3 内生成大纲+桥段，书创建即 phase=ready。
---
# 建书阶段（novel-build）

> **护栏**：建书只能驱动系统向导 UI，删书只能 navigate 书库让用户手动删——直建/直删工具不在工具面，本 skill 绝不绕向导。

## 前置检查（必做，只读工具）
1. `mcp__novel-engine__list_books` 看目标书是否已存在。
2. 已存在 → `mcp__novel-engine__get_book_detail` 看 `phase`：
   - `config` → 继续本 skill（已有基本盘，补设定即可，跳过已完成的步骤）。
   - `outlines/plots/ready` → 已过建书阶段，引导到 `novel-outline` / `novel-write`，不要重复建书。
3. 不存在 → `mcp__novel-engine__navigate(url="/books/start")` 把浏览器切到建书向导页。

## 决策点（必须停下问用户；选择类问题务必给编号选项，不要开放式空问）

> 选择类提问优先用 **AskUserQuestion 工具**（Claude Code 渲染成按钮/编号选项）；该工具不可用时，文字列编号选项等用户回数字。

1. **写作方向（编号菜单）**：先列方向让用户挑，再补一句核心冲突/主角：
   1. 都市爽文 — 重生/签到/神医/校花/首富/打脸（genre=都市）
   2. 玄幻升级 — 修仙/异界/洪荒/武侠/诸天/无敌流（genre=玄幻）
   3. 科幻脑洞 — 星际/无限流/快穿/游戏/电竞（genre=科幻）
   4. 悬疑烧脑 — 悬疑/权谋/复仇/悬念（genre=悬疑）
   5. 言情甜宠 — 甜宠/双强/团宠/白月光/追妻火葬场（genre=言情）
   6. 历史权谋 — 历史/权谋/军婚（genre=历史）
   7. 自由发挥 — 用户直接给一句话设定
   流派不用让用户纠结——平台流派由题材标签推导，向导建书时据此填 genre。
2. **笔名（先查现成档案，给编号选项）**：
   - 先 `mcp__novel-engine__query_profiles` 拉现有笔名档案（预设 枫落/夜雨/青衫 + 用户自建），把 `pen_name` + 风格摘要 + **平台注册状态**列成编号选项让用户挑（`registered_platforms` 已注册平台；新书默认平台 fanqie，顺带标注该笔名是否已注册番茄）。
   - 档案里没有合意的 → 让用户报一个新笔名（drive_ui set_field pen 会自动补 option）。
   - 用户说「你定」→ 按写作方向挑最匹配的现有笔名。
   - **不要一上来开放式问「用什么笔名」**——现有档案要先摆出来。
   - 选中笔名若在目标平台未登记账号 → **软提醒**（「该笔名未登记番茄账号，正式上架前需注册」，不阻止建书）。
3. **一句话种子设定**：问主角核心卖点 / 一句话设定（如「都市爽文，2050 太阳熄灭」）——它决定世界观候选方向。

## 世界观候选（submit 的硬前置——没让用户挑候选前，不得 submit）

> **硬规则：候选挑选必须完成并经用户确认，否则不得 `drive_ui(submit)`。** 这是用户明确要求的决策点，不可跳过。
> **交棒**：用户在步 2 点「已挑选完毕」后，**页面会自动给 agent（dsh）发建书任务（`novel-build`）驱动步 3**。Claude Code 只负责生成候选、呈现、等待挑选并确认，**不重复驱动步 3**（双驱动会互相覆盖）。

**推荐路线——agent 逐张生成候选、用户点选**：
1. **循环 `mcp__novel-engine__world_candidates(book_id="", idea=种子, genre=方向genre, tags=题材标签)` 约 5 次**（每次 LLM 只生成 **1 个**候选并**自动填入**步 2，工具按 idea/tags 持久化去重；卡逐张出现）——不需要 5 张可提前停。
2. 单次失败/空 → 重试一次；仍空跳过继续。
3. **告诉用户在平台上点选喜欢的候选方向**（agent 不要把候选搬到聊天里——平台卡片点选会自然带入书名/世界观简述到步 3；点卡仅高亮、可换）。
4. 用户确认点选后点「已挑选完毕」（步 2 按钮）→ 进步 3，页面自动触发建书任务交给 dsh；不想要候选 → `drive_ui(skip_candidates)`（「跳过，手动设定」在步 2 导航区，手动点不触发 agent 建书）。

**兜底**：`world_candidates` 失败/空 → 重试一次；仍空 → `drive_ui(skip_candidates)` + `drive_ui(set_field world_desc=手动拼好的世界观简述)`（步 3 自动补全仍会触发）——**skip 也要向用户说明**，不能无声跳过。

## 批处理（驱动向导 UI；agent 只填表单/点按钮，由系统建书）
1. `navigate(url="/books/start")`。
1.5. **`drive_ui(reset)`**：每次建书前先重置向导 state（除笔名），清除上一本残留草稿对 set_field/set_tags 的干扰（建书保真度护栏，spike 实测 issue）。
2. 在聊天里定：方向、笔名、一句话种子、题材标签（上面的决策点）。
3. `drive_ui(set_field {field:"idea", value:种子})` + `drive_ui(set_field {field:"pen", value:笔名})` + `drive_ui(set_tags {tags:[题材标签]})`——**同批推送，浏览器按序应用**（步 1 校验 idea+pen 非空；题材标签在步 1 多选，流派随之推导，并作候选生成硬约束）。**步 1 已无「下一步」**——由步 1 底部「🎲 生成候选」替代（见下条）。
4. **世界观候选（必须完成，见上）**：**循环 `world_candidates(book_id="", idea=种子, genre, tags)` 约 5 次**（每次 1 个并自动填入步 2，无需 `set_candidates`）→ 用户在平台点选候选卡（高亮、可换）→ 用户点「已挑选完毕」进步 3（**页面自动把建书任务交给 dsh 驱动步 3**；Claude Code 不重复驱动）。不想选 → `drive_ui(skip_candidates)`。
5. **进步 3 = 内容构建工作台（默认已交棒 dsh；Claude Code 仅手动驱动时才走）**：用户在步 2 点「已挑选完毕」后步 3 默认由 **dsh（novel-build）分阶段驱动**（页面 `_agentDriving` 已抑制浏览器一键补全）。**Claude Code 不要与页面 task2 同时驱动步 3**——只在用户明确要求「由 Claude 直接驱动步 3」且页面未自动发任务时，才按下列规范逐段驱动（每段结果经 `drive_ui` 落进表单）：
   ① **核心矛盾**：`generate_core_conflict(idea=世界观简述, world_brief=候选简述, tags, pen_name)` → `drive_ui(set_world, {world_building:{core_conflict:"..."}})`（返回 genre 供②）。
   ② **大纲+桥段（步3内先生成，阻塞数分钟）**：`generate_outline_preview(idea, genre=①, tags, core_conflict=①, pen_name, world_brief)` → `drive_ui(set_outline, {outlines, plots, threads, themes, basic_info})`（返回的大纲+桥段存向导 state，submit 随书落库，书创建即 phase=ready）。**不再 query_structures/query_plots/set_picks**。
   ③ **势力**（根据桥段分析）：`generate_factions(idea, world_brief, core_conflict=①, tags, genre, outline_data=②)` → `drive_ui(set_world, {world_building:{factions:[...]}})`。
   ④ **主要人物**（势力生成之后，依据大纲+桥段+势力）：`query_characters` 看原型 → `generate_characters(idea=世界观简述, title, tags, genre, archetype_ids=选中的原型, core_conflict=①, factions=③, outline_data=②)` → `drive_ui(set_characters, {characters:[平铺映射后的列表]})`——**传全 14 字段**：`name/identity/personality/catchphrase/importance/golden_finger/relation/archetype_id/gender/brief/title/age/death_year/role`（主角 importance=1、配角补 relation；gender/brief/title/age/death_year 由 generate_characters 产出，agent 原样透传，向导不展示但会保到建书，详情页可编辑）。
   ⑤ **其余世界观维度**：`generate_rest_world(idea, world_brief, core_conflict=①, factions=③, outline_preview=②序列化文本, tags, genre, pen_name)` → `drive_ui(set_world, {world_building:{era,power_system,geography,culture,history,social_structure,rules,world_summary}, tone, target_audience, pov, era_language})`。
   失败/跳过：任一段失败重试一次，仍失败跳过该段继续（已填内容保留、部分构建可提交）；② 失败则退回旧路径（`set_picks` 选材，submit 后 `generate_full_outline` 补生成）；⑤ 未做则 `_world_generated` 不置位。书名已由候选带入步 3，想改才 `drive_ui(set_field title=...)`。
6. 用户在浏览器可编辑/删角色行后继续。
7. **submit**：无需等一键补全，随时 `drive_ui(submit)`（步 3 仅拦进行中的 `fillWorld` 兜底）。**系统** `POST /books/start` 建书——步 3 分阶段构建的各段内容 + ②生成的大纲+桥段（`_outline_data`）已随 submit 落库，**书创建即 phase=ready**（不再 submit 后手动 generate_full_outline）。

## submit 后：向导已入库跳书详情，确认 phase=ready 交棒写作台
- `drive_ui(submit)` 建书成功后，**向导直接跳转书详情页（/books/&lt;id&gt;）**——3 步建书结束，步 3 分阶段构建的各段内容（核心矛盾/大纲+桥段/势力/人物/其余维度）已随 submit 落库，书创建即 phase=ready。
- 用只读工具轮询定位新书：
  1. `list_books` → 找到新书 `book_id`。
  2. `get_book_detail(book_id)` 检查世界观是否已充实（`basic_info.world_building` 各维非空）。通常已是——步 3 各段已随 submit 落库；仅当单薄（如 ⑤ 未做或 LLM 失败用户仍提交）才兜底 `generate_world(book_id, mode="one", idea=...)`。
  2.5 **保真度校验（必做）**：核对 `genre` / `sub_genre` / `tags` 与用户设定一致；漂移 → `navigate("/books/start")` + `drive_ui(reset)` + 重填 set_field/set_tags 重走批处理（最多重试 1 次，仍漂移则如实汇报停止）。
  3. `get_book_detail(book_id)` 确认 `phase == "ready"` 且 `outlines`/`plots` 非空（② 失败退回旧路径时，此处才需 `generate_full_outline(book_id)` 补生成）。
  4. `navigate(url="/books/<book_id>/continue")` 交棒写作台写前三章。
- 建书后**不要在向导页再 `drive_ui(next)`**（向导已跳书详情，命令桥守卫 bookId 已拦）。

## 删书（护栏：外部 agent 不能直删）
- 用户要求删书 → `navigate(url="/books")` + 告知「请在书库页点该书旁的删除按钮（有确认弹窗）」。agent 不做删除动作。

## 前三章开篇钩子（指令层，本 skill 内置）
前三章是吸睛关键，进入写作台后按此规则把握：
- **第一章第一句必须是强钩子**：从冲突/悬念/奇观/反常起手，不要环境描写或背景介绍开场（例：主角被系统判死刑的瞬间，而不是「这是一个修真世界…」）。
- **前三章内给出第一个爽点**：金手指觉醒/打脸/危机反转，让读者在第三章结束前吃到第一次回报。
- **每章章末留钩子**：结尾停在疑问/危机/反转上，制造追读欲。
- **落地检查**：写完章节后用 `mcp__novel-engine__review_text`（章末钩子评分）和 `mcp__novel-engine__diagnose_retention`（掉读风险）核验；不合格 → 引导到 `novel-write` 重写该章。不加新工具，靠现有规则层检查。

## 退出状态
- 成功：向导跑完世界观+大纲，`phase=ready`，浏览器停在写作台。用 `get_book_detail` 复核（主角名 + 世界观非空）。
- 下一步自然衔接：`novel-write`（写前三章）或 `novel-outline`（调整大纲）。

## 失败处置
- 「LLM 未配置」→ 提示到设置页或 `api.json` 配 key 后重试。
- `drive_ui` 后浏览器没反应 → 检查浏览器是否停在 `/books/start`（`navigate` 一次再试）；命令桥就绪轮询 ≤10s。
- 建书后浏览器未跳书详情 → `list_books`/`get_book_detail` 确认书已建，若已建可直接 `navigate` 书详情/写作台。
- `world_candidates`（路线 B）空 → 重试一次；仍空改 `drive_ui(skip_candidates)` + 手动 `set_field world_desc`。
- `BookBusyError` → 另一进程在操作此书，稍后重试。
