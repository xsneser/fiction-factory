---
name: novel-write
description: >-
  [已废弃] 不要按本 skill 写作——它引用已被移除的工具(get_writing_context / 旧 save_plot_draft 位置参数)。
  交互式写作请走侧栏 dsh 的 novel-story（一次性 Plot Writer + 服务端 FSM：prepare_plot_run →
  save_plot_draft → 即停；完整章节=服务端 _writer_fsm 逐 Plot 编排，章满收章/门禁/续规划交接全在服务端）。
  仅当用户明确要求查看历史写作流程存档时才加载本 skill。
---
# 写作阶段（novel-write）— 已废弃，写作走 dsh novel-story

> ⚠️ **已废弃（2026-09）**：Claude Code 交互侧不再直接写作。正文写作统一由侧栏 dsh 的
> `novel-story`（一次性 Plot Writer，write profile=2 工具）执行：`prepare_plot_run`（服务端已解析
> style.card/.sample + commit_token）→ 写当前 Plot → `save_plot_draft(commit_token, text, plot_summary,
> outcome, character_events)` → 即停。逐 Plot 推进、章满收章 + 质量门禁 + 到规划边界的续规划交接
> 全由服务端 `_writer_fsm` → `finalize_draft_chapter` 完成，**不要再自行 save_chapter_text 或输出
> [NEED_REPLAN]**。
>
> 下方为历史实现留档：引用已从注册表移除的 `get_writing_context`、已被 token 绑定签名取代的
> `save_plot_draft(book_id, chapter_num, plot_id, …)`、以及 write profile 不再暴露的
> `get_pen_style` / `pick_plot_sample` / `save_chapter_text`。**仅存档，不更新、勿按其写作。**

> **核心原则**：正文由你（agent）**自主生成**——你带着完整对话上下文（书设定/故事线/刚写的前一段/
> 风格要求）逐情节段写，生成后调用**薄工具** `save_plot_draft` / `save_chapter_text` 落盘。
> **不要**调用内部跑 LLM 的旧工具（`write_next_bridge` / `write_chapter`，已废弃留档）。

## 前置检查（必做）
1. `mcp__novel-engine__get_book_state(book_id)`：
   - `phase=config`（缺弧）→ 提示先经建书向导/dsh 落弧（本 skill 不排弧）。
   - `phase=plots`（config 补弧后/遗留恢复；正常新书提交即 ready，不经此）→ 提示用户在书详情页「✅ 确认弧+情节段」进 ready 再写（agent 无翻 ready 工具）。
   - `phase=ready` → 继续；看 `current_chapter` 与进行中草稿（draft）定位续写点。
2. `mcp__novel-engine__get_storyline(book_id)` → 当前弧、下一个待写情节段（plot_id/名称/写在哪章）。

## 上下文组装（渐进式披露 — **单次读取**，不要把整本书灌进上下文）
1. **一次** `mcp__novel-engine__get_writing_context(book_id)` → 返回 `{book(含 tags), storyline 全量, outline, chapters(最近摘要), draft, synopsis, protagonist, next_plot, style_card}`——含角色/世界观/基调/pov/下一个待写情节段/精简风格卡，一次拿全。
2. 整理成「写哪个情节段（next_plot）+ 出场角色 + 风格要求 + 前文语气」，然后**自己生成正文**。
3. **逐情节段循环里每轮只重取一次 `get_writing_context`**（draft/written_chapter 会变）；**不要**再单独调 `get_book_detail` / `get_storyline`（它们是 get_writing_context 的子集/重叠）。

## 规划边界（boundary → replan 交接）
- 每轮读 `get_writing_context` 时看 `planning.boundary.needs_replan`。若 `true` 且已无可写承诺 plot（`next_plot` 空或余量 ≤2）：**写完当前进行中情节段并 `save_chapter_text` 收章后停止**，加载 `novel-replan` skill 延伸下一批 committed；不得写到已承诺区之外，**不得越过 `save_outlines` 自行扩弧**。
- Claude 会话（本工具面全量）内直接走 `.claude/skills/novel-replan`；若你是 dsh 侧栏里写作，则由 orchestrator 在检测到边界时自动接管（无需你动手）。

## 写作规则（生成时内嵌到你的思考）
- **笔名风格强约束（必读必遵）**：动笔前先 `mcp__novel-engine__get_pen_style(book_id, no_ref=True)` 拿该笔名**全量风格**（句式风格 + 禁止内容 + 语言习惯 + 通用纪律；no_ref=只取规则/负约束，单篇样文由 `pick_plot_sample` 给），逐条遵守；每轮 `get_writing_context` 返回的 `style_card` 是**精简风格提醒（必读，防风格漂移）**。**未拿到风格不得写正文**；被裁剪/信息不足时用 `get_pen_style` 重读（独立薄工具，不纠缠全量上下文）。单篇样文（`pick_plot_sample` 返回，见下）是**最高风格来源**：直接参考其语言惯性/叙述距离/信息组织/对白衔接继续创作，**不总结、不抽公式、不套模板**；md 原则与规则只作负约束。样文 = 全局样文库（多维权表：scene/dramatic_state/narrative_action/cast/dialogue_density/information_density/pace/pov，英文键存）。**每情节段运行：先 `get_writing_context`（`plot_run.style_query` 已按情节段内容自动推导 scene/cast 等；线程/承诺不参与选样）→ 调一次 `pick_plot_sample(book_id)`，返回的 `text` 就是本段**唯一** STYLE REFERENCE 单篇样文（含 `# 场景:` 头）**——服务端按该 query 硬过滤→软加权→加权随机→近期避重抽恰 1 篇，连续情节段自动避重、语言参考随运行自然漂移；确需覆盖可给 `pick_plot_sample` 传 `query`，或该场景其余样文用 `get_style_sample` 拉全文备查。
- **一致性铁律**：人名/系统绑定/数值/设定不得与已写冲突；前后呼应伏笔。
- **视角铁律**：全书统一（默认第三人称），不漂移。
- **人物（plot_run.cast_pack 决定「谁在写」，三层不混）**：本情节段以 `plot_run.cast_pack` 的 `protagonists`/`active` 为出场基准（referenced 仅提及，不给行为卡）。`behavior.*`（decision/communication/emotion）决定该角色在压力/危险/背叛/对陌生人/朋友/敌人/愤怒等情境下**一贯反应**——别因场景氛围漂移性格；`speech_profile` 决定语气/句长/句式倾向与 `forbidden` 禁说——按倾向生成自然对白，**不固定复读口头禅、不当表情包**（`catchphrase` 仅在情绪高点一次点缀）。`dyn.*`（goal/relationship/arc_stage 等）是前文演进结果，本段须与之一致。
- **人物被剧情改变（character_events 记账）**：本段若发生改变角色的情节（关系/目标/实力/位置/弧阶段）→ 先在正文有行为表现，再随 `save_plot_draft(character_events=[{name, events:[{type,from?,to?,reason?}]}])` 上报，让角色被事件改变、跨章连续；`type ∈ goal_shift|power_shift|location_shift|arc_stage|trust_change|relationship|note`。**只报剧情造成的**变化**，不报 mood/secret/conflict 等推断量**（防 agent 自报污染人物）。
- **语言纪律**：禁 AI 味句式（仿佛/似乎/不禁/只见 堆叠），少用破折号，对话占比自然——以 `get_pen_style` 拿到的笔名规则（句式风格/禁止内容）为准。
- **前三章开篇钩子**（第 1-3 章）：首句强钩子、三章内出第一个爽点、章末留钩。
- **每章字数**：约 `words_per_chapter`（get_book_state 看）；一情节段 800-2500 字。
- **章末钩子**：每章最后一句留悬念/反转/爽点，保追读。

## 生成 → 落盘（逐情节段）
1. 用 `get_writing_context` 的 `next_plot`（第一个未写情节段）→ **你自主生成该情节段正文**
   （你的 LLM 直接产出，上下文连续）。
2. 生成完调用 `mcp__novel-engine__save_plot_draft(book_id, chapter_num=N, plot_id=..., plot_name=..., text=..., character_events=?)`
   落盘到进行中草稿（断点续写保底）。本情节段发生改变角色的剧情时传 `character_events=[{name, events:[{type,from?,to?,reason?}]}]`（type ∈ goal_shift|power_shift|location_shift|arc_stage|trust_change|relationship|note；不报 mood/secret）。
3. 继续下一情节段；每写完一情节段按需自我核查（语气连贯/伏笔/错词），有问题就地重写再落盘。

## 章满收尾 → save_chapter_text
- 累计字数 ≥ `words_per_chapter`（或故事线该章情节段写完）→ **你生成章节语义摘要**（80-150 字，
  供长程记忆），把本章全部情节段拼成完整正文，调用：
  `mcp__novel-engine__save_chapter_text(book_id, chapter_num=N, text=完整正文, title=「第N章」,
   summary=你生成的摘要, plot_segments=[{plot_id, plot_name, text}, ...])`
  ——它负责规则去AI味/审查/角色状态/承诺台账/书进度并落盘（内部不调 LLM）。
- `save_chapter_text` 返回 `word_count`/`review`；有问题可用 `chapter_quality_gate` 复核（只报告不修复）。

## 完整章节构建流水线（一键写完整章 / 完整章节构建）
1. **写**：逐情节段「你生成正文 → save_plot_draft」直到本满章 → save_chapter_text。
2. **门禁**：`mcp__novel-engine__chapter_quality_gate(book_id, chapter_num=0)` 跑审查/连续性/追读/伏笔/爽点。
3. **汇报（只报告不修复）**：读 `summary` + `checks.*.passed` + `decision_points`；
   `passed=false` → 逐条列给用户定夺。

## 退出状态
- 情节段写完：`save_plot_draft` 落盘草稿；整章写完：`save_chapter_text` 返回 `chapter`/`word_count`/`review`。
- 全书写完 → 引导 `novel-publish`（save_book_meta → publish_check → publish_book）。

## 失败处置
- phase 不过（config 缺弧 / plots 待确认）→ 按前置检查处理：config 引导落弧，plots 引导书详情确认进 ready，勿硬写。
- `BookBusyError` → 稍后重试。`budget_paused` → 停，问用户。
- 生成内容为空/报错 → 检查 `api.json` 模型配置与 max_tokens 余量（推理型模型需留足）。
- `save_chapter_text` 保存失败 → 情节段已用 `save_plot_draft` 落盘（草稿还在，可续），检查后重试。
