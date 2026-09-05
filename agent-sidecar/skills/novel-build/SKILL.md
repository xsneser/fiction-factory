---
name: novel-build
description: 建书 步3。已选候选/补全世界观/继续建书。流程:步3 内容构建工作台**深化式**生成（先查弧/桥段库取素材 → 反模板差异化命题(core_conflict + world_building.differentiation 随 set_world 内联落库) → 核心矛盾→势力→弧+桥段(每弧 notes 含目标+偏离模板点)→人物→其余维度 → 内联 validate_storyline/validate_world 回打≥1轮直到 passed → 汇报故事线蓝图），用户自行提交建书(agent 不调 submit)。深化已并入步3、提交即 phase=ready（无书详情二次确认/无后置深化任务）；config 兜底补弧另走 save_outlines→plots→用户在书详情确认（agent 无翻 ready 工具）。
---

# 建书 步3：内容构建工作台（novel-build）

> 护栏：建书只能 `drive_ui` 驱动浏览器向导，**agent 不调 submit**（用户自行点击提交）；**agent 无任何把书翻到 ready 的工具**——正常建书由用户在向导点提交（步3 深化式内容随书落库）即 phase=ready；config/补弧兜底路径的 plots→ready 只能用户在书详情页点「✅ 确认弧+桥段，开始写作」。直建/直删工具不在工具面。
> 触发：用户在步 2 点「已挑选完毕」后，页面自动把本任务发给 agent；也可在侧栏说「继续建书 / 补全世界观 / 深化弧」。
> 契约（set_world/set_outline/set_characters/校验字段）见 NOVEL_AGENT.md 1.2。

## 步 3 深化式内容构建（一次到位，不是浅稿后深化：本步产出即定稿级故事线）
- **先搜索两个库取素材**：`query_arc_library`/`arc_material_candidates` 查**情节弧库**模板（弧库=**平级独立弧**，每弧一条参考，无模板内嵌子弧）、`query_plots` 查**桥段库**（需要时再 `query_gags`/`query_characters`）作参考，再动手设计。
- **差异化命题（存盘，反模板）**：动手排弧前先想清「本书与同类/所查模板的三个差异点」（题材套路 × 借书 × 世界观如何偏离）。最核心一条压进 `core_conflict`；**完整发散论述直接写 `world_building.differentiation`**——二者都随 `set_world` 内联落库（2026-09-05 起向导已透传该键，无需等书建后再补）。
- **发散先行**：先在自身上下文出 2–3 条**相异**弧骨架，互相对比择一/融合后再细排；**不要**拿到弧库模板就逐槽位照填（库是"对镜"，借骨不借皮）。
- 围绕「核心矛盾 → 势力 → 弧+桥段 → 人物 → 其余维度」反复推演：先想清楚故事线与世界观，再落 `set_world`/`set_outline`/`set_characters`。
- **弧+桥段（字数按内容、反印刷感）**：outlines 弧树嵌套按字数跨度（`start_word/end_word`）、plots **仅挂最底层弧**；用 `set_outline` 落表。**每个桥段 `words`（目标字数，0 基整数，300~2500）按场景浓淡给——过渡/日常 300~600、常规推进 800~1600、关键/高潮 1800~2500，同弧/全书不要全部相等**；弧字数跨度 = 其桥段 `words` 之和（叶弧可跨多章、同父下不必相等、可三层），**不要按章数/words_per_chapter 均分**。**第三层条件化**：目标块 ≥3 章（≥3×words_per_chapter）且内含 2+ 可独立排序子目标时拆第三层（判据/样板见 NOVEL_AGENT.md 1.1）；每本书至少检查一遍有无这样的块——没有就保持两层并在 notes/汇报里说明，**勿为凑层硬拆**。**每条弧 `notes` 必含「本弧目标 + 偏离库模板 X 的点」**（notes 落库、供蓝图/用户过目复核）。
- **弧树层级自主定**：弧库模板是**单弧参考**（独立弧，借其方向/戏剧目标即可）；书内弧树（顶层弧 + `parent_arc_id` 子弧、层数/分支）由你按剧情结构自主设计——**不必照抄模板、不必均匀**（有的顶层弧不拆、有的两层、有的更深）。**反例（勿做）**：顶层弧按章均分等长叶弧、每个桥段字数全同——那是印刷感，不是剧情结构。
- **校验回打环（L2，走工具不靠肉眼，至少 1 轮）**：生成/修改后、提交前调 `validate_storyline(outlines=..., plots=..., words_per_chapter=...)`（内联模式，步3 书未创建时用）+ `validate_world(basic_info=...)`（势力/人物一致性）；按 `decision_points` 反复补弧/移桥段/缩弧跨度/补人物**直到 `passed=true`**（首次没过不许直接跳过）；若返回 `structure_hints`（叶弧跨度全相等/桥段字数全相同，软提示）**必须重排至消除或向用户说明**；仍有残留则逐条列入汇报向用户如实说明。
- **反复反思**：从剧情吸引力、设定一致性、阅读节奏出发反复审视，发现问题继续改，直到满意为止。
- **全部落定后停下**，向用户汇报**故事线蓝图**：书名/世界观/势力/人物 + 差异化命题一句话 + 弧树层级（顶层弧与子弧、每弧字数跨度）/ 桥段数 / 线程与设局-收局 + `passed`。**用户在步 3 即可看到故事线**，满意后**自行点击按钮提交**（agent 不调 submit）。提交即 phase=ready，无需书详情二次确认。

## 提交后：查相位收尾（无深化段——深化已在步3完成）
用户提交建书后 → `get_build_status()` 拿 `book_id`（`created=true` 才算建成；未建成先等片刻再查，仍无 → 如实汇报）→ `get_book_detail(book_id)` 看 phase：

- **phase=ready**（弧+桥段完整随 submit 落库，正常）→ 故事线已可写作，交棒 `novel-story`；向用户说明可直接进写作台。
- **phase=config**（submit 时②弧失败没带上）→ 兜底补弧：自主生成 outlines/plots（含每弧 notes）→ `save_outlines(mode=replace)` 落盘（→ phase=plots）→ 引导用户在书详情页点「✅ 确认弧+桥段」翻 ready（legacy 恢复门，非常态）。
- **phase=plots**（仅 outlines 无 plots 的 edge）→ 同上引导用户书详情确认补弧/确认。

## 退出状态
- 正常：蓝图已汇报、用户提交建书 → 书 phase=ready，交棒写作台。
- 兜底：config 补弧落 plots → 停在书详情等用户确认。

## 失败处置
- `drive_ui` 后浏览器没反应 → `navigate('/books/start')` 再试。
- 向导卡步 / 提交后确认按钮无反应 → `get_book_detail` 看 phase：若已 ready 直接 navigate 写作台。
- `BookBusyError` → 稍后重试。
