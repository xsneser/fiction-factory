---
name: novel-story
description: 写作阶段（write profile 最小工具面）。写正文/写下一章/写情节段/继续写。流程:get_writing_context 一次拿全 → 逐情节段自主生成正文+结构化 outcome → save_plot_draft(character_events/outcome/expected_facts) → 章满 save_chapter_text(→reconcile)→chapter_quality_gate。前置:phase=ready。config/plots 补弧不在本 profile(引导走建书/书详情确认)。
---

# 写作（novel-story）— write profile 最小工具面

> 本 skill 运行于 **profile=write**，工具面只有下面六个：`get_writing_context / get_pen_style / pick_plot_sample / save_plot_draft / save_chapter_text / chapter_quality_gate`。
> **只用这六个**；凡本文件没有出现的工具都不暴露——任何列表外工具都会 unknown tool，一律不要尝试调。
> 弧+情节段与正文由你自主生成，生成后调薄工具落盘。定义与契约见 NOVEL_AGENT.md 1.1/1.2。

## 前置检查（一次 get_writing_context 完成）
- 调一次 `get_writing_context(book_id)` → 返回 书(含 tags)/storyline(含 phase)/章摘要/draft/next_plot/plot_run/planning/style_card。
- 读 `planning.boundary`：`needs_replan=true` 且已无可写 plot → 收尾后输出 `[NEED_REPLAN]` 交接（见下），先延伸再写。
- **phase≠ready（config/plots/无故事线）**：写作工具被 phase 门控拒绝，**不要硬调**。如实告诉用户：本书停在 `config/plots`，补弧/确认在 write profile 之外——请走建书向导、书详情「✅ 确认弧+情节段」或「续规划」路径完成后，再回来写正文。
- 看 `current_chapter` 与 draft 定位续写点。

## 写作上下文（单次读取；人物/情节/风格三条链分开）
- **每情节段运行**：先 `get_writing_context(book_id)`（内含 `next_plot` 与 `plot_run`；`plot_run.run` 给你 `id`(plot_id@based_revision) 与 `based_on_storyline_revision`）。
- **风格（怎么写）**：动笔前 `get_pen_style(book_id, no_ref=True)` 拿全量风格逐条遵守；`plot_run.style_query` 已推导场景 → 调一次 `pick_plot_sample(book_id)` 取本段**唯一**单篇样文参考。样文用于语言惯性，不总结/不套模板。
- **人物（谁在写）**：`plot_run.cast_pack` 的 protagonists/active；`dyn.*`/`behavior.*`/`speech_profile` 决定一贯反应与对白，别漂移。风格不放 plot_run 判剧情。

## 生成 → 落盘（每次只写一段）
1. 用 `next_plot`（第一个未写情节段）→ **你自主生成正文**。
2. 生成正文的**同时**产出结构化结果（Runtime Control：平台不从正文推断语义，你没上报就没有 facts）：
   - `character_events`：本段造成的人物变化 `[{name, events:[{type,from?,to?,reason?}]}]`（type∈goal_shift|power_shift|location_shift|arc_stage|relationship|trust_change|note）；
   - `outcome`：`{choices_made[], information_revealed[], relationship_changes[], resource_changes[], promise_updates[], new_story_questions[]}`；
   - `expected_facts`（可选，写前可机器比较的预测）：`[{subject, type, expected_to, strength: must|likely|possible}]`，供 commit 后 reconcile 对照；
   - `run_id` / `based_on_storyline_revision`：用 `plot_run.run` 里给的值（省略则系统按 plot_id@当前 revision 派生）。
3. `save_plot_draft(book_id, chapter_num=N, plot_id, plot_name, text, character_events=…, outcome=…, expected_facts=…, run_id=…, based_on_storyline_revision=…)` 落草稿。
4. 每段自查（语气/伏笔/错词）后继续下一段；样文备查、弧/详情只读等能力都不在本 profile——**只用本文件列出的六个工具**。

## 章满收尾 → save_chapter_text（提交即 reconcile）
- 字数达标 → 生成章节摘要，拼全文调：
  `save_chapter_text(book_id, chapter_num=N, text=全文, title=「第N章」, summary=摘要, plot_segments=[{plot_id, plot_name, text}, ...])`
  （可选 `planning_patch` 章末上报 reader_question/人物意图；`expected_revision` 版本 CAS。）
- 返回含 `reconcile`（Prediction→Fact 对照：kind ∈ clean/prediction_drift/missed_prediction/unpredicted_fact/stale）。**以实际为准**：若正文走向与 expected_facts 不符，不要改正文去迎合预测，系统会把 Fact 记入 planning 供下次规划。
- 门禁：`chapter_quality_gate(book_id)` 跑五项；问题作决策点只报告。

## 边界交接（不要在本轮里补弧）
- 若 `get_writing_context` 返回 `planning.boundary.needs_replan=true`（余量 ≤2 plot 或字数不足一批）：把当前进行中的情节段写完并 `save_chapter_text` 收章，本轮**不再开新 plot**、**不调任何补弧/replan 工具**。在最终回复**末尾独占一行**输出：
  `[NEED_REPLAN] book_id=<书id> reason=<reason_codes 以;连接>`
  系统会按 `REPLAN_POLICY`（auto 自动原子提交续写 / confirm 停预览等确认）spawn replan；**不要替系统做 replan**。

## 退出状态 / 失败处置
- 情节段写完=save_plot_draft 落草稿；整章写完=save_chapter_text 返回 chapter/word_count/review/reconcile。
- phase 门控拒绝（config/plots）→ 按「前置检查」引导，勿硬调。`BookBusyError` → 稍后重试。生成空/报错 → 查模型/max_tokens；save_chapter_text 失败 → 草稿还在，检查后重试。
