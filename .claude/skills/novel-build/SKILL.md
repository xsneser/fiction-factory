---
name: novel-build
description: >-
  建书阶段向导。Use when the user wants to 开新书/创建小说/新建一本/构思世界观/写人物设定/借鉴已有书/选题材标签/写开头几章
  (start a new novel, build world and characters, borrow from an existing book, pick a title)。
  建书必须走「启动新书」界面：navigate /books/start → drive_ui 填表单 → 点下一步 → 由系统创建（护栏：
  create_book 已从 MCP 面移除，agent 不直建书）。内含前三章开篇钩子规则（指令层）。
  前置：书不存在或 phase=config。不做大纲（那是 novel-outline）。
---
# 建书阶段（novel-build）

> **护栏（必须遵守）**：`create_book` / `delete_book` 已从 MCP 面移除（web-only，内部侧栏 agent 才有）。
> 建书只能驱动系统向导 UI；删书只能 navigate 到书库让用户手动删。**本 skill 绝不调用 create_book / delete_book**。

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
   - 先 `mcp__novel-engine__query_profiles` 拉现有笔名档案（预设 枫落/夜雨/青衫 + 用户自建），把 `pen_name` + 风格摘要列成编号选项让用户挑。
   - 档案里没有合意的 → 让用户报一个新笔名（drive_ui set_field pen 会自动补 option）。
   - 用户说「你定」→ 按写作方向挑最匹配的现有笔名。
   - **不要一上来开放式问「用什么笔名」**——现有档案要先摆出来。
3. **一句话种子设定**：问主角核心卖点 / 一句话设定（如「都市爽文，2050 太阳熄灭」）——它决定世界观候选方向。

## 世界观候选（submit 的硬前置——没让用户挑候选前，不得 submit）

> 先定路线再进向导步 2。**硬规则：候选挑选必须完成并经用户确认，否则不得 `drive_ui(submit)`。** 这是用户明确要求的决策点，不可跳过。

- **路线 A（默认，用户浏览器点）**：`drive_ui(load_candidates)` → 浏览器渲染候选卡 → **必须 AskUserQuestion 问用户点了哪个候选** → 用户确认后才 `drive_ui(next)`。
- **路线 B（agent 聊天给候选，全 agent 驱动）**：`world_candidates(book_id="", idea=种子, genre=方向genre)` 出候选 → 聊天里编号让用户挑 → `drive_ui(pick_candidate, {idx, candidate:{title, world_brief, one_liner}})`（候选内嵌，不依赖浏览器渲染）；或 `drive_ui(skip_candidates)` + `drive_ui(set_field world_desc=手动拼好的世界观简述)`——**skip 也要向用户说明**，不能无声跳过。

## 批处理（驱动向导 UI；agent 只填表单/点按钮，由系统建书）
1. `navigate(url="/books/start")`。
2. 在聊天里定：方向、笔名、一句话种子（上面的决策点）。
3. `drive_ui(set_field {field:"idea", value:种子})` + `drive_ui(set_field {field:"pen", value:笔名})` + `drive_ui(next)`——**同批推送，浏览器按序应用**（向导步 1 校验 idea+pen 非空后进步 2）。
4. **世界观候选（必须完成，见上）**：路线 A 或 B 走完、用户确认后 `drive_ui(next)`。
5. 向导步 3：`drive_ui(set_field {field:"title", value:书名})` + `drive_ui(set_tags {tags:[题材标签]})`（标签从平台 `WORLD_TAGS` 挑，流派随之推导）；可选 `drive_ui(gen_characters)` 让用户在浏览器点「添加」角色。
6. `drive_ui(submit)` → **系统** `POST /books/start` 建书（phase=config）——**不再自动生成**，世界观+大纲由 agent 生成（见下）。

## submit 后：agent 经 MCP 生成世界观 + 完整大纲（关键）
- `drive_ui(submit)` 建书后，向导第 4 步显示故事线 Gantt 空态并轮询填充。
- 用只读工具轮询定位新书：
  1. `list_books` → 找到新书 `book_id`。
  2. **生成世界观**：`generate_world(book_id, mode="one", idea=所选候选的 one_liner)`（阻塞，写入 basic_info）。
  3. **生成完整大纲**：`generate_full_outline(book_id)`（阻塞数分钟，逐步落盘——向导第 4 步 Gantt 实时填充）。
  4. `get_book_detail(book_id)` 确认 `phase == "ready"`。
  5. `navigate(url="/books/<book_id>/continue")` 交棒写作台写前三章。
- **禁止**在向导步 3 上再 `drive_ui(next)`（会进空步 4，向导卡死）。

## 删书（护栏：外部 agent 无 delete_book）
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
- 向导卡在步 4/5（submit 后 SSE 跑完没跳转）→ `list_books`/`get_book_detail` 确认书已建、`phase` 到哪一步，若已 ready 直接 navigate 写作台。
- `world_candidates`（路线 B）空 → 重试一次；仍空改 `drive_ui(skip_candidates)` + 手动 `set_field world_desc`。
- `BookBusyError` → 另一进程在操作此书，稍后重试。
