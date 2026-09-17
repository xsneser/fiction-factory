# NovelEngine UI 修改计划 v2 —— 对接「故事线增量规划」最终架构

> 状态：**计划稿 v2（已按 2026-09-08 架构评审修正 5 处数据契约错位，待主 Agent Phase 落地后按波次实施）**
> 对照依据：`docs/novel_engine_agent_architecture_plan (2).md`（架构 Plan）+ 2026-09-08 架构评审结论（主 Agent / MCP / planning_state 最终方案）
> 本文件只做 UI 侧改造规划；UI 永不自行推断故事、boundary、future plot 或 Agent 状态——所有内容只消费平台事实与 Agent 已提交的决策结果。
> 实施顺序对齐架构 Plan §28 Step 9（最后才调整 UI / Gantt）与评审结论（先修数据契约，再按 Wave A 开工）。

---

## 0. v2 修订摘要（相对 v1）

| # | v1 的问题 | v2 修正 |
|---|---|---|
| ① | `future_plan` / `planning` 挂进 `storyline.to_dict()` | 语义分层：`storyline.json` = 合同（保留 `storyline_revision`）；`planning_state.json` = 思考草稿（H0/H1/H2、future_intents、questions、decision_log）。UI 经 GET planning-state **聚合两者**，不改 `storyline.to_dict()` 结构 |
| ② | 依赖 MCP 新工具 `get_story_state` / `commit_story_plan` / `get_planning_candidates` | 不新增这些 MCP 工具。UI 数据源 = `get_writing_context` 扩展 + `planning_state.json` + `extend_storyline`；HTTP `/commit-plan` 仅是 UI→后端交互 API，与 MCP 层分离 |
| ③ | Forecast 区画了 p19/p20「未来 plot」条 | Forecast 区**只画 intent**（H1 下一弧方向 / Open Question / Character Intent / H2 远期方向），不制造不存在的 plot；除非未来出现独立的 `speculative plot` 类型 |
| ④ | Planning 进度显示「已承诺 24,000 / 42,000 字」 | 去掉总字数终点假象，改为「已写 18k / 已承诺 24k / 当前可执行 3 plots / 未来方向 H1·H2」 |
| ⑤ | Boundary 由 UI 侧公式判断 | Boundary **完全由后端计算**，UI 只消费 `{status, trigger, remaining_plots, remaining_words}`；预留 `PLAN_INVALIDATED` / `MAJOR_CHARACTER_CHANGE` / `NEW_HIGH_PRIORITY_QUESTION` 等 trigger |

---

## 1. 目标

把 NovelEngine 的 UI 从「展示一部完整规划好的故事线」升级为「**当前已承诺执行区 + 可变的未来意图区**」的人机协作界面，让用户随时理解四件事：

> 故事现在在哪里？Agent 接下来准备干什么？哪些已经确定？哪些还可能变化？

**最终信息架构（评审确定的统一模型）**：

```text
                  NovelEngine Story UI
                         │
          ┌──────────────┴──────────────┐
          │                             │
  Committed Storyline           Planning State
  已承诺、可执行、可校验        当前判断、未来意图
          │                             │
 arcs / plots / threads        H0 / H1 / H2
 promises / written            questions / intents
          │                             │
          └──────────────┬──────────────┘
                         │
                   Gantt / Planning UI
```

核心语义（必须坚持）：

- **Gantt 的"实体故事线"只来自 committed storyline**（`storyline.json`）。
- **Forecast 不是另一套假的 storyline**，它是 Agent 当前"接下来可能往这里走"的意图投影（`planning_state.future_intents` 的 UI 投影）。
- `storyline.json` = 世界中的**合同**；`planning_state.json` = Agent 当前的**思考草稿**。两者聚合展示，绝不混写。

## 2. 现状盘点（已核验）

现有 UI 具备以下能力，**全部保留、只做叠加与分区**：

- 垂直 Gantt 组件 `ui/static/js/story_line.js`：字数轴纵向 + 三通道（弧/情节段/线程）横向并列 + 进度光标（水平红线）+ 设局→收局 + 承诺徽标 + 倒叙/插叙 + 缩放，`window.StoryLine.init(mountId, bt, {scrollable, currentWord, currentChapter})`，数据来自 `storyline.to_dict()`。
- 写作台 `ui/templates/storyline_write_flow.html`：左栏 = Plot Run 面板 + 故事线 Gantt（可拖拽分栏、可折叠）；右栏 = 书式阅读器（章节/情节段高亮联动）；3s 轮询 `/api/desk/chapters/<bid>`。
- 书详情 `ui/templates/book_detail.html`：`#detail-storyline` 挂载 Gantt（scrollable）。
- Agent 侧栏 `ui/static/js/agent_panel.js`：任务卡、工具卡、LLM 调试卡、`ne:agent-state` 事件、`agentSendTask(task, {card})`。
- 后端聚合：`desk.py /api/desk/chapters` 已返回 `plot_run`（`agent_tools._build_plot_run/_next_plot` 组装，UI 与 Agent 同源）。

## 3. 设计原则（最终规范）

1. **视觉双区、语义分明**：垂直 Gantt 的正式区保持字数轴纵向、三通道横向并列、写作光标水平、承诺边界水平；H1/H2 作为同一纵向滚动画布中正式区之后的方向区。Committed 区 = 实色；Forecast 区 = 斜纹底 + 虚线边 + intent 卡。
2. **UI 不渲染思维链，只展示决策状态**（decision state）：当前事实 → 当前故事问题 → 当前规划结果 → 执行 → 实际结果。不显示「我先考虑 A 又想到 B 所以决定 C」。
3. **合同 / 草稿分层**：`storyline.json` 承载合同（outlines/plots/threads/promises/revision）；`planning_state.json` 承载草稿（committed_until_word/horizon/future_intents/story_questions/character_intents/last_replan/decision_log）。UI 只聚合，不跨层写。
4. **不制造假象**：不画不存在的未来 plot；不用总字数终点暗示全书已规划；不把 intent 显示成 storyline。
5. **数据契约先行、空态兜底**：所有新字段来自 §12 唯一数据真相表；缺失时组件降级为现状，不报错。
6. **可回退**：新面板/新 API 全部可独立摘除。

## 4. 核心改造一：Gantt 双区

### 4.1 承诺边界线与写作光标（双线并存）

> 系统存在三个不同概念，**不能再混成一个进度**：
> `current_word`（已写到哪里，红线） / `committed_until_word`（已正式决定到哪里，黄虚线） / `future_intents`（之后大致可能往哪里走，正式故事线底部的方向区）。

- `story_line.js` 在正式区的 `renderCursor` 同级维护 `renderBoundary(contentArea, committedUntilWord)`：在 `committed_until_word` 对应高度画**横跨三通道的水平琥珀色虚线**，z-index 与光标同层。写作光标（红）在已写字数处，随写作下移；承诺边界（黄虚线）在已承诺字数处，两者位置不同、必须同时存在；两条线均不进入底部方向区。
- tooltip：边界线悬停显示「已承诺至约 N 字 / 剩余已规划 plot 数 / 最近 replan 时间」。
- 视觉语言（直接保留为最终规范）：**实色 = 已承诺；斜纹 = 可变未来；红线 = 写到哪里；黄线 = 决定到哪里。**

### 4.2 Forecast 区（只画 intent，不画假 plot）

- `renderForecast(forecastStrip, futurePlan)`：作为 `.sl-main` 内正式区之后的自然流内容，随同一纵向滚动条位于故事线最底部；**不渲染任何 plot 条**，也不参与字数轴。
- 区内只放 H1/H2 两行 intent 卡（`.sl-forecast-card`），横向流动并允许折行：

```text
┌────────────────────────────────────────────────────────────┐
│ 🧭 近期方向 H1 │ 工区级问题 → 天梯安全 · 关键转折未决定       │
├────────────────────────────────────────────────────────────┤
│ 🔭 远期方向 H2 │ 深空威胁 · ⟢ promise 03 / 07                │
└────────────────────────────────────────────────────────────┘
```

- 数据：全部来自 `planning_state`（H1 ← horizon.h1；H2 ← future_intents；Open Question ← story_questions；Character Intent ← character_intents）。
- **红线**：除非未来引入独立的 `speculative plot` 数据类型，否则 Forecast 区永远不出现带 plot_id 的条。

### 4.3 数据输入

- `storyline.to_dict()` 保持合同字段（含 `storyline_revision`）；不加 planning / future_plan。
- 前端注入：模板同时注入 `window.__BOOK_STORYLINE__`（合同）与 `window.__PLANNING_STATE__`（草稿，来自 GET planning-state）；`story_line.js adapt()` 分别解析，Gantt 条只消费合同，Forecast 区只消费草稿。

## 5. 核心改造二：Planning State 面板

### 5.1 展示内容（书详情完整面板 + 写作台必要提示条）

- **书详情页**保留完整规划摘要，供跨章节复盘：
  - 三指标行（**不用总字数终点**）：已写 / 已承诺 / 当前可执行 plots；
  - H0、Open Questions、Character Intents、Last Replan；
  - 空态：无 planning_state 时显示「Agent 尚未建立规划状态」，不报错。
- **写作台**只显示会影响当前操作的低高度提示条：
  - 仅在 boundary 真的需要续规划时显示触发原因和剩余 Plot/字数；无预警时不显示常驻「规划充足」；
  - 对照区尚未提供当前 Plot 时，才显示 H0 第一项作为下一段兜底；对照区已有当前 Plot 时不重复显示；
  - Open Questions / Character Intents 只显示开放条目数量徽标，不展开跨章节详情；问题终态不计入徽标；
  - 不在写作台重复显示版本号、已写/已承诺/剩余段常驻指标或最近续规划审计 JSON，它们分别由 Gantt/页面头部、书详情和后端日志承担。

### 5.2 数据来源

- `GET /api/storyline/<book_id>/planning-state` → 后端聚合返回 `{planning_state, storyline_snapshot:{revision, outlines/plots/threads/promises 摘要}, boundary}`。
- 端点实现放 `ui/web_blueprints/storyline.py`，**纯读** `books/<book_id>/planning_state.json` + `books/<book_id>/storyline.json`（与架构 Plan §28 Step 3「planning_state 先只读」一致）。

## 6. 核心改造三：Boundary 预警 + Replan 交互流

### 6.1 Boundary：后端算，UI 只消费

- UI **不实现任何边界公式**。后端 boundary detector 输出：

```json
{
  "boundary": {
    "status": "warning",
    "trigger": "LOW_COMMITTED_HORIZON",
    "remaining_plots": 2,
    "remaining_words": 6200
  }
}
```

- 预留 trigger 扩展（UI 无需随之改动）：`PLAN_INVALIDATED` / `MAJOR_CHARACTER_CHANGE` / `NEW_HIGH_PRIORITY_QUESTION`。
- 呈现：写作台只显示只读的规划状态；`needs_replan` 不再显示“规划下一段”按钮，也不打断续写。用户点击一次“继续写正文”后，由父 `_writer_fsm` 自主决定写 Plot、续规划或收章。

### 6.2 Replan：父 Flow 内自动预览 → 原子提交 → 继续写作

```text
点击继续写正文（chapter_to_completion / policy=auto）
   ↓
_writer_fsm：章满？还有已承诺 Plot？是否到规划边界？
   ├─ Writer 子 run → save_plot_draft → 回 FSM
   ├─ Planner 子 run → set_replan_preview → replan_service 原子提交 → 回 FSM
   └─ chapter_changed → 完成一章
```

- UI 不再提供 `#replan-drawer`、候选方向选择、重拟、丢弃或手工确认；H1/H2 只在故事线底部作为方向信息展示。
- Planner 的 `set_replan_preview` 仍是内部事件，页面仅刷新只读 planning/storyline 状态；不会打开人工面板。
- **层次分离（重要）**：`POST /commit-plan` 仍是兼容客户端与测试使用的 UI→后端 API，不是 MCP 世界的新工具；一键写作入口由服务端复用 `replan_service.commit_replan_preview` 自动提交。

## 7. 核心改造四：Plot Run 升级（execution brief + Prediction → Fact）

- `plot_run` 扩展返回 `execution_brief`：`dramatic_goal / conflict_source / character_choice / irreversible_change / reader_question / ending_hook`（把「我要写什么」升级为「这一段为什么存在」）。
- 新增**当前执行简报 + 上段承接约束 + 按需事实审计**对照区：

```text
本段 · 待写
戏剧目标 / 冲突来源 / 人物选择 / 不可逆变化 / 结尾钩子

上一段 · 已发生
结构化事实 → clean / 偏离 / 预计未发生 / 新事实

完整状态审计（按需）
人物 / 场景位置 / 弧与线程 / 承诺 / 待解问题
```

- 数据：左侧来自 `recent_plot_outcome` 的 agent 上报事实与对账；右侧来自同一次 desk 响应的 `plot_run.cast_pack`、`expected_facts`、`execution_brief`、`arc_goal` 和 `promise_state`；后端只做确定性 `comparison` 投影，UI 不新增故事状态推断。
- `storyline_write_flow.html` 保留一个共享渲染器和三列审计骨架，但默认优先展示当前 Plot 的执行简报；中间轴使用“写作重点/承接→推进”文案，`aria-label` 保留对照字段语义。没有变化的分组进入“完整状态审计”，单侧数据不保留不可见列或固定高度空单元格。
- 对账结果必须实际呈现为 `clean / prediction_drift / missed_prediction / unpredicted_fact` 徽标或行动提示；不再另设“实际变化/身后变化”列表。地点只显示带人物归属的 `dyn.location` / `location_shift`，不生成 plot-level 目标地点；承诺和问题按稳定 id 配对。

## 8. 核心改造五：Revision 与自动失败状态

- 一键写作由服务端 FSM 持有 `expected_revision` 并在 `replan_service` 原子提交；出现 stale/无效预览/锁冲突时结束为明确 error，保留草稿，不弹出已删除的人工规划面板。
- `commit-plan` 的 stale 响应仍保留给兼容客户端；当前页面不再渲染 revision modal，也不把失败伪装成章节完成。

## 9. 降级为 P2 的组件（第一版不做或弱化）

| 组件 | 原优先级 | 现优先级 | 说明 |
|---|---|---|---|
| 故事张力面板（§9 评审） | P1 | **P2** | 属于「让系统更好用」，不是「让增量规划成立」 |
| 软评分可视化（§21 评审） | P1 | **P2** | review-skill 输出为 JSON 即可，可视化延后 |
| Agent 侧栏工具 profile 可视化（§2/§16） | Wave A | **Wave E 弱化** | 工具 profile 是开发调试能力，不应成为写作体验核心视觉层；第一版仅保留「当前可用工具数」小徽标 |

## 10. 文件级改动清单

| 文件 | 改动 |
|---|---|
| `ui/static/js/story_line.js` | `.sl-main` 内正式区 + 底部 H1/H2 方向区；`renderBoundary()`、`renderForecast()`（仅 intent 卡）；滚动锚点、正式合同刷新、方向区跳转 |
| `ui/static/css/story_line.css` | `.sl-formal-row`、`.sl-forecast-strip`、`.sl-forecast-card` 与单一纵向滚动布局（对齐现有令牌） |
| `ui/templates/storyline_write_flow.html` | `#planning-state-panel`、一键章级续写/停止/状态文案；故事线方向跳转与正式合同刷新；`renderPlotRun()` 升级（execution brief + Prediction→Fact） |
| `ui/templates/book_detail.html` | 顶部 Planning State 摘要卡（已写/已承诺/可执行 + H0 + Open Q + Character Intent）；故事线合同刷新 |
| `ui/templates/_planning_ui.html` | 只读规划状态容器（不再包含 boundary banner、replan drawer 或 revision modal） |
| `ui/web_blueprints/storyline.py` | `GET /planning-state`（纯读聚合）、`POST /commit-plan`（验证 revision → 调 extend_storyline → validate → 更新 planning_state） |
| `ui/web_blueprints/desk.py` | `/api/desk/chapters` 响应附 `boundary`；`plot_run` 附 `execution_brief/character_impact/character_events`（透传） |
| `ui/web_blueprints/books.py` | 书详情注入 planning_state 摘要 |
| `ui/static/js/agent_panel.js` | 章级 flow mode / busy reject / task 生命周期事件；普通侧栏仍保留原有接力行为 |
| `ui/web_blueprints/agent.py` | `chapter_to_completion` → policy=auto；章级入口 busy 拒绝兜底 |
| `libraries/dsh_bridge.py` | 复用 `_writer_fsm`；自动续规划提交事件与提交失败有界处理 |
| `ui/static/css/base.css` | 新增 `--forecast-border` / `--boundary` 语义令牌 |

## 11. 实施波次与验收标准（按评审重排）

> 优先级：**P0** = Gantt 双区 / Planning State / Boundary / Replan / Revision；**P1** = Plot Run execution brief + Prediction→Fact；**P2** = Tension Panel / Soft Score / 复杂可视化。

| 波次 | 内容 | 验收 |
|---|---|---|
| **Wave A**（P0） | Planning State 面板 + Gantt 正式区/底部 H1-H2 方向区（单一纵向滚动）+ H0 | 书详情/写作台可见「已写 / 已承诺 / 可执行 plots」；向上看三泳道、滑到底看 H1/H2；Forecast 无任何 plot 条；旧书无 planning 数据仍正常 |
| **Wave B**（P0） | 章级一键续写（写作 / 自动续规划 / 收章）+ 最新正式故事线拉取 | 点击一次后父 FSM 持续运行；自动续规划后新弧/情节段立即出现在正式区，黄线推进，方向区仍在最底部，滚动/高亮不丢 |
| **Wave C**（P1） | Plot Run 升级（execution brief）+ Prediction→Fact 对照 + Open Question 卡 | 写作时可见「这一段为什么存在」；写前预测 vs 写后事实并排显示 |
| **Wave D**（P0 补齐） | 自动任务错误/暂停状态 + revision 保护 | 自动续规划或写作失败时明确提示、保留草稿、释放租约；不显示章节已完成假象 |
| **Wave E**（P2 + 收尾） | Tension Panel（弱化版）+ 软评分（JSON 展示）+ 响应式适配 + 侧栏工具小徽标 + 全页回归 | `test_e2e_pages.py` 通过；窄窗口无竖排截断 |

## 12. 唯一数据真相表（联调标准，评审确认）

> **UI 内容 → 唯一来源**。UI 永不自行推断故事、boundary、future plot 或 Agent 状态。

| UI 内容 | 唯一来源 |
|---|---|
| 已写字数 | 实际章节 / plot 写作（`current_word`） |
| 写作光标（红线） | 当前实际写作位置 |
| 已承诺字数 | `planning_state.committed_until_word` |
| 已承诺弧 | `storyline.outlines` |
| 已承诺 plot | `storyline.plots` |
| 线程 | `storyline.threads` |
| Promises | `storyline.promises` |
| H0 | 未写正式情节段的派生执行列表（UI 投影 `display_horizon.h0`） |
| H1 | `planning_state.horizon.h1`，在同一 Gantt 滚动画布底部展示 |
| H2 | `planning_state.future_intents`，在同一 Gantt 滚动画布底部展示 |
| Open Questions | `planning_state.story_questions` |
| Character Intent | `planning_state.character_intents` |
| 最近 Replan | `planning_state.last_replan` |
| Boundary | 后端 boundary detector（`{status, trigger, remaining_plots, remaining_words}`） |
| Revision | `storyline.storyline_revision` |
| Forecast | `planning_state.horizon.h1` + `planning_state.future_intents` 的 UI 投影（只画 intent，位于同一滚动画布的正式区之后） |
| Plot Run | `get_writing_context` / `plot_run` |
| 写后角色事实 | `save_plot_draft` / character state |
| 软评分 | review / review-skill 输出 |

**给主 Agent 的接口要求（最小交集）**：

1. `planning_state.json` 字段名按上表固定（H0/H1/H2、future_intents、story_questions、character_intents、last_replan、committed_until_word、decision_log）。
2. `storyline.json` 增加 `revision`（int）即可，UI 不要求其它新字段。
3. MCP 层**不新增** `get_story_state` / `get_planning_candidates` / `commit_story_plan`；写作上下文继续由 `get_writing_context` 扩展，结构写入复用 `extend_storyline`。
4. `GET /planning-state` 与 `POST /commit-plan` 是 UI 侧 HTTP API，由后端蓝图实现，不在 MCP 工具面暴露。

## 13. 风险与不变量

- **不变量**：不修改 `storyline.json` 既有结构（仅使用 `storyline_revision`）；不改变 `validate_storyline`/`validate_world` 硬规则；不动 `BookLock`；不把 planning_state 内容写进 outlines；方向区不进入 `TOTAL_WORDS`，红黄线不进入方向区。
- **风险 1：契约不一致** → §12 表 + 空态兜底 + 联调一次。
- **风险 2：Forecast 被误解为已确定剧情** → 斜纹底 + 虚线 + 「方向性意图，可随写作改变」文案 + 图例；Forecast 区无 plot 条。
- **风险 3：自动写作中途失败或重复点击** → 入口 busy reject；服务端 auto 续规划提交失败有限重试，错误保留草稿且不伪报章节完成。
- **风险 4：旧书无 planning 数据** → 所有新组件空态降级，回归现状。

## 14. 与主 Agent 的协调

- 实施前先 `git status` 确认主 Agent Step 1–8 落盘，避免同文件冲突。
- UI 侧改动文件与主 Agent 改动文件不重叠（主 Agent：`agent_tools.py` / `mcp_server.py` / `libraries/storyline.py` 只加 revision / `storage/`；UI 侧：`ui/static/`、`ui/templates/`、`ui/web_blueprints/`）。
- 若需将本计划同步给主 Agent：把本文件路径告知其即可。
