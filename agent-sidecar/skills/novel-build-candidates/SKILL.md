---
name: novel-build-candidates
description: 建书步 1（候选生成呈现）：开新书/写设定/构思世界观。**按钮路径**（headless 一次性）：**agent 自主生成 5 个世界观候选**（每次在你自己的上下文里生成 1 个 `{title, one_liner, world_brief}`，差异化记住已生成 title）→ `drive_ui(add_candidate)` 逐张填入步 2，停在步 2 等用户挑选，不自动选/跳步；侧栏「再来几个」可续接追加。**侧栏路径**：先 navigate('/books/start') 翻到步 1 表单（已给全 idea/tags 则预填），交用户填写点「🚀 让 Agent 构建」后按钮路径接管。护栏：create_book/delete_book 不在 MCP 面。挑选完建书由 novel-build 接续。
---

# 建书步 1：世界观候选生成（novel-build-candidates）

> **护栏（必须遵守）**：`create_book` / `delete_book` 不在 MCP 面——本 skill 绝不调用它们；建书只能 `drive_ui` 驱动系统向导、删书只能 navigate 书库页手动（见 CLAUDE.md）。
> headless 是一次性任务，不做交互问答，分三条路径：
> - **按钮路径**（任务说「已完成步 1 填表 / 已自动进步 2」）：决策点由任务指令给全，**agent 自主生成候选**填到步 2，停在交互点等用户挑选；挑选后建书由 `novel-build` 自动接续。
> - **侧栏路径**（用户说「帮我建一本 / 开一本新书」）：翻到 `/books/start` **步 1 表单**让用户自行填写（已给全则预填），然后**停**；用户点「🚀 让 Agent 构建」触发按钮路径。
> - **续接路径**（用户侧栏说「再来几个候选」）：复用同一 idea/tags，**agent 再生成**追加。
> **不要调已存档的厚工具**（`world_candidates` / `generate_*` 已移出 MCP，见 archive/deprecated_tools.md）——候选由你在自己上下文生成，用 `drive_ui(add_candidate)` 填入。

## 路径（先判定再动手）

### 🚀 按钮路径（任务说「已完成步 1 填表 / 已自动进步 2」）——agent 生成 5 个候选
表单已填好（idea/tags/笔名）、向导已在步 2、书必不存在（新书向导）。
**禁止** `list_books` / `get_build_status` / `navigate` / `reset` / `set_field` / `set_tags` / `next` / `query_profiles` / `set_candidates`——全是冗余，不做。

1. **你自主生成 5 个世界观候选**（每次 1 个，差异化）：基于一句话设定 idea + 题材标签 tags，在你自己的上下文里构思 `{title, one_liner, world_brief}`（记住已生成 title，保证 5 个方向不同、不重复）。
2. 每个生成完 → `mcp__novelengine__drive_ui(cmd="add_candidate", args={candidate:{title, one_liner, world_brief}})` ——卡片逐张实时填入步 2。
3. 某张生成失败/空 → 重试 1 次；仍空则跳过该张继续（有内容比凑数重要）。
4. **停止**，结束回复：「✅ 已生成 5 个世界观候选并逐张填入步 2，请在浏览器挑一个方向；点「已挑选完毕」后建书（步 3 由 agent 分阶段构建 → submit → 完整大纲）会自动接续。要更多候选就对我说『再来几个』。」

笔名**仅当**任务里为「（未选，请帮我选）」时才补：`query_profiles` 选最匹配（同步看 `registered_platforms` 已注册平台，告知用户）→ `drive_ui(set_field, {field:"pen", value:笔名})`（自动补 option）。

### 💬 侧栏路径（用户在侧栏说「帮我建一本 / 开一本新书」）——翻到步 1 表单，用户自行填写
不聊天索要设定、不代跳步、不代生成候选。核心 = 把用户带到启动新书**步 1 表单**；用户填写/确认后点「🚀 让 Agent 构建」触发按钮路径。
1. `navigate(url="/books/start")`（若已在向导页则无害）——翻到步 1 界面。
2. 向导停在非步 1 / 有残留草稿 → `drive_ui(reset)`（保留笔名、清表单、回步 1）；全新则跳过。
3. **预填（仅当任务已给全** idea/题材标签/笔名 **）**：`drive_ui(set_field, {field:"idea", value:种子})` + `drive_ui(set_tags, {tags:[题材标签]})`，笔名也给再 `drive_ui(set_field, {field:"pen", value:笔名})`——用户只需确认/微调；未给全则留空。
4. **停止**，回复引导：「已在浏览器打开启动新书，请填写/确认一句话设定、题材标签、笔名，点「🚀 让 Agent 构建」——我会把世界观候选逐张填到步 2 供你挑。」
5. 用户点按钮 → 任务带「已完成步 1 填表、已自动进步 2」→ 走**按钮路径**（agent 生成 × 5 → add_candidate）。本路径**绝不** 生成候选 / set_candidates / next / pick_candidate。

### 🔁 续接路径（侧栏「再来几个候选」）——追加新卡
候选已在步 2、用户要更多：
1. 复用**同一句话设定/题材标签**（从任务历史里取），**你自主再生成 1-2 个差异化候选**（避开已生成的 title）→ `drive_ui(add_candidate, {candidate})`。
2. 结束提示：「已追加到第 N 个候选，请在浏览器步 2 挑选；还要就再说『再来几个』。」

## 硬规则
- **绝不 `drive_ui(pick_candidate)` / `next` / `submit`**——挑选是用户动作，候选卡渲染后即停。
- **候选填入统一走 `drive_ui(add_candidate)`**——**不要 `drive_ui(set_candidates)`**（会整体覆盖误伤增量渲染）。
- **drive_ui 是两参工具**：必须同时传 `cmd` 与 `args`（如 `drive_ui(cmd="add_candidate", args={candidate:{...}})`），`cmd` 必填不可省——漏传会报 `cmd Field required`。

## 换候选 / 跳过
- 用户想重来 / 清掉候选 → `drive_ui(reset)`（清候选持久化 + 清表单回步 1）后重走按钮路径。
- 用户想跳过候选 → `drive_ui(skip_candidates)`（进步 3 手动模式，**不触发** agent 建书）。

## 退出状态
成功：候选逐张填入步 2，停在交互点等用户挑选。下一步：用户点「已挑选完毕」→ 页面自动触发 `novel-build`（建书）。

## 失败处置
- 某张候选生成失败/空 → 重试一次；仍空 → 跳过该张继续；全空 → `drive_ui(skip_candidates)` + `drive_ui(set_field world_desc=手动拼好的世界观简述)`（步 3 自动补全兜底），如实汇报。
- `drive_ui` 后浏览器没反应 → `navigate(url="/books/start")` 再试。
