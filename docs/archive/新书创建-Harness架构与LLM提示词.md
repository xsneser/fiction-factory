# 新书创建：Harness 架构与 LLM 提示词全景

> 日期：2026-08-05。本文档从代码（`libraries/`、`ui/web_blueprints/`）逐文件核对整理，是"启动新书 → 时间线规划 → 一键大纲 → 写作 → 书名/简介"全流程涉及的 **harness 组件**与 **LLM 提示词**的唯一汇总。
> 前置背景见《harness重构交接文档.md》（笑点涌现 / 内涵跟随桥段 / 语义摘要 / 集中式 prompt 的设计决策）。本文不复述设计史，只给"现在长什么样、每个提示词在哪、参数多少"。

---

## 0. 一句话架构

**新书创建 = 规划（时间线编辑器，产出 `BookTimeline` JSON）+ 写作（写作台，桥段驱动逐章）。**
所有 LLM 调用统一走 `core/llm_client.py` 的 `LLMClient`（DeepSeek，`deepseek-v4-flash`）；提示词由 `PromptHarness`（集中式）为主、各生成器内联为辅。笑点不写进大纲（写作时探测器涌现），内涵跟随桥段挂载，跨章长程记忆 = LLM 语义摘要。

---

## 1. 新书创建流程（端到端调用链）

```
┌─ 阶段 A：规划（时间线编辑器，产出 timeline.json）──────────────────────┐
│  1. POST /books/start            dashboard.start_new_book          │
│     → 建 BookTimeline(phase=config) → 存 books/timelines/tl_*.json │
│     → 若给了 timeline_hint，先 TimelineBuilder.build_outline_sequence│
│       （可走 AI，Prompt B'）                                        │
│  2. GET /timeline/<id>/edit      timeline.timeline_edit           │
│  3. POST /api/timeline/<id>/save-basic-info     （用户填/改基础设定）│
│  4. POST /api/timeline/<id>/generate-full      timeline.api_generate_full
│     → OutlineGenerator.generate（6 阶段 SSE，Prompt A/B/C/D/E/F）  │
│     → on_save 每阶段结束落盘 timeline.json                          │
│  5. 可选：/api/timeline/<id>/agent      OutlineAgent（Prompt L）    │
│     /extend-outline /generate-title（Prompt G）                     │
└──────────────────────────────────────────────────────────────────┘
┌─ 阶段 B：写作（写作台，桥段驱动）─────────────────────────────────────┐
│  6. GET /books/start/timeline/<id>/write  desk.timeline_start_writing│
│     → NovelEngine.start_new_book_timeline(tl, config)               │
│       · 构建 PromptHarness + GagInjector + TimelineChapterWriter    │
│       · BookManager.create → books/book_0xx/（book.json + timeline.json）│
│  7. POST /api/timeline-engine/<id>/write-bridge  （SSE，核心：按桥段撰写）│
│     → engine._write_next_bridge_stream → TimelineChapterWriter.write_bridge_stepwise
│       → render_bridge_prompt（Prompt I）+ GagInjector.detect（Prompt J）
│  8. POST /api/timeline-engine/<id>/write-chapter（SSE，整章：兼容路径）│
│     → _write_timeline_chapter_stream → write_chapter_stepwise       │
│  9. 每章写完 → _finalize_written_chapter                            │
│       · DeAIEngine.process_rule_based（免费规则去 AI 味）            │
│       · _summarize_chapter（Prompt K，长程记忆）                     │
│  10. 第 1 章写完 → _generate_book_meta → book_meta（Prompt G/H）      │
└──────────────────────────────────────────────────────────────────┘
```

路由/蓝图文件：`ui/web_blueprints/dashboard.py`、`timeline.py`、`desk.py`、`books.py`、`ctx.py`（共享服务与时间线存取）。

---

## 2. Harness 组件清单

| 组件 | 文件 | 职责 |
|---|---|---|
| `PromptHarness` | `libraries/prompt_harness.py` | **集中式提示词出口**：书级设定卡（Book Bible）+ 6 个渲染器 |
| `GagInjector` | `libraries/gag_injector.py` | 笑点探测器环（写作时涌现） |
| `OutlineGenerator` | `libraries/outline_generator.py` | 一键完整大纲：6 阶段 LLM 管线（SSE） |
| `OutlineAgent` | `libraries/outline_agent.py` | 大纲助手：自然语言改配置 |
| `TimelineBuilder` | `libraries/timeline.py` | 大纲序列/桥段填充（规则或 AI） |
| `TimelineChapterWriter` | `libraries/timeline_writer.py` | 唯一写作核心：桥段驱动逐章 |
| `NovelEngine` | `libraries/engine.py` | 总调度：启动新书 / 续写 / 章节收尾 |
| `DeAIEngine` | `libraries/de_ai.py` | 去 AI 味（免费规则层） |
| `LLMClient` | `core/llm_client.py` | DeepSeek API 客户端（同步 + 流式） |
| 四大库 | `structure/plot/gag/theme.py` | 大纲模板 / 桥段模板 / 笑点模式 / 母题（候选池） |
| `ProfileManager` | `libraries/profiles.py` | 笔名风格档案（注入风格 bullet） |
| 数据模型 | `libraries/timeline.py` | `BookTimeline` / `OutlineSlot` / `PlotSlot` |

### 2.1 PromptHarness 六个渲染出口（`prompt_harness.py`）

| 方法 | 场景 | 返回 |
|---|---|---|
| `build_book_bible(max_chars=1200)` | 大纲 Phase 2/5 前置设定卡 | 全量设定卡（主角→世界观→风格→视角→时代语言→时间纪律→配角→母题→基调，按优先级截断） |
| `build_book_bible_condensed(max_chars=600)` | 写作 / 探测器 / Phase 3 前置 | 精简版（主角+世界观+风格+视角+时代语言+时间纪律+母题，目标 ~440 字） |
| `render_bridge_prompt(...)` | 桥段写作（Prompt I） | user prompt 字符串 |
| `render_detector_prompt(...)` | 笑点探测器（Prompt J） | `{"system","user"}` |
| `render_summary_prompt(...)` | 章节语义摘要（Prompt K） | `{"system","user"}` |
| `render_outline_context(phase_kind, tl)` | 大纲各 phase 前置设定卡 | 要拼到大纲 prompt 开头的文本块 |
| `prescreen_gag_pool(plot, book_id)` | 候选笑点池免费规则预筛 | ≤6 个 GagPattern |

**书级设定卡分段**（每个 `_xxx_bullets` 方法）：主角 `_protagonist_bullets` / 世界观 `_world_bullets` / 风格 `_style_bullets`（笔名档案 `_profile_style_text`，支持 `PenNameProfile` 对象与 dict 两种形态）/ 视角 `_pov_bullets` / 时代语言 `_era_language_bullets`（era 年份 ≤2015 自动禁网络新词）/ 重生时间纪律 `_rebirth_time_bullets` / 配角 `_supporting_cast_bullets` / 母题 `_theme_bullets` / 基调 `_tone_bullets`。

**两条全书铁律**（模块级常量，注入写作）：
- `OPENING_MODE_RULES` — 炸裂开场：冷开场三句入冲突、前 200 字钩子、前 500 字危机、冲突线前置。
- `CONSISTENCY_RULES` — 全书一致性：系统绑定全书只一次、数值必须闭环、对话时间线不穿帮、跨天要有时间过渡。

### 2.2 GagInjector 探测环（`gag_injector.py`）

```
prescreen_pool(plot, book_id) → harness.prescreen_gag_pool
detect(item, recent_text, humor_style, pool)
  → 调用 render_detector_prompt（Prompt J）
  → 解析失败/缺字段/越权模式 → 一律未命中（静默）
  → 命中（has_opportunity && gag_ids⊆池 && deploy_hint 非空）
build_inspiration_hint(hit, pool) → 「用主角口吻、一句收进当前场景、不解释」落点纪律
```

集成点：`timeline_writer._write_plot_segment_groups` 每写完一组短句、预算未用尽（`remaining-words>100`）时跑一次，命中则把 hint 注入**下一组**写作 prompt，用完即清。

### 2.3 数据模型关键字段（`timeline.py`）

- `BookTimeline`：`basic_info`（主角/世界观/配角/基调/视角/时代语言）、`outlines[]`、`plots[]`、`threads[]`、`themes[]`、`phase`（config→outlines→plots→gags→ready）。
- `OutlineSlot`：模板 id、章节范围、stages、transition_type（sequential/overlap/merge）、narrative（chronological/flashback/interleaved）。
- `PlotSlot`：模板 id、category、大纲/阶段定位、`cover_beats`（字数规划）、`template_structure`、`slots`、`gag_ids`（**保留字段但不再写入**）、`theme_hints`、`hook_points`、`thread_id/seq`、`resolves_plot_id/name`（设局→收局）、`roles`（出场人物）、`written_chapter`（断点续写）。

---

## 3. LLM 提示词清单（逐个）

> 所有调用的 system prompt 都是"只返回 JSON"类约束；`extract_json`（`core/llm_client.py`）负责从带前后文的输出中抠 JSON。以下按调用顺序编号。

### Prompt A — 故事分析（大纲 Phase 1，`outline_generator._analyze_story`）

- **调用**：`llm.call(system, prompt, temperature=0.7, max_tokens=2048)`，非流式。
- **输入**：流派/子流派 + 每章目标 + 笔名风格偏好（句长/幽默/视角偏好）+ 用户想法。
- **要求**：主角（名/身份/性格/背景/金手指/性别/年龄/死亡年份）、世界观（时代年份/力量体系/势力 2-4/世界规则——数值语义写死防每章换解释）、基调、目标读者、视角（默认第三人称禁漂移）、配角 2-3（含惯用语句/简介）、时代语言约束、世界规则。
- **返回 JSON**：`protagonist / world_building / supporting_cast / tone / target_audience / pov / era_language`。
- **兜底**：解析失败 → `_default_basic_info(genre)`；`genre=="custom"` 且无 custom_context → 同兜底。
- **Phase 1 后规则校验**：`_validate_timeline_math`（重生死亡年份 < 故事年份、年龄/年份自洽）纯规则不调 LLM。

### Prompt B — 故事线规划（Phase 2，`_ai_sequence`；`render_outline_context("sequence")` 前置设定卡）

- **调用**：`_stream_decision_content`（流式），temp 0.7，max_tokens 8192。
- **输入**：书级设定卡（全量 ≤900 字）+ 主角/世界观 + 用户想法 + 候选大纲模板（≤10 条，结构库 `structure_lib.search(genre, sub_genre)`）。
- **要求**：选 2-max_outlines 个模板按时间线串联，可重叠 2-5 章（transition_type=overlap），体现"开局爽→中段稳→高潮燃"。
- **返回 JSON**：`outlines[{template_id,name,start_chapter,end_chapter,transition_type,reason}]`。
- **兜底**：解析失败/无候选 → `_rule_sequence`（顺序取前 N 个模板）。
- **旁路**：`TimelineBuilder._ai_build_sequence`（Prompt B'，`timeline.py`）是启动时给了 timeline_hint 才走的旧入口，返回结构相同但无书级设定卡、无 decision 事件。

### Prompt C — 桥段选择（Phase 3，`_ai_select_plots`；`render_outline_context("select_plots")` 前置精简卡）

- **调用**：`_stream_decision_content`（流式），temp 0.5，max_tokens 8192。
- **输入**：书级设定卡（精简 ≤500）+ 大纲名/阶段名 + 阶段事件 + 流派 + 候选桥段（`_collect_plot_candidates` 聚合上下文/流派/事件关键词，≤12）+ 已用桥段避免重复。
- **要求**：每阶段选 1-3 个，类型须契合弧线主题（开篇用钩子类、悬疑用推理类），避免爽文桥段乱入正剧/悬疑弧，避免重复、可递进。
- **返回 JSON**：`plot_ids[], reason`。
- **兜底**：无 LLM 或解析失败 → 规则取 `candidates[:2]`。
- **每个桥段展开**：`cover_beats = template.word_range[1]//400`；模板的 slots 展开为变量槽位；链式嵌套（后一个成为前一个的子桥段）。

### Prompt D — 线程与呼应（Phase 4，`_plan_threads_and_splits`；`render_outline_context("thread_split")` 前置精简卡）

- **调用**：`llm.call(...)` 非流式，temp 0.3，**max_tokens 16384**（实测 8192 会被推理吃满导致 content 空）；空返回/解析失败重试 3 次后回退规则。
- **输入**：书级设定卡（精简）+ 大纲 JSON + 全部桥段 JSON（按大纲顺序 ≤60 个）+ 线程定义说明（主线/副线/伏笔线，第 1 章附近多线程并进）。
- **要求**：每条桥段归线程；选中适合"设局→收局"的桥段（阴谋/悬疑/智斗/成长转折）生成收局槽位放后几个 stage（payoff_after_stage≥2），纯即时爽点不拆。
- **返回 JSON**：`threads[{id,name,desc}] / assignments[{plot_id,thread,seq}] / splits[{plot_id,payoff_after_stage,payoff_name,payoff_thread}] / reason`。
- **落库**：`_apply_thread_assignments`（英文 id 规范化为中文）+ `_apply_split_payoffs`（新建收局 PlotSlot，`resolves_plot_id`）。
- **兜底**：`_apply_thread_fallback`（按桥段分类归线程：悬疑→伏笔阴谋线、情感日常→副线、其余→主线）。

### Prompt E — 内涵挂载复查（Phase 4.5，`_review_theme_assignments`）

- **调用**：`_stream_decision_content`（流式），temp 0.3，max_tokens 8192。
- **输入**：桥段快照（id/name/category/template_id/theme_hints[:2]）≤20 个。
- **要求**：复查母题是否挂到能承载它的桥段、分布是否均匀、有无硬挂。
- **返回 JSON**：`corrections[{plot_id,theme_hints[],reason}] / summary`。
- **落库**：只接受书级母题库（`tl.themes`）内的名字，每桥段 ≤2。

### Prompt F — 一致性验证 LLM（Phase 5，`_validate_with_llm`）

- **调用**：`_stream_decision_content`（流式），temp 0.3，max_tokens 8192。
- **输入**：大纲视图（name/range/transition/stages）≤10 条 + 流派 + 桥段总数。
- **要求**：检查时间线重叠/间隔、桥段覆盖、明显漏洞。
- **返回 JSON**：`issues[], summary`。
- **规则校验先行**：`_validate`（章节连续性、重叠区 ≤5 章、桥段覆盖率、内涵覆盖率 ≥30%、总章节 10-500）纯规则不调 LLM。

### Prompt G — 书名生成（`timeline.generate_title` / `engine._generate_book_meta`）

- **调用**：temp 0.8，max_tokens 1024，非流式。
- **输入**（`book_meta.build_title_prompt`）：流派/子流派/平台 + 第 1 章正文前 1000 字；要求 5 个备选书名（4-10 字，有网感，慎用"之/录/传"）+ best + reason。
- **返回 JSON**：`titles[] / best / reason`。

### Prompt H — 简介生成（`engine._generate_book_meta`）

- **调用**：temp 0.8，max_tokens 1024，非流式。
- **输入**（`book_meta.build_synopsis_prompt`）：流派/平台 + 第 1 章正文前 1000 字；要求 100-200 字、吸睛有钩子、不剧透。
- **返回 JSON**：`synopsis`。

### Prompt I — 桥段写作（`render_bridge_prompt`，核心写作提示词）

- **调用方**：`TimelineChapterWriter._group_prompt`（有 harness 恒走本渲染器；无 harness 才回退内联极简模板）。
- **system**：沿用 `timeline_writer` 写作铁律（画面优先/短句基干句长交错/对话独立成段/视角锁主角/严禁词表 12 词）。
- **user 结构**：
  - 书级设定卡（精简版）
  - 开场模式铁律（`is_opening=True`，第 1 章前 800 字/前 3 桥段命中）
  - 全书一致性铁律
  - 视角铁律（第一/第三人称显式重申）
  - 所属大纲 + 当前阶段 + 本桥段要推动的事件 + 桥段骨架 + 变量槽位
  - 本桥段出场人物（`_roles_block`：性别/性格/口头禅/简介，防"她"字错误、保持声线）
  - 母题（`theme_hints`，有才出现，措辞"从情节自然流露、不直白点题、不加括号注解"）
  - 收局/设局槽位（`resolves_plot_id` → 收束；`resolver_name` → 埋钩子）
  - 灵机一动（探测器命中才注入，用完即清）
  - 已完成章节语义摘要（有才出现）
  - 前文上下文（上一章结尾[-150] / 本章已写[-900] / 本桥段已写[-300] / 角色当前状态 ≤500）
  - 写作要求 7 条（3-5 句约 150-250 字、一句一行、画面优先、组尾悬念、视角统一、严禁词表、预算控制）
- **调用参数**：`llm.call(system, user, temperature=0.7, max_tokens=1600)`；空响应/连续重复词/疑似错词各重试 `WRITER_EMPTY_RETRIES=2` 次。

### Prompt J — 笑点探测器（`render_detector_prompt`，`GagInjector.detect`）

- **调用**：temp 0.3，max_tokens 400，非流式。
- **system**：你是中文网文主角的「喜剧嗅觉探测器」。只返回 JSON。
- **user**：判断刚写正文里下一句顺势落笑点是否"天然、不硬凑"。
  - 主角喜剧声线（笔名档案 `style_fingerprint.humor_style`）
  - 候选笑点模式池（≤4 条：`[id] name：pattern_description[:60]`）
  - 刚写好的正文（本组+前 1-2 组，`recent_text[-450:]`）
  - 判断铁律：只在对话刚结束/动作刚发生/情绪高点/天然落差处认为有戏；**没有天然缝隙绝不硬造，宁可错过不可硬塞（最高优先级）**；gag_ids 最多 1 个且必须选自候选池。
- **返回 JSON**：`has_opportunity / gag_ids[] / reason / deploy_hint`。
- **纪律**：deploy_hint 空或 >60 字丢弃；越权模式（gag_ids⊄池）视为未命中。

### Prompt K — 章节语义摘要（`render_summary_prompt`，`engine._summarize_chapter`）

- **调用**：temp 0.3，max_tokens 1024，每章 1 次。
- **输入**：本桥段名（bridge_line）+ 本章内容尾部 `content_tail[-600:]`。
- **要求**：80-150 字客观摘要（发生了什么/主角状态/埋的钩子），不评价文笔。
- **返回 JSON**：`summary`。
- **落库**：`chapters/{NNNN}.json.summary`；写作前注入最近 5 章（`load_chapter_summaries`）作为长程记忆。

### Prompt L — 大纲助手意图解析（`OutlineAgent._parse`）

- **调用**：temp 0.2，max_tokens 2048，非流式。
- **输入**：当前故事线上下文（大纲列表+桥段列表，序号即"第几个"）+ 用户口语指令。
- **返回 JSON**：`intent / target_index / target_name / new_fields / gags / remove_gag_ids / new_plot / outline_index / reply`。
- **intent**：modify_plot / add_gag / remove_gag / add_plot / remove_plot / modify_outline / general。
- **兜底**：解析失败 → general + 帮助文本。

---

## 4. Token 预算与参数约定（实测校准）

> ⚠️ **核心坑**：`deepseek-v4-flash` 先输出 `reasoning_content` 再输出 `content`，两者共享 `max_tokens`。max_tokens 过小（如 700）推理吃光预算 → content 为空。因此所有 max_tokens 都留足推理余量，写作路径对空响应重试 2 次。

| 提示词 | 输入规模 | max_tokens | temp | 频率 |
|---|---|---|---|---|
| A 故事分析 | 用户想法+风格 | 2048 | 0.7 | 每新书 1 次 |
| B 故事线规划 | 设定卡≤900+候选模板 | 8192 | 0.7 | 每新书 1 次 |
| C 桥段选择 | 设定卡精简+候选≤12 | 8192 | 0.5 | 每阶段 1 次 |
| D 线程与呼应 | 设定卡+大纲+桥段≤60 | **16384** | 0.3 | 每新书 1 次 |
| E 内涵复查 | 桥段≤20 | 8192 | 0.3 | 每新书 1 次 |
| F 一致性验证 | 大纲≤10 | 8192 | 0.3 | 每新书 1 次 |
| G 书名 | 第 1 章前 1000 字 | 1024 | 0.8 | 第 1 章写完 1 次 |
| H 简介 | 第 1 章前 1000 字 | 1024 | 0.8 | 第 1 章写完 1 次 |
| I 桥段写作 | 设定卡精简+计划+摘要+前文窗口 | **1600** | 0.7 | 每短句组 |
| J 笑点探测器 | ≤500 token（recent 450 字+池 4×60） | **400** | 0.3 | 每短句组 |
| K 语义摘要 | ≤500 token（content 尾 600 字） | 1024 | 0.3 | 每章 1 次 |
| L 大纲助手 | 上下文+指令 | 2048 | 0.2 | 用户触发 |

探测器/摘要调用输入 ≤500 token（字符数 ≤约 750）、输出 ≤256 —— 这是 harness 的硬约定（`prompt_harness.py` 头部注释）。

---

## 5. 数据落盘位置

| 文件 | 内容 | 写入时机 |
|---|---|---|
| `books/timelines/tl_*.json` | 时间线草稿（未建书的规划中） | 启动新书 / 编辑器各动作 |
| `books/book_0xx/book.json` | 正式书配置（标题/笔名/流派/预算/detector_frequency/进度） | `BookManager.create/update` |
| `books/book_0xx/timeline.json` | 正式书时间线（大纲+桥段+threads+themes） | 每阶段结束 / 每桥段写完 |
| `books/book_0xx/outline/outline.json` | 结构大纲（含 synopsis） | 第 1 章写完 |
| `books/book_0xx/chapters/{NNNN}.json` | 章节正文 + summary | 每章固化 |
| `books/book_0xx/draft_chapter.json` | 进行中章节草稿（按桥段撰写中断恢复） | 每桥段写完未切章时 |
| `books/book_0xx/character_states.json` | 角色状态机 | 每章收尾 |
| `books/book_0xx/cost.json` | LLM 费用 | 每次调用 |

---

## 6. 与既有文档的关系

- 《harness重构交接文档.md》：设计决策史 + 重构改动清单（笑点涌现等），本文档的"为什么"版本。
- 《项目规划.md》：产品层面规划。
- 本文档：新书创建流程的"现在时"代码事实——组件、提示词、参数、落盘，均标注到文件与行。
- 代码是最终真相：若本文档与代码冲突，以 `libraries/` 下源码为准。
