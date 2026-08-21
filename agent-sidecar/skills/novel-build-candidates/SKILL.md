---
name: novel-build-candidates
description: 建书步 1（候选生成呈现）：开新书/写设定/构思世界观。headless 一次性任务，决策点由任务指令给全。只做一件事——生成世界观候选并**呈现**（world_candidates → drive_ui(set_candidates)），**停在步 2 等用户挑选**，不自动选/跳步；按钮路径只需 2 次工具调用。护栏：create_book/delete_book 不在 MCP 面。挑选完建书由 novel-build 接续。
---

# 建书步 1：世界观候选生成（novel-build-candidates）— dsh 侧车版

> **护栏（必须遵守）**：`create_book` / `delete_book` 是 web-only，不在 MCP 面——本 skill 绝不调用它们。
> 建书只能驱动系统向导（`drive_ui` 写意图队列，浏览器 `/books/start` 轮询消费后由系统 `POST /books/start` 创建）。
> headless 是一次性任务：idea/题材标签/笔名等决策点由**任务指令给全**，不做交互问答。
> **本 skill 只做一件事：生成候选并呈现，然后停**。挑选是用户动作；用户在步 2 点「已挑选完毕」后建书由 `novel-build` 自动接续。
> **原则：工具调用尽量少**——按钮路径下表单/笔名/步序 web 已备好，只需 **2 次调用**。

## 两条路径（先判定再动手）

### 🚀 按钮路径（任务说「已完成步 1 填表 / 已自动进步 2」）——极简，只 2 次调用
表单已填好（idea/tags/笔名）、向导已在步 2、书必不存在（新书向导）。
**禁止** `list_books` / `get_build_status` / `navigate` / `reset` / `set_field` / `set_tags` / `next` / `query_profiles`——全是冗余，不做。

1. `mcp__novelengine__world_candidates(book_id="", idea=任务的一句话设定, tags=任务的题材标签)`（**genre 可空，由 tags 推导**；LLM 调用约 2-5s）→ 候选。
2. `mcp__novelengine__drive_ui(set_candidates, {candidates:[{title, one_liner, world_brief}]})` → 浏览器把候选渲染成**可点选卡**，用户点选后高亮、可换。
3. **停止**，结束回复：「✅ 已生成 N 个世界观候选并呈现在步 2，请在浏览器挑选一个方向；点「已挑选完毕」后，建书（步 3 分阶段世界观 → submit → 完整大纲）会自动接续。」

笔名**仅当**任务里为「（未选，请帮我选）」时才补：`query_profiles` 选最匹配 → `drive_ui(set_field, {field:"pen", value:笔名})`（自动补 option）。

### 💬 侧栏路径（任务说「开一本新书」）——完整驱动表单
任务已给全 idea/tags/pen 则直接填；未给全按任务给的写作方向从编号菜单选最匹配（genre 仍可空由 tags 推导）：
1. `navigate(url="/books/start")`（若已在向导页则无害）。
2. `drive_ui(reset)`：清残留草稿（保留笔名）。
3. `drive_ui(set_field, {field:"idea", value:种子})` + `drive_ui(set_field, {field:"pen", value:笔名})` + `drive_ui(set_tags, {tags:[题材标签]})`。
4. `drive_ui(next)`（步 1 → 步 2）。
5. 同按钮路径：`world_candidates(book_id="", idea=种子, tags=题材标签)` → `drive_ui(set_candidates, {candidates:[…]})` → **停止**交棒。

## 硬规则
- **绝不 `drive_ui(pick_candidate)` / `next` / `submit`**——挑选是用户动作，候选卡渲染后即停止。
- **drive_ui 是两参工具**：必须同时传 `cmd` 与 `args`（如 `drive_ui(cmd="set_candidates", args={candidates:[{title, one_liner, world_brief}]})`），`cmd` 必填不可省——漏传会报 `cmd Field required`。

## 换候选 / 跳过
- 候选质量差 / 用户要换 → 重新 `world_candidates` → `drive_ui(set_candidates, ...)` 覆盖重渲染。
- 用户想跳过候选 → `drive_ui(skip_candidates)`（进步 3 手动模式，**不触发** agent 建书）。

## 退出状态
成功：候选已呈现在步 2，停在交互点等用户挑选。下一步：用户点「已挑选完毕」→ 页面自动触发 `novel-build`（建书）。

## 失败处置
- `world_candidates` 空/失败 → 重试一次；仍空 → `drive_ui(skip_candidates)` + `drive_ui(set_field world_desc=手动世界观简述)`（步 3 自动补全兜底），如实汇报。
- `drive_ui` 后浏览器没反应 → `navigate(url="/books/start")` 再试。
