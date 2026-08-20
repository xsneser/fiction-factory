---
name: novel-build
description: 建书阶段。开新书/写设定/构思世界观。建书必须走系统向导 UI(drive_ui),护栏:create_book/delete_book 不在 MCP 面。前置:书不存在或 phase=config。内含前三章开篇钩子规则（指令层）。
---

# 建书阶段（novel-build）— dsh 侧车版

> **护栏（必须遵守）**：`create_book` / `delete_book` 是 web-only，不在 MCP 面——本 skill 绝不调用它们。
> 建书只能驱动系统向导（`drive_ui` 写意图队列，浏览器 `/books/start` 轮询消费后由系统 `POST /books/start` 创建）。
> headless 是一次性任务：写作方向/笔名/一句话种子/题材标签等决策点由**任务指令给全**，不做交互问答。

## 前置检查（只读）
1. `mcp__novelengine__list_books` 看目标书是否存在。
2. 已存在 → `mcp__novelengine__get_book_detail` 看 `phase`：`config` → 补设定即可；`outlines/plots/ready` → 已过建书，转 novel-outline / novel-write（**不重复建书**）。
3. 不存在 → `mcp__novelengine__navigate(url="/books/start")`（浏览器切到向导页）。

## 决策点（任务指令给全；缺方向时按编号菜单选最匹配的）
- **写作方向菜单**（任务未指定 genre 时用）：1 都市爽文（重生/签到/神医/打脸）2 玄幻升级（修仙/异界/无敌流）3 科幻脑洞（星际/无限流/游戏）4 悬疑烧脑（权谋/复仇/悬念）5 言情甜宠（甜宠/双强/白月光）6 历史权谋（历史/权谋/军婚）7 自由发挥。流派由题材标签推导，向导据 tags 填 genre。
- **笔名**：先 `mcp__novelengine__query_profiles` 拉现有档案，从任务/档案里选最匹配的（预设 枫落/夜雨/青衫 + 用户自建）；无合意 → 用任务给的笔名（`drive_ui(set_field pen=...)` 会自动补 option）。
- **世界观候选（submit 硬前置）**：headless 用**路线 B**——`mcp__novelengine__world_candidates(book_id="", idea=种子, genre=方向, tags=标签)` 取候选 → `drive_ui(pick_candidate, {idx, candidate:{title, world_brief, one_liner}})` 注入（候选内嵌，不依赖浏览器点选）。

## 批处理（驱动向导 UI，由系统建书）

> **触发**：两种入口都可——① 浏览器「启动新书」页点「🚀 让 Agent 构建」按钮（idea/tags/笔名 已在表单，任务文本携带）；② 侧栏聊天说「开一本新书：…」。**前端固定候选管线已移除**（不再有「🎲 生成候选」fetch），候选生成完全由本 skill 走路线 B；`drive_ui(load_candidates)` 是前端 stub（提示用），不调。
> **无需平台启动**：dsh 是 Web 进程内子进程（平台已在 58080 跑，由 `libraries/dsh_bridge.py` 拉起），本 skill **不做平台启动/打开浏览器**；`navigate` 只切站内页。

1. `navigate(url="/books/start")`（若已在向导页则无害）。
2. **`drive_ui(reset)`**：每次建书前先重置向导 state（除笔名），清除上一本残留草稿对 set_field/set_tags 的干扰（建书保真度护栏，spike 实测 issue）。
3. `drive_ui(set_field, {field:"idea", value:种子})` + `drive_ui(set_field, {field:"pen", value:笔名})` + `drive_ui(set_tags, {tags:[题材标签]})`（同批推送，浏览器按序应用；表单已填时幂等）。
4. **`drive_ui(next)`（步 1 → 步 2）**：填完步 1 必须 `next` 进步 2（浏览器 `validate(1)` 校验 idea+pen 非空）。**勿漏此步**——否则下一步 `next` 只会把向导从步 1 推到步 2，而不是进步 3。
5. **世界观候选（路线 B，步 2 → 步 3）**：`world_candidates(book_id="", idea, genre, tags)` → `drive_ui(pick_candidate, {idx, candidate:{title, world_brief, one_liner}})`（注入，浏览器渲染卡片实时显示）→ **`drive_ui(next)`（步 2「已挑选完毕」→ 步 3）**；候选质量差 / 用户要求手定 → `drive_ui(skip_candidates)`（跳过挑选直接进步 3）。
6. **步 3 = 内容构建工作台（分阶段，agent 自主驱动）**：浏览器不再自动一键补全（旧 `world-complete` 保留为「✨ 重新补全」兜底按钮）。按顺序逐段构建，每段经 `drive_ui` 落进表单，步 3 顶部状态区 5 个徽标实时显示 ✅/未填：
   - ① **核心矛盾**：`generate_core_conflict(idea=世界观简述, world_brief=候选简述, tags, pen_name)` → `drive_ui(set_world, {world_building:{core_conflict:"..."}})`（返回 genre 供②查库）。
   - ② **开篇大纲+桥段**（agent 自主决策，不询问）：`query_structures(genre=①)` **一次拉全**模板清单，直接从返回挑 1-2 个最匹配，**不要换 keyword/sub_genre 重查**；`query_plots(category="开篇")` 同理一次拉全。选定立即 `drive_ui(set_picks, {templates:[{id,name}], plots:[{id,name}]})` → `_outline_picks` 随 submit 落库，submit 后 `generate_full_outline` 自动消费。
   - ③ **势力**：`generate_factions(idea, world_brief, core_conflict=①, tags, genre)` → `drive_ui(set_world, {world_building:{factions:[...]}})`。
   - ④ **主要人物**（基于势力和大纲）：`query_characters(genre=方向)` **一次查够**原型池（tag=主角/配角 最多各一次），选 archetype_ids 直接喂 `generate_characters(idea, title, tags, genre, archetype_ids=选中的原型, core_conflict=①, factions=③, outline_preview=②)`，**选定即停，不要逐角色重复 query_characters** → `drive_ui(set_characters, {characters:[全 14 字段列表]})`——`name/identity/personality/catchphrase/importance/golden_finger/relation/archetype_id/gender/brief/title/age/death_year/role`（主角 importance=1、配角补 relation；gender/brief/title/age/death_year 原样透传，详情页可编辑）。
   - ⑤ **其余世界观维度**（大纲确定后补）：`generate_rest_world(idea, world_brief, core_conflict=①, factions=③, outline_preview=②, tags, genre, pen_name)` → `drive_ui(set_world, {world_building:{era,power_system,geography,culture,history,social_structure,rules,world_summary}, tone, target_audience, pov, era_language})`。
   - 失败/跳过：任一段失败重试一次，仍失败跳过该段继续（已填内容保留、部分构建可提交）；⑤ 未做则 `_world_generated` 不置位，submit 后 `generate_full_outline` Phase 1 自动补齐剩余维度。
7. **submit**：无需等一键补全，随时 `drive_ui(submit)` → **系统** `POST /books/start` 建书（phase=config）——分阶段构建各段 + `_outline_picks` 已随 submit 落库。

## submit 后：校验 + 生成完整大纲
1. **`mcp__novelengine__get_build_status()`** 拿 `book_id`（浏览器建书成功会把 `bookId` 回写到 `storage/build_status.json`；`created=true` 才算建成，未建成先等片刻再查，仍无 → 重试 `drive_ui(submit)` 或如实汇报）。
2. `get_book_detail(book_id)` 看世界观充实度（步 3 已落库，通常充实；单薄才 `generate_world(book_id, mode="one", idea=...)` 兜底）。
3. **保真度校验（必做）**：核对 `genre` / `sub_genre` / `tags` 与任务设定一致；漂移 → `navigate(url="/books/start")` + `drive_ui(reset)` + 重填 set_field/set_tags 后重新走批处理（最多重试 1 次，仍漂移则如实汇报停止）。
4. **必须调** `mcp__novelengine__generate_full_outline(book_id)`（阻塞数分钟，逐步落盘，**自动消费 `_outline_picks`（②选定的模板/桥段）**；世界观充实自动跳过 Phase 1 故事分析）。
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
