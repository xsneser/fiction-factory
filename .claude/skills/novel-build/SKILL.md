---
name: novel-build
description: >-
  建书阶段向导。Use when the user wants to 开新书/创建小说/新建一本/构思世界观/写人物设定/借鉴已有书/选题材标签/生成书名/写开头几章
  (start a new novel, build world and characters, borrow from an existing book, pick a title)。
  流程：navigate 建书页 → 一句话设定+笔名 → create_book → 世界观候选 → 用户挑 → generate_world
  → save_basic_info 微调/标签 → confirm_world 锁定。内含前三章开篇钩子规则（指令层）。
  前置：书不存在或 phase=config。不做大纲（那是 novel-outline）。
---
# 建书阶段（novel-build）

## 前置检查（必做，只读工具）
1. `mcp__novel-engine__list_books` 看目标书是否已存在。
2. 已存在 → `mcp__novel-engine__get_book_detail` 看 `phase`：
   - `config` → 继续本 skill（已有基本盘，补设定即可，跳过已完成的步骤）。
   - `outlines/plots/ready` → 已过建书阶段，引导到 `novel-outline` / `novel-write`，不要重复建书。
3. 不存在 → `mcp__novel-engine__navigate`（url=`/books/start`）让用户看到建书页。

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
   流派不用让用户纠结——平台流派由题材标签推导，`create_book` 按上面标注的 genre 填即可。
2. **笔名（先查现成档案，给编号选项）**：
   - 先 `mcp__novel-engine__query_profiles` 拉现有笔名档案（预设 枫落/夜雨/青衫 + 用户自建），把 `pen_name` + 风格摘要列成编号选项让用户挑。
   - 档案里没有合意的 → 让用户报一个新笔名（后续可在 `/profiles` 建档案）。
   - 用户说「你定」→ 按写作方向挑最匹配的现有笔名；没有合适的就起一个新的。
   - **不要一上来开放式问「用什么笔名」**——现有档案要先摆出来。
3. **一句话种子设定**：问主角核心卖点 / 一句话设定（如「都市爽文，2050 太阳熄灭」）——它决定世界观候选方向。
4. **世界观候选（建书前，向导②顺序）**：
   - 从零构思 → `mcp__novel-engine__world_candidates(book_id="", idea=种子, genre=方向genre)` —— **无书即可出候选**（book_id 留空，这是向导②的建书前调用）→ 把每个候选的 `one_liner` 编号列出让用户挑。
   - 借鉴已有书 → `mcp__novel-engine__borrow_preview(source_book_id=...)` 预览 → 用户确认走借鉴路线（该路需先建书再 `generate_world(mode="borrow")`）。
   - **不要先 create_book 再 world_candidates**——候选在无书阶段就能出，保持向导②的顺序。
5. **微调设定 / 题材标签**：生成后把世界观摘要+主角摆给用户看，问要不要改（改哪个字段用 `save_basic_info` 只覆盖那一项）；题材标签（`world_building.tags`）让用户从平台 `WORLD_TAGS` 里挑，流派随之推导。

## 批处理（按向导顺序：先世界观候选 → 建书 → 生成世界观 → 微调 → 锁定）
1. `mcp__novel-engine__world_candidates(book_id="", idea=种子, genre=方向genre, sub_genre=...)` → 出候选，把每个候选的 `one_liner` 编号让用户挑一个。
2. `mcp__novel-engine__create_book(title=种子工作名, pen_name, genre=方向genre, sub_genre=...)` → 记下返回的 `book_id`。
3. `mcp__novel-engine__generate_world(book_id, mode="one", idea=所选候选的 one_liner)` 生成实际世界观（借鉴路线则 `mode="borrow", source_book_id=...`）。
4. `mcp__novel-engine__save_basic_info(book_id, basic_info=...)` 落库（深合并，只覆盖要改的字段）+ 微调/题材标签。
5. `mcp__novel-engine__confirm_world(book_id)` → 返回 `world_generated=true` 才算锁定过关；`false` 说明 basic_info 不够充实（世界观维度或主角缺失），补设定后重试。

## 前三章开篇钩子（指令层，本 skill 内置）
前三章是吸睛关键，无论是否立即进入写作，都按此规则把握：
- **第一章第一句必须是强钩子**：从冲突/悬念/奇观/反常起手，不要环境描写或背景介绍开场（例：主角被系统判死刑的瞬间，而不是「这是一个修真世界…」）。
- **前三章内给出第一个爽点**：金手指觉醒/打脸/危机反转，让读者在第三章结束前吃到第一次回报。
- **每章章末留钩子**：结尾停在疑问/危机/反转上，制造追读欲。
- **落地检查**：写完章节后用 `mcp__novel-engine__review_text`（章末钩子评分）和 `mcp__novel-engine__diagnose_retention`（掉读风险）核验；不合格 → 引导到 `novel-write` 重写该章。不加新工具，靠现有规则层检查。

## 退出状态
- 成功：`phase=config` + `_world_generated=true`。用 `get_book_detail` 复核（主角名 + 世界观至少 4 个维度非空）。
- 下一步自然衔接：`novel-outline`（生成大纲）→ `novel-write`（写前三章）。

## 失败处置
- 「LLM 未配置」→ 提示到设置页或 `api.json` 配 key 后重试。
- `world_candidates` 空 → 重试一次；仍空改 `generate_world` 一句话直出。
- 已确认过世界观 → 立即 `confirm_world` 打标，避免重复生成。
- `BookBusyError` → 另一进程在操作此书，稍后重试。
