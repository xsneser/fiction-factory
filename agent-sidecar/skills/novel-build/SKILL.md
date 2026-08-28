---
name: novel-build
description: 建书 步3。已选候选/补全世界观/继续建书。流程:步3 内容构建工作台自主生成(先查弧/桥段库取素材 → 核心矛盾→势力→弧+桥段→人物→其余维度,set_world/set_outline/set_characters 落表单) → validate_storyline/validate_world 内联校验 → 汇报设定概要 → 用户自行提交建书(agent 不调 submit)。书创建即 phase=ready。
---

# 建书 步3：内容构建工作台（novel-build）

> 护栏：建书只能 `drive_ui` 驱动浏览器向导，**agent 不调 submit**（用户自行点击提交）——直建工具不在工具面。
> 触发：用户在步 2 点「已挑选完毕」后，页面自动把本任务发给 agent；也可在侧栏说「继续建书」。
> 契约（set_world/set_outline/set_characters/校验字段）见 NOVEL_AGENT.md 1.2。

## 步 3 思考与迭代（不是固定顺序流程：可反复思考、任意顺序修改设定与故事线，每改一版落对应表单）
- **先搜索两个库取素材**：`query_arc_library`/`arc_material_candidates` 查**情节弧库**模板、`query_plots` 查**桥段库**（需要时再 `query_gags`/`query_characters` 查笑点/角色）作故事设计与弧树/桥段的参考，再动手设计。
- 围绕「核心矛盾 → 势力 → 弧+桥段 → 人物 → 其余维度」反复推演：先想清楚故事线（全文大纲）与世界观，再落 `set_world`/`set_outline`/`set_characters`，改到什么程度自己判断。
- **弧+桥段**：outlines 弧树嵌套按字数跨度（`start_word/end_word`）、plots **仅挂最底层弧**；用 `set_outline` 落表。
- **校验（走工具，不靠肉眼）**：生成/修改后、提交前调 `validate_storyline(outlines=..., plots=..., words_per_chapter=...)`（内联模式，步3 书未创建时用；已建书用 `validate_storyline(book_id=...)`，含 **arc_fill 弧内空白**）+ `validate_world(basic_info={world_building:{factions:...}, characters:[...]})`（势力/人物一致性），按 `decision_points` 反复补弧/移桥段/缩弧跨度/补人物直到 `passed=true`，或如实向用户说明残留问题。
- **反复反思**：从剧情吸引力、设定一致性、阅读节奏出发反复审视，发现问题继续改，直到满意为止。
- **全部落定后停下**，向用户汇报设定概要（书名/世界观/势力/人物/弧+桥段数），让用户**自行点击按钮提交**（agent 不调 submit）。

## 提交后：校验 + 交棒写作台
- 用户提交建书后 → `get_build_status()` 拿 `book_id`（`created=true` 才算建成；未建成先等片刻再查，仍无 → 如实汇报）。
- `get_book_detail(book_id)` 确认 `phase=="ready"` 且 `outlines`/`plots` 非空；单薄才自主生成 → `save_basic_info` 兜底。
- `navigate(url="/books/<book_id>/continue")` 交棒写作台。

## 退出状态
- 世界观 + 弧+桥段完成，`phase=ready`。下一步：`novel-story`（弧 + 写作）。

## 失败处置
- `drive_ui` 后浏览器没反应 → `navigate('/books/start')` 再试。
- 向导卡步 → `get_book_detail` 确认书已建、phase 到哪；若已 ready 直接 navigate 写作台。
- `BookBusyError` → 稍后重试。
