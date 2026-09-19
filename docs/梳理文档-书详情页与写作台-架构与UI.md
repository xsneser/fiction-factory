### 3.3 🎯 Plot Run 面板逐区块（你问的「这一部分是干什么的」）

渲染函数 `renderPlotRun`（`storyline_write_flow.html:458-533`）与 `renderRecentOutcome`（`:544-625`），数据 = `desk_chapters_api` 里的 `plot_run` / `recent_plot_outcome`，**与 Agent 侧共用同一组装（`agent_tools._build_plot_run`）**——UI 显示什么，Agent 下一步就写什么：

> 日期：2026-09-08　定位：**梳理/速查档**，聚焦两个页面：① 书详情页 `/books/<id>`（book_detail.html）；② 写作台 `/books/<id>/continue`（storyline_write_flow.html）。给大体量模型做 UI 架构分析、组件与数据流研判、以及「读者台账 vs 增量规划」「质量诊断 vs 审查」相似性比对。
> 事实源：代码是最终真相。数据模型 / 弧·情节段·线程关系 / agent·dsh 工具面参照 `docs/梳理文档-故事线关系与角色注入与架构.md`（2026-09-08，本档不重复其模型定义，只补「页面层怎么消费模型」）。**行号为探索时锚点，字段增删以代码为准。**
> 操作面：`ui/templates/`、`ui/web_blueprints/`、`ui/static/js/`、`agent_tools.py`、`libraries/{planning_state,promise_ledger,reviewer,character_state,continuity,retention}.py`。

---

## 一、两个页面的定位（一句话）

- **书详情页** = 书的「档案 + 角色状态台」：设定/故事线/章节的**查看**入口 + 🎭 角色状态（默认展开），**不写正文**。
  （2026-09-19：承诺台账 / 诊断 / 快照留痕 / 决策中心 四个面板已从页面删除——它们是 agent 决策与排障视图，人不看；
  底层 `scan_promises` / `diagnose_*` / `chapter_quality_gate` / `book_snapshot` 全部保留，仍被 agent 工具与收章链路使用。）
- **写作台** = 书的「生产 + 驾驶舱」：顶部两栏（**上一段（已完成）** + 角色状态）回答「刚写完那段实际发生了什么、现在走到哪、人什么状态」，下方左=故事线 Gantt、右=分页阅读器实时展示「Agent 写了什么」，通过侧栏 Agent（dsh）逐情节段续写。
- 两者共享：📋 故事线 Gantt（story_line.js，两页都从 `/api/storyline/<id>/planning-state` 取规划叠层）、侧栏 Agent（agent_panel.js）。
  🧭 规划提示条（`_planning_ui.html` + planning_ui.js）**只挂写作台**；书详情改为一段不渲染的极简 loader 取同样的数据喂 Gantt。

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
| ~~6~~ | ~~🧭 增量规划面板~~ | **已删**（agent 决策视图）。书详情改为 `book_detail.html:44-76` 的一段**不渲染** loader：取 `/api/storyline/<id>/planning-state` → 设 `window.__PLANNING_STATE__`/`__PLANNING_BOUNDARY__` → 派发 `ne:planning-updated`，只为保住第 12 项 Gantt 的规划叠层 |
| 7 | 设定编辑表单 | `_world_edit_form.html` 直铺可编辑（人物/世界观/基调等） |
| 8 | 🎭 角色状态（手风琴，**默认展开**） | `_book_runtime_panels.html`：仅此一个面板。每卡三行 位置/状态/动作；主角与本章出场展开、其余收进「其他角色 N 人」（详见 §2.3） |
| 9 | 📝 简介 | `basic_info.synopsis` 卡片 |
| 10 | ✍️ 进行中草稿 | 手风琴展示 `draft_chapter.json` 未固化文本（写作台中断保留，只读） |
| 11 | 📑 目录 toc-split | 左=章列表（第 N 章: 标题 + 📊 字数·时间，独立滚动）；右=章节展开（标题/📝 摘要/🔍 审查 tag 分·通过·问题摘要/正文 pre）。审查 tag 来自 `ch.review` 服务端数据 |
| 12 | 📋 故事线 Gantt | `#detail-storyline`（`story_line.js` 挂载），**默认折叠**：容器高度 0，首次点开 `#sl-toggle` 时才 `StoryLine.init`（`book_detail.html:125-143`）；`ne:planning-updated` 后重初始化 |
| 13 | 空态 | 无章节时「✍️ 还没有生成章节 → 进入写作台」 |

### 2.3 角色状态面板与 API（2026-09-19 收缩：只剩这一个）

| 面板 | 触发 | 端点 | 规则层 |
|---|---|---|---|
| 🎭 角色状态 | 进页自动 | `GET /api/book/<id>/character-states` | `libraries/character_state.py`（`character_states.json`：章末落账的动态态 + 事件台账） |

**已删除的四个面板与其端点**（底层能力全部保留，只是没有浏览器入口——它们仍被 agent 工具与收章链路使用）：

| 原面板 | 原端点 | 底层仍在用它的地方 |
|---|---|---|
| 承诺台账 | `GET /api/book/<id>/promises` | `diagnose_promises` / 章质量门禁 / 写作上下文 / 收章台账更新 |
| 诊断 | `POST /api/book/<id>/diagnose` | `chapter_quality_gate`（收章后自动跑）/ 决策点聚合 |
| 快照留痕 | `GET/POST /api/book/<id>/snapshots…` | `agent_tools._wrap_book_lock()` 在每个写工具前自动留底 |
| 决策中心 | `GET /api/book/<id>/decision-center` | `diagnose_story_window` / `chapter_quality_gate.decision_points` |

同时删除的三个孤儿函数（书详情页从未有按钮调用它们）：`runChapterReview` / `runChapterDeai` / `runPunchPoints`；
它们对应的三个章节级端点 `…/review` `…/deai` `…/punch-points` 仍在后端保留。

**角色状态接口补的三个展示标记**（`books.py api_character_states`，纯磁盘读 + 内存拼装）：
`role`、`is_protagonist`（静态设定的 `role==主角` 或 `importance==1`）、`appears_in_current_chapter`
（`last_appeared_chapter == current_chapter`；「本章」= **最近已完成、已收章**的章节，不扫进行中草稿——
章内实时口径属于写作台）。没有它们，前端无从判断谁该默认展开。

---

## 三、写作台（storyline_write_flow.html）

### 3.1 数据源（desk.py:157-249）

`storyline_write_flow(engine_id)` 组装：引擎 `state` + `storyline` + `book`（跨进程修正 current_chapter，从磁盘 book.json 现读）+ `chapters`（磁盘已写章节 **+ 进行中草稿**，草稿以 `draft:true` 标记、bridges 逐情节段渲染）+ `total_ch`（字数轴总章数，由 `planned_words` 推导兜底）+ `planning_state / planning_boundary`。页面注入 `__BOOK_STORYLINE__ / __STORYLINE_ID__ / __ENGINE_ID__ / __BOOK_CURRENT_CHAPTER__ / __BOOK_CHAPTERS__`。

### 3.2 UI 结构（自上而下）

| # | 区块 | 内容与交互 |
|---|---|---|
| 1 | 面包屑 + h1 ✍️ 写作台 | 书库 › 书名 › 写作台 |
| 2 | info_bar | 书名 · 笔名 · 进度（第 N 章 · 共 M 字）· 状态 badge · 每章 N 字 |
| 3 | 🧭 规划提示条 | 只读边界告警 + 规划门禁 + 少量方向/问题/人物意图提示（`_planning_ui.html`，写作台唯一形态）；规划由章级写作 Flow 自动完成，无手工抽屉 |
| 3.5 | 顶部两栏（全宽） | `#wf-latest-context`，置于**两栏布局之上**：左 `#wf-last-plot` = **上一段的结构化事实**（归因行 + 已作选择/已知信息/关系变化/资源变化/承诺更新/新问题 + 折叠的「规划上下文」）；右 `#wf-current-cast` = 角色状态（每卡 位置/状态/动作 三行，主角与本段出场展开、`referenced` 收进「其他角色 N 人」）。两栏不重叠：角色字段全在右栏。数据来自 `/api/desk/chapters/<bid>`（§3.3） |
| 4 | 左栏 editor-left | 可拖拽分栏（20%–70%，localStorage `ne_storyline_w` 持久化，`storyline_write_flow.html:266-297`）；本栏只剩故事线 Gantt |
| 6 | 📋 故事线 Gantt | `#editor-storyline`（story_line.js 挂载，scrollable 模式，含缩放控件） |
| 7 | 右栏 editor-mid | 顶部 ✍️ 继续写正文卡（`runWritingTask` → 侧栏 Agent；运行态 ⏳ 续写运行中… / ⏹ 停止）；下方分页阅读器 |
| 8 | 分页阅读器 | `ReaderCore` 双容器推入动画；章节渲染把正文按情节段包成 `span.m-bridge[data-bridge=plot_id]`（草稿加徽标）；工具栏 📑目录/⬅上一页/「第 N 章 · 第 N 页」/下一页➡；目录抽屉右侧滑出 |
| 9 | 双向高亮 | 点 Gantt 情节段/弧 → 右栏跳页高亮对应正文（`sl:plot-click`/`sl:outline-click` → `highlightBridgeContent`/`highlightOutlineContent`）；agent 高亮 `StoryLine.highlight` 反向 |
| 10 | 轮询 | 进页立即 + 3s 轮询 `/api/desk/chapters/<bid>`：刷新顶部两栏（`renderLatestContext`，fingerprint 未变不重建 DOM）与章节（`Reader.setChapters` 保留当前页/高亮、`_activePid` 恢复） |

### 3.3 顶部两栏：上一段（已完成）的事实 + 角色状态

渲染入口 `renderLatestContext(d)`（单一入口，轮询每轮只调一次；fingerprint 变化才重绘）。左栏 `renderLastPlot(d)`
读 `recent_plot_outcome` + `planning`，右栏 `renderCurrentCast(d)` 读 `plot_run.cast_pack` + `cast_events`。

**两栏分工不重叠**：角色的 位置/目标/实力/关系/弧阶段 **由右栏覆盖**（右栏每卡的「位置」= `dyn.location`、
「状态」三个 chip = `power_level`/`arc_stage`/`relationship_to_mc`、「动作」= `dyn.goal`，正是那一组的全部字段），
所以左栏只放**非角色**内容。**待写段**的信息也只在左栏 Gantt（点情节段）与规划提示条里看。

**左栏 = 上一段的结构化事实 + 规划上下文折叠组**

| 区块 | 数据路径 | 缺失时 |
|---|---|---|
| 归因行 | `recent_plot_outcome.plot_name`（这些事实属于哪一段）+ `reconcile.kind` 徽标（✅与预期一致 / 🟡实际发展与预期不同 / 🟡预计变化尚未发生 / 🔵出现新的变化）与 `reconcile.summary`（如「存在未覆盖事实」） | 无 plot_name → 整卡空态「还没有已完成的段落」 |
| 已作选择 | `recent_plot_outcome.facts.choices_made` | 该行不渲染（六类事实都是**有值才渲染**） |
| 已知信息 | `facts.information_revealed` | 同上 |
| 关系变化 | `facts.relationship_changes` | 同上 |
| 资源变化 | `facts.resource_changes` | 同上 |
| 承诺更新 | `facts.promise_updates` | 同上 |
| 新问题 | `facts.new_story_questions` | 同上 |
| ▸ 待解问题（默认折叠） | `planning.story_questions`（**只留这一组**：H1 方向条是 planner 视图、弧/线程 Gantt 上已有，都已删；人物意图在右栏） | 空则不渲染 |

值归一化沿用旧实现口径：列表用「；」拼接，字典取 `text/description/title`（`_factText()`）。
**刻意不放进左栏**：章号与字数进度（页头已有）、情节段摘要 `plot_summary`（正文就在下方）。

**右栏 = 角色状态 + 「其他人物意图」**

底部另列 `planning.character_intents` 里**本段没出现在卡片上**的人（如巴鲁姆/老葛林）——他们在页面上本来无处可见；
本段出场的人不重复（该数据的 `observations` 与卡上的「位置/状态/动作」同源，同一批 `character_events`）。
条目按「事件类型 中文 → 新值」渲染（`_INTENT_EVENT_ZH`）；该数据**没有** `intent` 字段，若某天有了则优先用它。

**每卡的字段（位置 / 状态 / 动作 三行）**

| 行 | 数据 | 说明 |
|---|---|---|
| 位置 | `dyn.location`（回退 `location`） | 章内上报的 `location_shift` 会覆盖正式状态并标「本章已上报」 |
| 状态 | `dyn.power_level` / `dyn.arc_stage` / `dyn.relationship_to_mc` + 离线 >50 章警告 | 三个 chip；全空 → 「未记录」 |
| 动作 | `dyn.goal`（回退 `goal`）+ `cast_events[名].to (+ reason)` | 数据模型里**没有** `action` 字段：`goal`= 当前目标，`cast_events` = 章内最近一条上报变化 |

**左栏只搬回了旧三列对照表的「摘要 + 对账状态」两项**。旧表的 `已作选择 / 已知信息 / 关系变化 / 资源变化`
四类事实行、以及 `人物 / 场景位置 / 承诺 / 待解问题 / 弧与线程` 那几组（旧实现里本就默认折叠）都**没有**搬回来
——它们的行构造/配对/统计逻辑没有作为 `comparison` 字段返回，搬它们等于在前端重写那套投影。

**口径与红线**

> ⚠️ ① 角色状态来自**正式状态机 + 章内 staged 覆盖**（`_staged_cast_projection`），这是写作台比书详情「新一章」的原因；
> ② `cast_events` 由 **desk 侧单独组装**（读 `staged_story_state.json` 的 `character_changes`），**刻意不改 `_staged_cast_projection`**——
>    那个函数同时喂 Writer 上下文，它的 `_dyn_of` 只给 5 个动态字段「防噪音」，塞事件会污染写入提示词；
> ③ 没有数据就如实说「未记录 / 暂无明确行动」，**绝不从 identity/personality 推断**，也不编「情节段目标地点」；
> ④ 对账视图（`reconcile` / `expected_facts` / `state_missing`）已随三列对照区一起移出写作台；
> ⑤ `primary_turn` 由 desk 侧注入 `plot_run["plot"]`（改 `_build_plot_run` 会动 `commit_token` 的 `context_fingerprint`，让在途令牌全部失效）；
> ⑥ 顶部**不再**向 planning_ui 发「本区是否已展示当前待写段」的信号（`__NE_LATEST_HAS_CURRENT__` /
>    `ne:current-context-updated` 已随这次改动删除）：左栏只展示已完成段，所以横幅的「下一段：<H0>」
>    恒按 H0 显示——它是顶部唯一的「接下来写什么」。

### 3.4 API 面（desk.py）

| 端点 | 用途 |
|---|---|
| `GET /api/desk/chapters/<bid>` | 写作台正文 JSON：chapters（+草稿）+ plot_run + recent_plot_outcome + planning（boundary）；全部磁盘现读 |
| `POST /api/storyline-engine/<eid>/step` | 蓝图引擎：整章一步写（旧路径，按章） |
| `POST /api/storyline-engine/<eid>/write-chapter`（SSE） | 蓝图引擎流式整章（plot_start/plot_done/chapter_done） |
| `POST /api/storyline-engine/<eid>/write-bridge`（SSE） | 蓝图引擎流式**单情节段**（bridge_start/group_chunk/bridge_done/chapter_done/complete） |
| `POST /api/book/<bid>/confirm-storyline` | plots → ready（兜底） |

---

## 四、共享组件：故事线 Gantt 与规划提示条

### 4.1 故事线 Gantt（story_line.js，三泳道 + 字数轴）

- **入口**：`window.StoryLine.init(mountId, storyline, opts)`（`story_line.js:737`）。opts：`scrollable`（无限长滚动 + 缩放 −/+/默认，0.4–4x）、`planning`（承诺边界/可变未来）、`boundary`、`currentWord`（进度光标 = 已写累计字数）。
- **DOM 结构**（`story_line.js:769-791`）：表头（已承诺 · 每章约 N 字 · 弧/情节段/线程计数）→ 字数轴（刻度对齐）→ 内容区三泳道：`📋 弧`（flex:4）/ `🔗 情节段`（flex:4）/ `🧵 线程`（flex:2）→ 底部「🔭 可变未来」区 + 图例（弧/主次情节段/线程色/◉设局→↪收局/黄线=承诺边界/斜纹=可变未来/倒叙插叙）。
- **数据适配**（`adapt`，`story_line.js:100-237`）：弧按 `start_word/end_word` 0 基字坐标落位（缺则章×WPC 推导、兜底 30 章）；情节段在弧内按 `planned_words` 累计定位；线程横带 = 成员情节段区间并集；设局/收局/承诺徽标按 `resolves_plot_id` 与 `promises[]` 映射。
- **视觉要素**：弧树嵌套（parent 连线 + 层级缩进）、倒叙（琥珀）/插叙（绿）着色 + `narrative_target` ◉、情节段条颜色 = 所属线程色、父子情节段贝塞尔线、**设局→收局金色虚线**（长距离自动衰减透明度）、进度光标、承诺边界黄线（「已承诺至 N」）、H1/H2 意图卡片。
- **点击行为**：弧/情节段条 click → `sl:outline-click` / `sl:plot-click`（bubbles，写作台消费做正文高亮）；tooltip 展示 字数/章号/线程/收局/出场/承诺。

### 4.2 🧭 规划提示条（planning_ui.js，**只剩写作台在用**）

- **数据**：`GET /api/storyline/<bid>/planning-state` → `_planning_ui_payload`（`storyline.py:12-63`）：`planning_state`（written/committed 字数、horizon、story_questions、character_intents、last_replan）+ `storyline_snapshot`（revision/各计数/线程/promise_count）+ `boundary`（`detect_story_boundary`）。
- **UI**：边界告警（🟡/🔴 `pm-boundary`，来自 `boundary.reason_codes` + 剩余段数/字余量）+ 规划门禁（⛔ `pm-alert-danger`，来自 `planning_state.checklist.gates.blocking`）+ 清单徽标 + 少量方向/问题/人物意图提示。
  `window.NEPlanning` 只暴露 `refresh` / `syncStoryline`。
- **2026-09-19 收缩**：书详情页那份 `data-compact="0"` 的完整四卡矩阵（指标行 + 现在执行/待解决问题/人物当前意图/最近续规划）与其渲染函数、CSS 已删——agent 决策视图，人不看。
  书详情改为一段**不渲染**的 loader（`book_detail.html`）取同一端点，只为喂 Gantt 的规划叠层。手工 replan 抽屉早已移除（规划由章级 Flow 自主完成）。

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

> ⚠️ 2026-09-19：本章对比的两组**页面入口都已从书详情删除**（承诺台账 / 诊断面板 / 增量规划矩阵）。
> 下面比的是**库层**能力（`scan_promises` / `diagnose_*` / `planning_state`），这些能力仍在，只是不再有人看的 UI。

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
| `ui/templates/_book_runtime_panels.html` | **只剩角色状态**面板（默认展开、主角+本章出场展开）：`/character-states` → 位置/状态/动作三行 + 矛盾与压力折叠 + 事件台账折叠 |
| `ui/templates/_planning_ui.html` + `ui/static/js/planning_ui.js` | 规划提示条（**只挂写作台**）：边界告警 + 规划门禁 + 清单徽标；`window.NEPlanning` 暴露 refresh/syncStoryline |
| `ui/static/js/story_line.js` | 故事线 Gantt：adapt 数据适配(100-237)、三泳道渲染(253-676)、光标/边界/可变未来(678-717)、init(737)、highlight/scrollTo/setZoom(840-891) |
| `ui/web_blueprints/books.py` | 书库/详情/删除 + 运行时 API：detail(139)、character-states(264)、promises(281)、diagnose(300)、snapshots(320)、chapter review/deai/punch-points(342-389) |
| `ui/web_blueprints/desk.py` | 写作台：desk_chapters_api(76，plot_run 组装)、storyline_write_flow(157)、continue_book_page(403)、蓝图引擎流式端点 |
| `ui/web_blueprints/storyline.py` | 规划 API：planning-state(66)、commit-plan(74)、replan-preview DELETE(85)、storyline JSON(110) |
| `agent_tools.py` | 薄工具源：`_build_plot_run`(387)、`_next_plot`(483)、`get_writing_context`(495)、`_build_cast_pack`(288)、diagnose_*（1754/1775/1800）、`chapter_quality_gate`(1849) |
| `libraries/planning_state.py` | 规划状态：load/save、horizon、replan preview、`detect_story_boundary`(278) |
| `libraries/promise_ledger.py` | 读者承诺台账：`scan_promises`(80) 分组扫描 + suggestions |
| `libraries/reviewer.py` / `continuity.py` / `retention.py` / `character_state.py` / `tag_generator.py` / `de_ai.py` | 规则层：单章审查 / 连续性 / 追读 / 角色状态机 / 爽点 / 去AI |
| `books/<id>/` | book.json（进度）· storyline.json（弧/情节段/线程/承诺）· chapters/*.json · draft_chapter.json（草稿+bridge）· character_states.json · tags.json · cost.json |
