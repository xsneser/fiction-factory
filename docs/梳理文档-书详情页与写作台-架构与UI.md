# 梳理：书详情页 / 写作台 —— 页面架构 · UI 结构 · 数据链路（供 LLM 研判）

> 日期：2026-09-08　定位：**梳理/速查档**，聚焦两个页面：① 书详情页 `/books/<id>`（book_detail.html）；② 写作台 `/books/<id>/continue`（storyline_write_flow.html）。给大体量模型做 UI 架构分析、组件与数据流研判、以及「读者台账 vs 增量规划」「质量诊断 vs 审查」相似性比对。
> 事实源：代码是最终真相。数据模型 / 弧·情节段·线程关系 / agent·dsh 工具面参照 `docs/梳理文档-故事线关系与角色注入与架构.md`（2026-09-08，本档不重复其模型定义，只补「页面层怎么消费模型」）。**行号为探索时锚点，字段增删以代码为准。**
> 操作面：`ui/templates/`、`ui/web_blueprints/`、`ui/static/js/`、`agent_tools.py`、`libraries/{planning_state,promise_ledger,reviewer,character_state,continuity,retention}.py`。

---

## 一、两个页面的定位（一句话）

- **书详情页** = 书的「档案 + 只读诊断台」：设定/故事线/章节的**查看**入口，叠加规则层运行时面板（角色状态、承诺台账、质量诊断、历史快照），**不写正文**。
- **写作台** = 书的「生产 + 驾驶舱」：左侧 Plot Run + 故事线 Gantt 决定「这一轮写什么、写到哪」，右侧分页阅读器实时展示「Agent 写了什么」，通过侧栏 Agent（dsh）逐情节段续写。
- 两者共享三块：🧭 增量规划面板（_planning_ui.html）、📋 故事线 Gantt（story_line.js）、侧栏 Agent（agent_panel.js）。

### 1.1 路由表

| 页面 | 路由 | 蓝图/模板 | 主数据源 |
|---|---|---|---|
| 书详情 | `GET /books/<book_id>` | `ui/web_blueprints/books.py:139 book_detail` → `book_detail.html` | `book_mgr`（book.json/storyline.json/outline.json/chapters/*.json/draft_chapter.json） |
| 写作台 | `GET /books/<book_id>/continue` | `ui/web_blueprints/desk.py:403 continue_book_page` → 建/取引擎 → 302 `storyline_write_flow` | 同上 + 引擎会话 `_engines[cont_<id>]` |
| 写作台（引擎页） | `GET /books/storyline/write/<engine_id>` | `desk.py:157 storyline_write_flow` → `storyline_write_flow.html` | 引擎 state/storyline/book + 磁盘现读章节 |
| 写作台正文 JSON | `GET /api/desk/chapters/<book_id>` | `desk.py:76 desk_chapters_api` | 磁盘现读（跨进程 stale 免疫） |
| 故事线 Gantt JSON | `GET /api/storyline/<book_id>` | `ui/web_blueprints/storyline.py:110` | `load_storyline` 磁盘直读 |

> 写作台入口链：书库/详情页「✍️ 进入写作台」→ `continue_book_page` 建 `NovelEngine.continue_book(book_id)` 引擎（`_engines[cont_<id>]`）→ 302 到 `storyline_write_flow(engine_id)`。无故事线的旧书给出指引页而非 500（`desk.py:421-431`）。

---

## 二、书详情页（book_detail.html）

### 2.1 数据源（books.py:139-183）

`book_detail()` 组装：`book`（进度/状态/字数）+ `storyline`（BookStoryline，有则 `basic_info` 打 `has_storyline` 标；无则从 outline 尽力还原）+ `outline` + `chapters`（1..current_chapter+1 现读，`word_count` 现算）+ `draft`（draft_chapter.json 的 buffer 拼文本）。页面注入 `window.BOOK_ID / __BI__ / __BOOK_TITLE__ / __BOOK_STORYLINE__`。

### 2.2 UI 结构（自上而下）

| # | 区块 | 内容与交互 |
|---|---|---|
| 1 | 面包屑 | 📚 书库 › 书名 |
| 2 | 书名 h1 | 点击就地编辑（输入框回车/失焦保存 → `POST /api/storyline/<id>/save-basic-info` 同步 book.title + storyline.book_title，`book_detail.html:145-206`） |
| 3 | 顶部操作行 | ✍️ 进入写作台（有 storyline 时，绿）/ 🚀 上架·导出（蓝）/ 删除（confirm 表单） |
| 4 | plots 兜底横幅 | 仅 `phase=='plots'` 出现：「📐 故事线待确认」+ 🔍 预览故事线 + ✅ 确认弧+情节段（→ `POST /api/book/<id>/confirm-storyline`，翻 ready）。注释明示：正常新书提交即 ready，此横幅只剩 config/补弧失败兜底用（`book_detail.html:20-32`） |
| 5 | detail-grid 基本信息 | 笔名 / 题材标签（`world_building.tags` 优先，无则 genre+sub_genre）/ 男频女频（target_audience）/ 基调·视角（tone·pov）/ 进度（total_words 字 · current_chapter 章）/ 状态 badge |
| 6 | 🧭 增量规划面板 | `_planning_ui.html`（planning_write_mode=false）：指标（已写/已承诺/可执行 plots）+ H0/H1/H2 + 开放问题 + 人物意图 + 最近续规划；replan 抽屉「🔭 规划下一段」+ 版本冲突 modal（详见 §四） |
| 7 | 设定编辑表单 | `_world_edit_form.html` 直铺可编辑（人物/世界观/基调等） |
| 8 | 运行时面板 ×4（手风琴） | `_book_runtime_panels.html`：🎭 角色状态 / ⏳ 读者承诺台账 / 🔍 质量诊断 / 🗂 历史快照（详见 §2.3） |
| 9 | 📝 简介 | `basic_info.synopsis` 卡片 |
| 10 | ✍️ 进行中草稿 | 手风琴展示 `draft_chapter.json` 未固化文本（写作台中断保留，只读） |
| 11 | 📑 目录 toc-split | 左=章列表（第 N 章: 标题 + 📊 字数·时间，独立滚动）；右=章节展开（标题/📝 摘要/🔍 审查 tag 分·通过·问题摘要/正文 pre）。审查 tag 来自 `ch.review` 服务端数据 |
| 12 | 📋 故事线 Gantt | `#detail-storyline`（`story_line.js` 挂载），**默认折叠**：容器高度 0，首次点开 `#sl-toggle` 时才 `StoryLine.init`（`book_detail.html:125-143`）；`ne:planning-updated` 后重初始化 |
| 13 | 空态 | 无章节时「✍️ 还没有生成章节 → 进入写作台」 |

### 2.3 运行时面板与 API（竞品规则层组件 UI 化，全部只读/按需算）

| 面板 | 触发 | 端点 | 规则层 |
|---|---|---|---|
| 🎭 角色状态 | 进页自动 | `GET /api/book/<id>/character-states` | `libraries/character_state.py`（`character_states.json` 动态态 + 事件台账；推断字段 conflict/secret/emotional_pressure 黄色高亮区分，禁 agent 直写） |
| ⏳ 读者承诺台账 | 进页自动 | `GET /api/book/<id>/promises` | `libraries/promise_ledger.py:80 scan_promises` → 分组 🔴逾期/🔵推进/⚪停滞/✅近期兑现 + 建议（详见 §六.1） |
| 🔍 质量诊断 | 点「▶ 运行诊断」 | `POST /api/book/<id>/diagnose` | `agent_tools.diagnose_continuity/retention/promises`（**MCP 同源**，注释明示）→ 🧩连续性扫描/📈追读诊断/⏳承诺台账 + 💡建议（详见 §六.2） |
| 🗂 历史快照 | 点「🔄 加载快照」 | `GET /api/book/<id>/snapshots` → `…/diff` → `…/rollback` | `libraries/book_snapshot.py`（写工具落库前自动留底，最多 10 份；回滚前再留底一次保证可逆） |

另有两个章节级规则端点（函数就绪于 `_book_runtime_panels.html`，按 `rp-review-<n>/rp-deai-<n>/rp-tags-<n>` id 挂接，当前 book_detail 无显式按钮——审查结果以服务端 tag 呈现于目录右栏）：

- `POST /api/book/<id>/chapter/<n>/review` → `libraries/reviewer.py ContentReviewer`（单章审查：passed/score/summary/issues[]）
- `POST /api/book/<id>/chapter/<n>/deai` → `libraries/de_ai.py DeAIEngine`（去 AI 味，替换/断句计数）
- `POST /api/book/<id>/chapter/<n>/punch-points` → `libraries/tag_generator.py tag_chapter`（爽点标注，落盘 tags.json）

---

## 三、写作台（storyline_write_flow.html）

### 3.1 数据源（desk.py:157-249）

`storyline_write_flow(engine_id)` 组装：引擎 `state` + `storyline` + `book`（跨进程修正 current_chapter，从磁盘 book.json 现读）+ `chapters`（磁盘已写章节 **+ 进行中草稿**，草稿以 `draft:true` 标记、bridges 逐情节段渲染）+ `total_ch`（字数轴总章数，由 `planned_words` 推导兜底）+ `planning_state / planning_boundary`。页面注入 `__BOOK_STORYLINE__ / __STORYLINE_ID__ / __ENGINE_ID__ / __BOOK_CURRENT_CHAPTER__ / __BOOK_CHAPTERS__`。

### 3.2 UI 结构（自上而下）

| # | 区块 | 内容与交互 |
|---|---|---|
| 1 | 面包屑 + h1 ✍️ 写作台 | 书库 › 书名 › 写作台 |
| 2 | info_bar | 书名 · 笔名 · 进度（第 N 章 · 共 M 字）· 状态 badge · 每章 N 字 |
| 3 | 🧭 增量规划面板 | `planning_write_mode=true`：额外启用顶部 `boundary-banner`（⚠️ 临近规划边界，让 Agent 规划下一段） |
| 4 | 左栏 editor-left | 可折叠（`toggleStorylinePanel`）、可拖拽分栏（20%–70%，localStorage `ne_storyline_w` 持久化，`storyline_write_flow.html:234-266`） |
| 5 | 🎯 Plot Run 面板 | `#wf-plot-run`（标题「当前情节段运行 · 每次只写一段 · 单篇样文参考」）+ `#wf-plot-outcome`（写后对照），数据来自 `/api/desk/chapters/<bid>`（§3.3） |
| 6 | 📋 故事线 Gantt | `#editor-storyline`（story_line.js 挂载，scrollable 模式，含缩放控件） |
| 7 | 右栏 editor-mid | 顶部 ✍️ 继续写正文卡（`runWritingTask` → 侧栏 Agent；运行态 ⏳ 续写运行中… / ⏹ 停止）；下方分页阅读器 |
| 8 | 分页阅读器 | `ReaderCore` 双容器推入动画；章节渲染把正文按情节段包成 `span.m-bridge[data-bridge=plot_id]`（草稿加徽标）；工具栏 📑目录/⬅上一页/「第 N 章 · 第 N 页」/下一页➡；目录抽屉右侧滑出 |
| 9 | 双向高亮 | 点 Gantt 情节段/弧 → 右栏跳页高亮对应正文（`sl:plot-click`/`sl:outline-click` → `highlightBridgeContent`/`highlightOutlineContent`）；agent 高亮 `StoryLine.highlight` 反向 |
| 10 | 轮询 | 进页立即 + 3s 轮询 `/api/desk/chapters/<bid>`：刷新 Plot Run、Prediction→Fact、章节（`Reader.setChapters` 保留当前页/高亮、`_activePid` 恢复） |

### 3.3 🎯 Plot Run 面板逐区块（你问的「这一部分是干什么的」）

渲染函数 `renderPlotRun`（`storyline_write_flow.html:345-380`），数据 = `desk_chapters_api` 里的 `plot_run`，**与 Agent 侧共用同一组装（`agent_tools._build_plot_run`）**——UI 显示什么，Agent 下一步就写什么：

> ⚠️ 历史标注（2026-09-10）：本文写作时 Agent 读的是 `get_writing_context`；现行写 profile 只有
> `prepare_plot_run` / `save_plot_draft` 两个工具（其快照由 prepare 在 `_build_plot_run` 之上再组装），
> 但「UI 与 Agent 同源」这一结论仍然成立。详见 `docs/架构总览.md` §六。

| 区块 | 数据字段 | 含义（示例） |
|---|---|---|
| 📌 当前情节段 | `plot_run.plot` | 这一轮要写的情节段：名称 + `↪ 收局/解决「X」` 徽标 + 分类 · 目标约 N 字 · 出场角色。例：猎头巴雅尔 · 战斗 · 目标约 1900 字 · 出场 秦戈、巴雅尔 |
| 📋 所在弧 | `plot_run.arc_goal` | 弧路径（父弧链 `/` 拼接，`_outline_name_chain`）+ 阶段 + **弧内待写约 N 字**（该弧下所有未写情节段目标字数合计）。例：残炉立锥 / 火枪与矿脉 · 弧内待写约 5700 字 |
| 🧵 所属线程 | `plot_run.thread` | 线程名 + `chain_note`（「`<thread_id>` 已写 X 条 / 未写 Y 条」，X=该线程已写情节段数，Y=未写数，`agent_tools.py:464`）。例：商会与霜脊·盟线 · t_allies 已写 1 条 / 未写 8 条 |
| ✅/⏳ 承诺 | `plot_run.promise_state` | 设局/收局指向本情节段的伏笔：描述 + 已兑现/待兑现 + 约第 N 章 |
| 🎯⚔️🧭🔄❓🪝 执行简报 | `plot_run.execution_brief` | 戏剧目标/冲突来源/人物选择/不可逆变化/读者问题/结尾钩子（情节段自带，写前对齐） |
| 👤 写前人物影响预测 | `plot_run.character_impact` | 本情节段预计造成的人物变化（写前） |
| 👤 Prediction → Fact | `recent_plot_outcome` | 最近写完情节段的「预测 vs 实际 vs 结果」对照（从草稿 bridges 的 `character_events` 现取，`desk.py:107-116`） |

> 一句话：**Plot Run = 「当前这一轮写什么」的驾驶舱**。🎯 指定写哪段，📋 提示弧目标与字数余量，🧵 提示线程进度（已写/未写条数），✅⏳ 提示欠读者什么承诺，底部把「写前预期」和「写后事实」对齐成闭环。

### 3.4 API 面（desk.py）

| 端点 | 用途 |
|---|---|
| `GET /api/desk/chapters/<bid>` | 写作台正文 JSON：chapters（+草稿）+ plot_run + recent_plot_outcome + planning（boundary）；全部磁盘现读 |
| `POST /api/storyline-engine/<eid>/step` | 蓝图引擎：整章一步写（旧路径，按章） |
| `POST /api/storyline-engine/<eid>/write-chapter`（SSE） | 蓝图引擎流式整章（plot_start/plot_done/chapter_done） |
| `POST /api/storyline-engine/<eid>/write-bridge`（SSE） | 蓝图引擎流式**单情节段**（bridge_start/group_chunk/bridge_done/chapter_done/complete） |
| `POST /api/book/<bid>/confirm-storyline` | plots → ready（兜底） |

---

## 四、共享组件：故事线 Gantt 与增量规划面板

### 4.1 故事线 Gantt（story_line.js，三泳道 + 字数轴）

- **入口**：`window.StoryLine.init(mountId, storyline, opts)`（`story_line.js:737`）。opts：`scrollable`（无限长滚动 + 缩放 −/+/默认，0.4–4x）、`planning`（承诺边界/可变未来）、`boundary`、`currentWord`（进度光标 = 已写累计字数）。
- **DOM 结构**（`story_line.js:769-791`）：表头（已承诺 · 每章约 N 字 · 弧/情节段/线程计数）→ 字数轴（刻度对齐）→ 内容区三泳道：`📋 弧`（flex:4）/ `🔗 情节段`（flex:4）/ `🧵 线程`（flex:2）→ 底部「🔭 可变未来」区 + 图例（弧/主次情节段/线程色/◉设局→↪收局/黄线=承诺边界/斜纹=可变未来/倒叙插叙）。
- **数据适配**（`adapt`，`story_line.js:100-237`）：弧按 `start_word/end_word` 0 基字坐标落位（缺则章×WPC 推导、兜底 30 章）；情节段在弧内按 `planned_words` 累计定位；线程横带 = 成员情节段区间并集；设局/收局/承诺徽标按 `resolves_plot_id` 与 `promises[]` 映射。
- **视觉要素**：弧树嵌套（parent 连线 + 层级缩进）、倒叙（琥珀）/插叙（绿）着色 + `narrative_target` ◉、情节段条颜色 = 所属线程色、父子情节段贝塞尔线、**设局→收局金色虚线**（长距离自动衰减透明度）、进度光标、承诺边界黄线（「已承诺至 N」）、H1/H2 意图卡片。
- **点击行为**：弧/情节段条 click → `sl:outline-click` / `sl:plot-click`（bubbles，写作台消费做正文高亮）；tooltip 展示 字数/章号/线程/收局/出场/承诺。

### 4.2 🧭 增量规划面板（planning_ui.js）

- **数据**：`GET /api/storyline/<bid>/planning-state` → `_planning_ui_payload`（`storyline.py:12-63`）：`planning_state`（written/committed 字数、horizon、story_questions、character_intents、last_replan）+ `storyline_snapshot`（revision/各计数/线程/promise_count）+ `boundary`（`detect_story_boundary`）+ `replan_preview`。
- **UI**（`planning_ui.js:26-60`）：指标行（已写/已承诺/可执行 plots）+ 六格（H0 现在执行 / H1 下一段方向 / H2 远期意图 / 开放问题 / 人物意图 / 最近续规划）+ `boundary-banner`（⚠️ 临近规划边界：PLOTS_LOW 剩余情节段不足 / WORDS_LOW 承诺字数耗尽 / PLAN_INVALIDATED / MAJOR_CHARACTER_CHANGE / NEW_HIGH_PRIORITY_QUESTION）。
- **Replan 抽屉**（🔭 规划下一段）：当前诊断 → 候选方向（2-3 个，可点选重生成）→ 待提交情节段 → 预览校验（validation.problems）→ 重拟/丢弃/确认并提交。提交走 `POST /api/storyline/<bid>/commit-plan`（`expected_revision` 乐观并发；stale → 版本冲突 modal 强制刷新重规划）。**预览不提交正式故事线**——planning 与写作是「先预览后确认」的分离管道。

---

## 五、数据链路：Plot Run 的组装（agent_tools.py）

```
写作台轮询 GET /api/desk/chapters/<bid>（desk.py:76）
  └─ 磁盘现读 book.json（current_chapter）+ draft_chapter.json（草稿）
  └─ load_storyline → _next_plot(tl, draft)（agent_tools.py:483）
       = 第一个 written_chapter==0 且不在草稿内的情节段（draft-aware）
  └─ _build_plot_run(tl, p, char_states)（agent_tools.py:387）
       ├─ plot          情节段全量（含 resolves_name/roles/hook_points/is_payoff）
       ├─ arc_goal      所在弧：arc_path（父弧链）/notes/stage/word_span/
       │                words_left_in_arc（弧内未写情节段 words 合计，:433-436）
       ├─ thread        thread 定义 + same_thread_written/same_thread_unwritten
       │                + chain_note「<tid> 已写 X 条 / 未写 Y 条」（:459-465）
       ├─ promise_state 设局/收局指向本情节段的承诺（:467-473）
       ├─ execution_brief / character_impact（情节段自带）
       ├─ style_query   infer_plot_query（服务端判场景 → pick_plot_sample 选样）
       └─ cast_pack     出场角色分级紧凑卡（protagonists/active/referenced，
                        谁在写；dyn 来自 character_states，:288-384）
写作 Agent 侧（历史：get_writing_context；现行：prepare_plot_run 快照）的 plot_run = 同一函数
→ 「UI 显示 = Agent 上下文 = 同一组装」，两处不漂移（所见即所写）
```

---

## 六、供 LLM 研判的相似性观察（现行状态，非结论）

### 6.1 ⏳ 读者承诺台账 vs 🧭 增量规划

| 维度 | 读者承诺台账（promise_ledger.py:80 scan_promises） | 增量规划（planning_state.py + planning_ui.js） |
|---|---|---|
| 输入 | `tl.promises[]` + 全书章节 + current_chapter | storyline + book（written/committed 字数）+ 草稿 ids |
| 输出结构 | overdue / advanced / stalled / fulfilled_recently + counts + suggestions | horizon H0/H1/H2 + story_questions + character_intents + boundary + preview |
| 核心机制 | 伏笔**生命周期**：设局→推进→兑现，`deadline_chapter` 逾期即告警 | 叙事**预算**：`committed_until_word` 承诺字数 vs `written_until_word` 已写，余量不足请求 replan |
| 触发条件 | 逾期（deadline < 当前章）/ 停滞（近 10 章无推进关键词） | `detect_story_boundary`：剩余 plots ≤2 或承诺余量 <1 批（PLOTS_LOW/WORDS_LOW，防抖去重） |
| 处理哲学 | 只报告不修复，suggestions 供决策 | 只生成预览不提交，commit 才落正式故事线 |
| UI 呈现 | 详情页手风琴（分组列表）+ 写作台 Plot Run 行内 ✅/⏳ | 详情页/写作台规划面板 + Gantt 承诺黄线 + Plot Run 🧵/✅⏳ |
| 共通点 | **同为「欠账感」驱动**：台账=欠读者伏笔，规划=欠叙事预算；都是写前/写作中扫描、分级状态 + 建议、问题作 decision_points；都与 `storyline.json` 的 `promises[]`/`plots[]` 同源 | |

> 研判线索：两者可统一抽象为「**读者合同管理**」——台账管「伏笔合同」（设局/收局），规划管「字数合同」（已承诺/已写）。边界触发器（deadline / committed_until_word）与防抖（debounced / revision）机制同构，可考虑共用「合同-违约-决策点」模式。

### 6.2 🔍 质量诊断 vs 审查

| 维度 | 审查（libraries/reviewer.py ContentReviewer） | 质量诊断（agent_tools.diagnose_*，agent_tools.py:1754/1775/1800） |
|---|---|---|
| 粒度 | **单章**（review(ch, chapter_num)） | **多章/全书**（最近 5 章连续性/追读；全书承诺） |
| 输出 | passed / score / summary / issues[]（severity/category/description/location/suggestion） | continuity（逐章 checks + issues）+ retention（章级 hook_strength/dialogue_ratio/drop_risk）+ promises（台账分组）+ suggestions |
| 触发 | 落盘时自动跑 + 详情页一键（按需） | 详情页「▶ 运行诊断」手动聚合跑；Agent 侧 `chapter_quality_gate`（五项聚合：审查/连续性/追读/伏笔/爽点，`agent_tools.py:1849`） |
| 数据源 | 单章 content | chapters + storyline + character_states |
| 处理哲学 | 只报告不修复（修复动作是另一个工具 deai） | 只报告不修复，问题作 decision_points |
| 共通点 | **审查是单章粒度的诊断，诊断是多章粒度的审查**：输出结构同构（状态/分数 + 问题列表 + 建议）；同为规则层零成本确定性输出（无 LLM）；UI 与 MCP 工具同源（/diagnose 注释「MCP 同源」）；聚合体 chapter_quality_gate 把两者并进同一门禁报告 | |

> 研判线索：`chapter_quality_gate` 已把「审查 + 三个 diagnose + 爽点」聚合成统一门禁——两者本质是同一「规则层质量扫描」在不同粒度的实例，差异只在窗口（1 章 vs N 章）与维度组合。可研判是否收敛为「诊断器注册表（粒度 × 维度）+ 统一决策点输出」。

### 6.3 其他观察点

1. **所见即所写**：Plot Run 面板与 Agent 写作上下文共用 `_build_plot_run`——UI 与工具面零漂移，是页面↔agent 协同的关键契约。
2. **双进程 stale 免疫**：所有读路径从磁盘现读（book.json/chapters/draft），web 缓存只做行级优化（`_book_rows_cache` 按 mtime 失效，books.py:21-39）；详情页进入先 `book_mgr.list_all()` mtime 重扫。
3. **先预览后提交**：replan preview（expected_revision 乐观并发）与快照 diff/rollback（回滚前留底）是同一「预览确认才落库/变更」模式，贯穿规划与审计两条链路。
4. **决策点统一契约**：validate_storyline 问题 / 审查 issues / 诊断问题 / 边界 replan / 质量门禁——全部「只报告不修复 → 决策点由 agent/用户定夺」，可作统一交互范式。
5. **Gantt 是模型的直接投影**：三泳道 = 纵轴（弧/情节段）× 横轴（线程）× 字坐标轴，设局→收局虚线 = promises 生命周期；可视化和数据模型一一对应。

---

## 七、关键文件清单

| 文件 | 职责 |
|---|---|
| `ui/templates/book_detail.html` | 书详情页：操作行/基本信息/规划面板/设定表单/运行时面板/目录/故事线 Gantt 挂载；书名就地编辑(145-206)、Gantt 懒初始化(125-143)、plots 确认脚本(207-226) |
| `ui/templates/storyline_write_flow.html` | 写作台：Plot Run 面板(345-393)、双向高亮(147-220)、拖拽分栏(234-266)、续写任务(268-339)、3s 轮询(396-448) |
| `ui/templates/_book_runtime_panels.html` | 运行时面板 JS：角色状态/承诺台账/诊断/快照/审查/去AI/爽点（全部按 id 注入） |
| `ui/templates/_planning_ui.html` + `ui/static/js/planning_ui.js` | 增量规划面板：指标/H0-H2/边界 banner/replan 抽屉/版本冲突 modal；`window.NEPlanning` 暴露 refresh/open/requestReplan |
| `ui/static/js/story_line.js` | 故事线 Gantt：adapt 数据适配(100-237)、三泳道渲染(253-676)、光标/边界/可变未来(678-717)、init(737)、highlight/scrollTo/setZoom(840-891) |
| `ui/web_blueprints/books.py` | 书库/详情/删除 + 运行时 API：detail(139)、character-states(264)、promises(281)、diagnose(300)、snapshots(320)、chapter review/deai/punch-points(342-389) |
| `ui/web_blueprints/desk.py` | 写作台：desk_chapters_api(76，plot_run 组装)、storyline_write_flow(157)、continue_book_page(403)、蓝图引擎流式端点 |
| `ui/web_blueprints/storyline.py` | 规划 API：planning-state(66)、commit-plan(74)、replan-preview DELETE(85)、storyline JSON(110) |
| `agent_tools.py` | 薄工具源：`_build_plot_run`(387)、`_next_plot`(483)、`get_writing_context`(495)、`_build_cast_pack`(288)、diagnose_*（1754/1775/1800）、`chapter_quality_gate`(1849) |
| `libraries/planning_state.py` | 规划状态：load/save、horizon、replan preview、`detect_story_boundary`(278) |
| `libraries/promise_ledger.py` | 读者承诺台账：`scan_promises`(80) 分组扫描 + suggestions |
| `libraries/reviewer.py` / `continuity.py` / `retention.py` / `character_state.py` / `tag_generator.py` / `de_ai.py` | 规则层：单章审查 / 连续性 / 追读 / 角色状态机 / 爽点 / 去AI |
| `books/<id>/` | book.json（进度）· storyline.json（弧/情节段/线程/承诺）· chapters/*.json · draft_chapter.json（草稿+bridge）· character_states.json · tags.json · cost.json |
