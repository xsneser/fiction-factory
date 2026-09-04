---
name: novel-build
description: 建书 步3。已选候选/补全世界观/继续建书/深化弧/确认弧/故事线草案。流程:步3 内容构建工作台自主生成(先查弧/桥段库取素材 → 核心矛盾→势力→弧+桥段→人物→其余维度,set_world/set_outline/set_characters 落表单) → validate_storyline/validate_world 内联校验 → 汇报设定概要 → 用户自行提交建书(agent 不调 submit)。书创建即 phase=plots(草案待确认)；提交后跑「深化弧+桥段」(差异化命题 + validate(book_id) 回改环 → 蓝图汇报)，ready 只由用户在书详情页「确认弧+桥段」触发(agent 无翻 ready 工具)。
---

# 建书 步3：内容构建工作台（novel-build）

> 护栏：建书只能 `drive_ui` 驱动浏览器向导，**agent 不调 submit**（用户自行点击提交）；**agent 无任何把书翻到 ready 的工具**——plots→ready 只能用户在书详情页点「✅ 确认弧+桥段，开始写作」。直建/直删工具不在工具面。
> 触发：用户在步 2 点「已挑选完毕」后，页面自动把本任务发给 agent；也可在侧栏说「继续建书 / 深化弧 / 确认弧」。
> 书创建跳到书详情（?newdraft=1）后页面会**自动再派发一次本任务**（深化段），本 skill 覆盖提交后深化。
> 契约（set_world/set_outline/set_characters/校验字段）见 NOVEL_AGENT.md 1.2。

## 步 3 思考与迭代（不是固定顺序流程：可反复思考、任意顺序修改设定与故事线，每改一版落对应表单）
- **先搜索两个库取素材**：`query_arc_library`/`arc_material_candidates` 查**情节弧库**模板（弧库=**平级独立弧**，每弧一条参考，无模板内嵌子弧）、`query_plots` 查**桥段库**（需要时再 `query_gags`/`query_characters`）作参考，再动手设计。
- **差异化命题（存盘，反模板）**：动手排弧前先想清「本书与同类/所查模板的三个差异点」（题材套路 × 借书 × 世界观如何偏离），把最核心一条压进 `core_conflict`（world_building 标准键，随书落库）；发散比较与完整论述在书已建（phase=plots）后经 `save_basic_info` 写 `world_building.differentiation`（深合并保留；提交前向导期无书、自定义键会被 worldToBi 丢，先只压 core_conflict）。
- **发散先行**：先在自身上下文出 2–3 条**相异**弧骨架，互相对比择一/融合后再细排；**不要**拿到弧库模板就逐槽位照填（库是"对镜"，借骨不借皮）。
- 围绕「核心矛盾 → 势力 → 弧+桥段 → 人物 → 其余维度」反复推演：先想清楚故事线与世界观，再落 `set_world`/`set_outline`/`set_characters`。
- **弧+桥段**：outlines 弧树嵌套按字数跨度（`start_word/end_word`）、plots **仅挂最底层弧**；用 `set_outline` 落表。**每条弧 `notes` 必含「本弧目标 + 偏离库模板 X 的点」**（notes 落库、供蓝图/用户过目复核）。
- **弧树层级自主定**：弧库模板是**单弧参考**（独立弧，借其方向/戏剧目标即可）；书内弧树（顶层弧 + `parent_arc_id` 子弧、层数/分支）由你按剧情结构自主设计——**不必照抄模板、不必均匀**（有的顶层弧不拆、有的两层、有的更深）。
- **校验（走工具，不靠肉眼）**：生成/修改后、提交前调 `validate_storyline(outlines=..., plots=..., words_per_chapter=...)`（内联模式，步3 书未创建时用）+ `validate_world(basic_info=...)`（势力/人物一致性），按 `decision_points` 反复补弧/移桥段/缩弧跨度/补人物直到 `passed=true`，或如实向用户说明残留问题。
- **反复反思**：从剧情吸引力、设定一致性、阅读节奏出发反复审视，发现问题继续改，直到满意为止。
- **全部落定后停下**，向用户汇报设定概要（书名/世界观/势力/人物/差异化命题/弧+桥段数），让用户**自行点击按钮提交**（agent 不调 submit）。

## 提交后：深化弧+桥段（phase=plots 草案）→ 引导用户确认
用户提交建书后 → `get_build_status()` 拿 `book_id`（`created=true` 才算建成；未建成先等片刻再查，仍无 → 如实汇报）。`get_book_detail(book_id)` 看 phase：

- **phase=plots（草案，弧+桥段已随 submit 落库）→ 跑深化段**（研究式多段深挖，不是一次出稿）：
  1. **审差异化**：读 `basic_info.core_conflict`/`world_building.differentiation`；要补强则 `save_basic_info(book_id, {world_building:{differentiation:…}})`（plots 期已放行）。
  2. **补 notes（L1 存盘）**：逐顶层弧查 notes；缺「弧目标+偏离点」的，补全后 `save_outlines(mode=replace)` 重落（保持 phase=plots）。
  3. **回改环（L2，至少 1 轮）**：`validate_storyline(book_id=…)`（含 arc_fill 弧内空白）→ 按 decision_points 回改（save_outlines）→ 再校验；**直到 passed=true，或把残留 decision_points 逐条列入汇报**。第一次没过不许直接跳过。
  4. **蓝图汇报**：给用户 弧树层级 / 每弧字数跨度 / 桥段数 / 线程与设局-收局 + `passed` + top `decision_points` + 差异化命题一句话；提示在书详情页预览故事线，点「✅ 确认弧+桥段，开始写作」。**agent 不做任何翻转动作**（没有工具）。
- **phase=ready**（用户已确认或已有正文）→ 直接交棒写作台。
- **phase=config**（submit 时②弧失败没带上）→ 先补弧：自主生成 outlines/plots → `save_outlines(mode=replace)` 落盘（→ plots）→ 回到上面深化段。

## 退出状态
- 弧+桥段深化完、蓝图已汇报，停在书详情等用户确认；用户点确认后 phase=ready → 下一步 `novel-story` 写作。

## 失败处置
- `drive_ui` 后浏览器没反应 → `navigate('/books/start')` 再试。
- 向导卡步 / 深化后确认按钮无反应 → `get_book_detail` 看 phase：若已 ready 直接 navigate 写作台。
- `BookBusyError` → 稍后重试。
