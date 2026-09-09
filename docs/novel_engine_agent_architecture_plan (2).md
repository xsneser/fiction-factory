# NovelEngine Agent 架构 / MCP 工具 / 故事线增量规划优化 Plan

> 目标：交给本地 Agent 执行。本文不是“讨论稿”，而是实现计划。
> 事实基线：以当前代码/`TOOL_REGISTRY`/`EXPECT_MCP_TOOLS` 为准；本计划不要求推翻现有护栏，只要求在其上做减法、分层和增量规划。
> 核心方向：**让 Agent 更像一个“带记忆、带局部预测、持续修正的编剧”，而不是一个拿着 44 个按钮逐个试的工具调用器。**

---

## 0. 最终目标

把 NovelEngine 从当前的：

> 用户任务 → Agent 自己理解 → 看 44 个 MCP → 自己猜该调谁 → 大量读/试/改 → 一次性构建较完整故事线 → 顺序写

升级成：

> 用户意图 → 任务路由 → 状态读取 → **人类式叙事推演** → 局部计划（当前 + 下一步 + 远期承诺）→ 最小工具集合执行 → 验证 → 写作/结果观察 → 更新故事世界状态 → **自动判断是否需要向前延伸故事线** → 下一轮。

核心原则：

1. **LLM 负责判断、生成、取舍和因果推演；平台负责事实、状态、落盘、校验和护栏。**
2. **工具不应该成为 Agent 的思考空间。** 工具只是实现动作。
3. **故事线不要求一次规划到底。** 初次建书只提交一个“可信的前部故事区”，后续写到规划边界时再继续构建。
4. **未来剧情允许有不确定性。** 当前可执行剧情必须确定；远期只保留方向、承诺、潜在线索，不要过早锁死。
5. **写作必须闭环：计划 → 写作 → 事件/状态更新 → 检查 → 重新计划。**

---

# 1. 对当前架构的判断

## 1.1 已经做对的事情：保留

当前架构最重要的方向是正确的：

- Agent 在平台外，MCP 是薄工具面。
- `agent_tools.TOOL_REGISTRY` 是工具唯一来源。
- `PHASE_GATES` / `BookLock` / `LoopGuard` 保留。
- `storyline.json` 作为书的剧情事实层。
- `outlines` / `plots` / `threads` 三个维度正交。
- `promises` 负责“设局 → 推进 → 收局”。
- `get_writing_context` 已经把人物、当前情节段、弧目标、线程、承诺、风格集中成写作上下文。
- `plot_run.cast_pack` 已经把出场角色与动态状态做了预解析。
- `save_plot_draft(character_events=...)` + `save_chapter_text` 已经形成“写作造成状态变化 → 系统记账”的闭环。
- dsh 已经有 skill 层，把“流程规则”和“原子工具”分开。

这些不要推翻。

当前文档明确说明：NovelEngine 由平台外 Agent 驱动，MCP 只提供薄工具；工具统一从 `TOOL_REGISTRY` 注册，并有 phase / lock / loop 三重护栏。见来源：fileciteturn4file0L10-L15

## 1.2 当前最主要的架构问题

### 问题 A：44 个工具对 Agent 的“注意力预算”太贵

当前 Agent 看到的是一个“平台 API”，而不是“当前任务需要的动作集合”。

结果：

- Agent 会花 token 识别工具，而不是思考故事。
- 相近工具之间容易反复尝试。
- 在不同阶段会误调用当前根本不适用的工具。
- Tool docstring 与 `NOVEL_AGENT.md` 双份契约，进一步增加认知成本。

当前工具数是 44，且文档明确要求以 `tools/mcp_smoke.py EXPECT_MCP_TOOLS` 为准；因此不要在计划里手写固定的 44 个名字作为长期真值，而应该让运行时自动生成分类清单。fileciteturn4file0L10-L15

### 问题 B：实际运行里已经出现“无效探索”

这次实际运行中，Agent 在还没有 `book_id` 的情况下调用了 `arc_material_candidates(book_id="")`，随后才意识到此阶段不能依赖这个工具；它还对候选库做了多轮相近关键词搜索。fileciteturn3file5L230-L252

后续实际运行里又连续尝试了多组弧/情节段查询，其中多个查询返回空结果，然后 Agent 再调整关键词。fileciteturn7file1L259-L295

这说明问题不是“模型不够聪明”，而是**工具路由没有在进入推理前完成**。

### 问题 C：Agent 在工具副作用/权限上反复思考

实际运行里，Agent 对“是否自己调用 submit”“skill 规定与工具 docstring 是否冲突”进行了很长一段内部讨论。最终正确行为是填完后停下等待确认。fileciteturn2file0L10-L10

这说明“操作权限”应该变成运行时明确状态，而不是让模型每次重新解释。

### 问题 D：故事线当前更像“静态总大纲”，不适合持续创作

当前 `storyline` 的硬规则要求纵轴由顶层弧完整覆盖，`words` 决定情节段和弧跨度，写作按 `next_plot` 顺序前进；这对于“已经规划完成的一段故事”很好，但对于“先写前 20k / 30k，后面边写边发现故事”不够自然。fileciteturn7file0L45-L87

所以不要直接把未来 150k 字全部规划死；应该增加“**已承诺故事区**”和“**远期意图区**”的概念。

### 问题 E：动态状态已经存在，但没有真正成为“续写规划器”的一等输入

当前角色状态机、`plot_run.cast_pack`、最近章节摘要已经具备基础，但真正决定“下一弧怎么继续”的仍主要是 Agent 自己从当前上下文临场推断。fileciteturn7file0L91-L170

下一步需要把“故事状态”提升为显式 planning state。

---

# 2. MCP 工具：什么是必须，什么不是必须

## 2.1 原则：不要删除能力，先减少“同时暴露给 Agent 的能力”

不建议第一步就物理删除 44 个工具。

应采用：

> **44 个底层能力保留 + 按 skill/阶段动态暴露 6～12 个高相关工具。**

即：

- Registry 仍是唯一真源。
- MCP Server 启动时根据 `profile` 过滤工具。
- 同一个底层 tool 可以被多个 profile 复用。
- 这样既不破坏旧代码，也不会让模型每轮看见 44 个按钮。

建议增加：

```text
mcp_server.py --profile build
mcp_server.py --profile story
mcp_server.py --profile publish
mcp_server.py --profile scout
mcp_server.py --profile inspect
```

运行时由 dsh bridge 根据 skill 自动选择 profile。

## 2.2 工具分层

下面是建议的长期分类。**具体 44 个当前名字必须由运行时从 `TOOL_REGISTRY` + `EXPECT_MCP_TOOLS` 自动枚举后映射，不能手工维护“44 个工具总表”。**

### A. 必须常驻 / Core Runtime Tools

这些是 Agent 的“手脚”：

- `navigate`
- `drive_ui`
- `get_build_status`
- `list_books`
- `get_book_detail`
- `get_writing_context`
- `save_outlines`
- `save_plot_draft`
- `save_chapter_text`
- `validate_storyline`
- `validate_world`

说明：不是所有任务都需要全部暴露，但这些属于系统核心动作。

### B. 建书阶段必须

建书时优先暴露：

- `get_build_status`
- `query_profiles`（只有用户未选笔名/需要匹配时）
- `query_arc_library`
- `query_plots`
- `query_gags`（只有明确需要笑点/节奏机制时）
- `query_characters`（只有需要角色原型参考时）
- `drive_ui`
- `validate_storyline`
- `validate_world`

建书阶段不应默认暴露：

- `arc_material_candidates`：书尚未创建时如果必须依赖 `book_id`，直接从该阶段隐藏，避免出现本次运行这种空 `book_id` 调用。
- `save_chapter_text`
- `save_plot_draft`
- `publish_*`
- `fetch_*` / `read_crawled_novel` / `extract_state` 等侦察工具

### C. 写作阶段必须

推荐只暴露：

- `get_book_detail`
- `get_writing_context`
- `get_pen_style`
- `pick_plot_sample`
- `save_plot_draft`
- `save_chapter_text`
- `chapter_quality_gate`
- `save_outlines`（只有需要延伸故事线时才给）
- `validate_storyline`（扩弧后使用）

这里最关键的是：

> **普通写作不应该看到 query library 全家桶。**

因为写作时最重要的是“当前剧情事实”，不是再去重新找素材。

当前设计已经明确把 `get_writing_context` 作为写作上下文总入口；它返回最近章节、故事线、人物卡、`next_plot`、`plot_run` 和 `style_card`。fileciteturn5file7L227-L243

### D. 故事线延伸阶段必须

建议未来单独形成一个 `story-planning` profile：

- `get_writing_context`
- `save_outlines`
- `validate_storyline`
- `validate_world`
- `get_book_detail`
- 库查询工具只在模型判断“需要借鉴机制”时开放

这是“续写到规划边界时”的核心 profile。

### E. 上架阶段必须

只暴露：

- `get_book_detail`
- `save_book_meta`
- `publish_check`
- `publish_book`
- `mark_finished`
- `export_book`

不让出版流程看到大量创作工具。

### F. 侦察/提取阶段必须

只暴露：

- `list_rankings`
- `discover_hot`
- `fetch_novel`
- `list_crawled_novels`
- `read_crawled_novel`
- `extract_state`
- `judge_extraction`
- `ingest_library_assets`
- 必要的 `drive_ui` / `set_review` 相关能力

当前系统已经把侦察/提取分到 `novel-scout` skill。fileciteturn5file2L64-L73

## 2.3 “不是必须”的工具不等于删除

建议分为三档：

### P0：执行核心

任务完成必须依赖的工具。

### P1：条件工具

满足特定条件才开放，例如：

- `query_gags`
- `query_characters`
- `query_profiles`
- `get_pen_style`
- `pick_plot_sample`
- `chapter_quality_gate`
- `save_outlines`

### P2：低频/离线能力

不要进入常规创作上下文：

- 侦察抓取
- 库入库
- 发布导出
- 历史调试类辅助动作

## 2.4 工具返回必须“更像状态”，少像原始数据库

重点改：

不要：

```json
{
  "templates": [几十条完整模板]
}
```

推荐：

```json
{
  "intent": "find_arc_reference",
  "query": "基建 军事 星际",
  "matches": [
    {
      "id": "arc_x",
      "name": "...",
      "mechanism": "...",
      "useful_for": ["逆袭", "危机升级"],
      "not_to_copy": ["宗门背景", "具体角色"],
      "relevance": 0.82
    }
  ],
  "next_action_hint": "compare_or_design"
}
```

这样模型看到的是“决策素材”，不是“数据库 dump”。

---

# 3. Agent 应采用什么“思考路线”

## 3.1 不要模拟人类“碎片化思考”，而要模拟“人类创作者的决策循环”

这里说的“模拟人类思考”不是让模型输出思维链，也不是要求模型写一大段内部推理。

应该把创作者行为抽象成：

```text
观察现实/故事状态
    ↓
确认当前要解决的问题
    ↓
判断目标与约束
    ↓
生成少量候选方向
    ↓
预测每个方向带来的后果
    ↓
选一个当前最优方向
    ↓
把它落成可执行剧情
    ↓
写/执行
    ↓
观察结果与人物变化
    ↓
更新世界状态、线程、承诺
    ↓
重新判断下一步
```

这就是后续 Agent 的主循环。

## 3.2 每个剧情节点都采用“目标—阻力—选择—代价—结果”模型

每一个 plot 不应该只是：

> “主角去做 X，然后发生 Y。”

而应至少在 Agent 的临时 planning state 中回答：

```text
goal        当前谁想完成什么？
obstacle    谁/什么阻止？
stakes      失败会失去什么？
choice      有哪些合理选择？
choice_made 当前角色为什么选这个？
cost        选择带来什么代价？
consequence 这个决定改变了什么？
new_question 结果制造了什么新问题？
```

这会天然产生“像人一样的故事推进”，因为后一个节点来自前一个节点的结果，而不是来自作者机械地填下一格。

## 3.3 加一个“预测深度”，避免一次想完整本书

定义三层 horizon：

### H0：可执行区

当前 1～3 个 plot。

必须具体到人物、场景、动作、冲突和结尾状态。

### H1：近期弧区

当前弧 + 下一弧。

只要求：

- 弧目标
- 关键转折
- 进入/退出条件
- 主要 promise
- 可能的 antagonistic pressure

不要求每个 plot 都写死。

### H2：远期意图区

未来若干弧只保存：

- 主冲突方向
- 一个或两个潜在线索
- 角色成长方向
- 世界升级方向
- 未兑现承诺

允许改变。

### 核心规则

> 越远的剧情越“方向性”，越近的剧情越“执行性”。

这样就能解决“建书时不必一次构建完整故事线”的问题。

---

# 4. 故事线模型：从“全量大纲”升级为“承诺区 + 预测区”

## 4.1 不直接破坏现在的 `outlines / plots`

保留现有：

- `outlines`
- `plots`
- `threads`
- `promises`

因为它们是实际落盘和 UI 的稳定基础。当前定义明确把弧作为树状目标节点、情节段作为叶弧下的执行单元、线程作为横向叙事线索。fileciteturn7file0L45-L70

新增一个轻量 planning 层：

```json
"planning": {
  "mode": "open",
  "committed_until_word": 24000,
  "forecast_horizon_arcs": 2,
  "future_intents": [],
  "decision_points": [],
  "last_replanned_at_plot": "p08"
}
```

## 4.2 committed_storyline

`outlines / plots / threads / promises` 中已经落盘的区域，叫：

> **Committed Storyline（已承诺故事线）**

它必须满足现有硬规则：

- 顶层弧连续覆盖 committed 区间。
- plots 只能挂叶弧。
- 弧 span 与 plots words 对齐。
- `validate_storyline` 必须通过。

这完全兼容现有校验器。当前校验器已经要求顶层弧无空白、plot 只能挂叶弧。fileciteturn7file0L72-L87

## 4.3 forecast_storyline

不要直接写入 `outlines`。

用独立字段或独立文件保存：

```json
"future_plan": {
  "next_arc_intent": "...",
  "possible_turns": ["...", "..."],
  "active_threads": ["thread_main", "thread_mystery"],
  "open_promises": ["promise_03", "promise_07"],
  "character_growth": {
    "顾衡": "从个人证明走向承担系统性责任"
  },
  "world_escalation": "从工区级问题升级到天梯安全与深空威胁"
}
```

未来区不作为“当前故事线正式结构”的验证对象。

## 4.4 为什么这样做

现有系统把章节看作字数视图、plot 作为最小写作单元，这非常适合“已承诺区域”。fileciteturn7file0L74-L87

真正需要改变的是：

> **允许故事线只有“目前已经做出承诺的部分”，而不是要求把未知未来也假装成已经确定。**

---

# 5. 建书时如何构建故事线

## 5.1 建书不再追求“一次写完整本书”

建议默认模式改为：

```text
建书输入
 ↓
核心冲突
 ↓
世界与势力
 ↓
主角状态 / 角色关系
 ↓
确定开篇弧
 ↓
确定第二弧的方向
 ↓
写出前 15k～30k 字的 committed storyline
 ↓
保存 1～2 个远期方向
 ↓
结束建书规划
```

不是：

```text
直接生成 90k～180k 全书 30～60章全部 plot
```

## 5.2 初始规划长度

不要写死单一阈值，建议用动态规则：

```text
initial_horizon = max(
    1个完整开篇弧,
    2个可形成因果闭环的弧,
    约 4～10 个可执行 plot
)
```

当题材复杂时可以更长。

关键不是字数，而是：

> **必须让第一个故事阶段拥有完整的“开始—升级—阶段性结果”。**

不能只规划“第一章很精彩”。

---

# 6. 后续续写：故事线如何继续构建

## 6.1 续写不是“取下一个 plot”这么简单

当前写作入口是：

`get_writing_context → next_plot = 第一个未写 plot → 生成 → save_plot_draft → save_chapter_text`。fileciteturn7file0L72-L87

建议升级成：

```text
get_writing_context
    ↓
检查 planning boundary
    ↓
如果 committed storyline 足够长：直接写
    ↓
如果剩余 plot < threshold：触发 Replan
    ↓
Replan
  ├ 当前世界状态
  ├ 最近章节结果
  ├ 角色动态状态
  ├ 未解决 promise
  ├ 活跃线程
  ├ 当前弧目标
  ├ H1 下一弧方向
  └ H2 远期意图
    ↓
生成 1～2 个新弧候选
    ↓
选定/融合
    ↓
只生成未来 4～10 个可执行 plot
    ↓
save_outlines(append/merge)
    ↓
validate_storyline
    ↓
继续写
```

## 6.2 触发重新规划的条件

不要等“全部 plot 写完”才扩。

推荐：

```text
remaining_committed_plots <= 2
OR
remaining_committed_words < 2 * words_per_chapter
OR
current_arc_near_end AND next_arc_uncommitted
OR
new_major_decision_point == true
OR
plot_result_invalidates_forecast == true
```

这样 Agent 可以像人类作者一样边写边调整。

---

# 7. Replan：Agent 真正应该做的“故事大脑”

新增一个 skill：

```text
novel-replan
```

它不直接负责写正文，只做：

> **从最新事实状态重新设计下一段故事。**

## 7.1 Replan 输入

必须聚合：

```text
book meta
world rules
current arc target
recent chapter summaries
character dynamic states
cast relationships
open promises
active threads
written plots
unwritten plots
future_plan
style constraints（只用于风格判断，不用于情节逻辑）
```

## 7.2 Replan 输出

```json
{
  "diagnosis": {
    "current_state": "...",
    "main_tension": "...",
    "most_urgent_problem": "...",
    "reader_question": "..."
  },
  "candidate_directions": [
    {
      "name": "...",
      "benefit": "...",
      "risk": "...",
      "thread_effect": "...",
      "character_effect": "..."
    }
  ],
  "selected_direction": "...",
  "next_arc": {...},
  "next_plots": [...],
  "future_plan_updates": {...}
}
```

注意：这不是让 Agent 输出思维链，而是让它输出**决策结果**。

---

# 8. 让“人类式思考”真正进入 plot 生成

## 8.1 plot 不是模板填空

当前 plot 可以带模板、slots、roles、theme_hints、hook_points 等；这些保留。

但 Agent 在生成一个 plot 时，要先产生一个**非持久化 execution brief**：

```json
{
  "dramatic_goal": "...",
  "conflict_source": "...",
  "character_choice": "...",
  "irreversible_change": "...",
  "reader_question": "...",
  "ending_hook": "..."
}
```

然后才写 plot。

## 8.2 每个 plot 至少有一个不可逆变化

禁止连续多个 plot 只是：

- 调查一点
- 解释一点
- 再调查一点
- 再解释一点

建议硬规则：

> 每 1 个 plot 至少改变一个：
> 角色关系 / 资源 / 风险 / 信息 / 目标 / 阵营态度 / 空间位置 / power state。

这样故事自然向前。

## 8.3 “读者问题”机制

每个关键 plot 结束后产生：

```text
reader_question
```

例如：

```text
“37号舱事故真的是焊接误差吗？”
“谁在提前知道天梯故障？”
“主角为什么会被调到七号工区？”
```

下一阶段规划时优先考虑：

- 回答一个问题
- 升级一个问题
- 创造一个新问题

而不是机械推进事件。

---

# 9. 线程 / promise 应成为“故事记忆”而不是附属数据

当前线程已经定义为横向叙事轴，promise 负责设局→收局。fileciteturn7file0L45-L52

下一步优化：Replan 前先自动生成一个“故事张力面板”：

```text
MAIN_THREAD
  当前目标：...
  当前阻力：...
  最近推进：...
  再次推进窗口：...

SIDE_THREAD_A
  当前状态：pending
  最后出现：plot_x
  已沉默：3 plots

PROMISES
  高优先级未解决：3
  已推进未收局：4
  过期风险：1
```

这会让 Agent 更像在“记故事”，而不是每次重新读整本书。

---

# 10. 角色状态：从“写完记账”升级为“写前决策输入”

当前角色状态已经在 `character_states.json` 中维护，并通过 `cast_pack` 提供动态 `dyn`。fileciteturn7file0L143-L151

应增加一个写前判断层：

```text
character state
 ↓
当前目标
 ↓
当前关系
 ↓
当前弧阶段
 ↓
这个 plot 会让他/她如何改变？
```

每次写 plot 前只要求：

```json
{
  "character_impact": [
    {
      "name": "顾衡",
      "expected_change": "从证明自己转向主动承担责任"
    }
  ]
}
```

写完后再转成 `character_events`。

这样：

> **写前是预测，写后是事实。**

这正是人类作者写长篇时最重要的连续性机制之一。

---

# 11. 风格、人物、情节三条链继续分开

当前架构已经把：

- `cast_pack` → 谁在写
- `plot_run` → 写什么
- `pick_plot_sample/get_pen_style` → 怎么写

分开，这是正确方向。fileciteturn7file0L95-L98

继续保持：

```text
剧情规划器 ≠ 风格规划器
角色状态器 ≠ 故事线生成器
样文 ≠ 情节模板
```

不要为了“智能”把所有信息重新塞进一个超大 prompt。

---

# 12. Skill 重构建议

当前已经有：

- `novel-build-candidates`
- `novel-build`
- `novel-story`
- `novel-publish`
- `novel-scout`

建议增加：

```text
novel-plan
novel-replan
novel-write
novel-review
```

其中：

### `novel-plan`

职责：建书前部故事区 + H1/H2。

### `novel-replan`

职责：剧情写到边界后，根据最新事实延伸。

### `novel-write`

职责：只负责写当前 plot / chapter，不负责大规模重新规划。

### `novel-review`

职责：检查：

- 因果
- 连续性
- 人物行为
- 线程
- promise
- 读者问题
- 爽点/节奏

这比让一个 `novel-story` skill 既负责选弧、排 plot、写正文、检查、重规划更稳定。

---

# 13. dsh / Prompt 架构优化

## 13.1 system prompt 不再承担流程

继续保持三层：

```text
Persona
Workspace contract
Task
```

当前 dsh 已采用这一分层，且任务文本主要承载历史回放 + 当前任务。fileciteturn6file9L228-L236

优化点：

### Persona

只描述身份和总原则。

### Workspace contract

只描述不可违反的事实规则：

- 数据字段
- 工具契约
- 安全/护栏

### Skill

描述：

- 本阶段目标
- 输入
- 输出
- 步骤
- stop conditions
- allowed tools/profile

### Task

只保留：

- 用户当前意图
- 当前阶段
- 极短历史

不要再把整套流程重复塞进任务文本。

---

# 14. 建议引入“Agent State”

不要让 Agent 完全靠聊天历史记忆创作状态。

增加持久化：

```text
storage/agent_runs/<book_id>/planning_state.json
```

建议结构：

```json
{
  "book_id": "...",
  "phase": "story",
  "current_arc_id": "A2",
  "current_plot_id": "p17",
  "planning_mode": "open",
  "committed_until_word": 42000,
  "horizon": {
    "h0": 3,
    "h1": 2,
    "h2": 5
  },
  "story_questions": [
    {
      "question": "...",
      "priority": 0.82,
      "status": "open"
    }
  ],
  "active_threads": [...],
  "critical_promises": [...],
  "character_intents": [...],
  "future_plan": {...},
  "last_replan_reason": "...
}
```

注意：这个状态是 Agent 的“规划记忆”，不取代 `storyline.json`。

---

# 15. 推荐新增/优化工具

不要新增一堆工具。只新增真正减少 Agent 认知负担的几个“聚合工具”。

## P0：新增 `get_story_state`

一次返回：

```text
current arc
current plot
recent chapters
character dynamics
active threads
open promises
planning boundary
future intents
```

这样可以减少 Agent 自己拼装上下文。

## P0：新增 `extend_storyline`

输入：

```json
{
  "book_id": "...",
  "outlines": [...],
  "plots": [...],
  "threads": [...],
  "future_plan": {...},
  "mode": "append"
}
```

内部负责：

- 合并
- 校验 id
- 校验叶弧
- 计算跨度
- 更新 planning boundary
- 调 `validate_storyline`

目的：不要让 Agent 自己连续调用多个低层 save 工具完成同一个“扩写故事线”动作。

## P1：新增 `get_planning_candidates`

仅在 replan 时用。

输入：当前 state + 主题/标签。

输出：3～5 个高质量“剧情机制方向”，不是直接返回几十个模板。

可以内部组合：

- `query_arc_library`
- `query_plots`
- `query_gags`

这样 Agent 不必自己做重复查询。

## P1：新增 `commit_story_plan`

把 Agent 的 planning state 中选定的：

- next arc
- next plots
- future intents

正式提交为 storyline + planning state。

---

# 16. 建议工具路由器

新增一个纯规则层：

```text
agent_tool_router.py
```

根据：

```text
phase
skill
book_exists
book_phase
task_intent
```

返回：

```json
{
  "profile": "story",
  "allowed_tools": [...],
  "required_tools": [...],
  "forbidden_tools": [...]
}
```

注意：路由器必须是**规则系统，不是 LLM**。

这样“当前能不能调这个工具”不需要 Agent 思考。

---

# 17. 本次实际运行应如何被改写

当前运行发生了：

1. 先拿 `get_build_status`。
2. 因为 `book_id` 为空仍尝试 `arc_material_candidates`。
3. 再查 `query_profiles`。
4. 再多次试弧库关键词。
5. 再多次试 plot 库关键词。
6. 中途多次讨论 submit 权限。
7. 最终完成世界观、弧树、人物、校验。

其中最终结果质量不错：故事线验证通过，总字数 112000，5 个顶层弧、15 个叶弧、62 个 plot；world 验证也通过。fileciteturn3file3L130-L155

但执行路径过长。

## 改造后的运行顺序

```text
[1] read phase/build state
[2] resolve task → novel-build
[3] auto-select MCP profile=build
[4] check prerequisite
    if book_id empty:
       hide arc_material_candidates
[5] one bundled library reconnaissance
    → arc refs + plot refs
[6] generate 2～3 radically different story directions
[7] choose one
[8] build core_conflict
[9] build factions
[10] build committed story horizon
[11] build characters
[12] validate storyline + world
[13] self-review: causality / appeal / character pressure / thread / promise
[14] revise only when needed
[15] save planning state
[16] stop and report
```

关键区别：

> **“先发现可用工具”是系统工作；“如何讲故事”才是 Agent 工作。**

---

# 18. 建书时建议的 Agent 决策模板

skill 中不要要求模型输出长思维链；使用短结构化决策表：

```text
STORY DECISION

Core conflict:
...

Opening promise:
...

Current protagonist pressure:
...

Arc 1 goal:
...

Arc 1 irreversible change:
...

Arc 2 direction:
...

Active threads:
...

Critical promises:
...

Why this version is different:
...

What remains intentionally unresolved:
...
```

这份内容可以被保存到 planning_state，而不是塞进用户聊天。

---

# 19. 续写时建议的 Agent 决策模板

每次触发 replan：

```text
CURRENT STATE
- where are we?
- what changed?
- what is now impossible?

TENSION
- who wants what?
- why can't they get it?
- why now?

READER
- what question is currently open?
- what expectation should be paid off next?

CHARACTER
- who is being forced to choose?
- what choice reveals character?
- what will change afterwards?

STORY
- which thread advances?
- which promise is paid / delayed / transformed?
- what new pressure is created?

PLAN
- next arc intent
- next 3-8 plots
- future intent changes
```

---

# 20. 读取上下文的优化原则

当前 `get_writing_context` 已经是核心聚合工具。fileciteturn5file7L227-L243

未来应进一步让它返回“分级上下文”：

```json
{
  "critical": {...},
  "current": {...},
  "recent": [...],
  "background": {...},
  "forecast": {...}
}
```

优先级：

```text
critical > current > recent > forecast > background
```

这样模型不会被全量历史淹没。

尤其：

- 人物：只给 active cast 的完整卡 + referenced 的紧凑卡。
- 线程：只给 active threads 全量状态。
- promise：优先高风险 promise。
- 历史正文：只给最近摘要和当前必要片段。

当前系统已经避免把整套动态角色状态块直接灌进 prompt，而更多通过 `cast_pack` / 状态机 / gate 使用；这一方向继续保持。fileciteturn7file0L143-L160

---

# 21. Validate 不应该只验证“结构正确”

当前 `validate_storyline` 和 `validate_world` 已很好地做硬规则校验。

但 Agent 自检还要增加四个软指标：

```text
causal_score
character_choice_score
thread_progress_score
reader_pull_score
```

建议由 Agent 的 review skill 输出结构化结果：

```json
{
  "passed": true,
  "scores": {
    "causal": 0.91,
    "character_choice": 0.88,
    "thread_progress": 0.84,
    "reader_pull": 0.93
  },
  "problems": [],
  "revision_needed": false
}
```

注意：这不是硬规则，不应该把主观评分强行塞进平台底层 validator。

---

# 22. 关于现有双进程架构：暂不推翻，但要降低其“状态不一致成本”

当前 Web 与 MCP 是两个独立进程，通过 `books/` JSON 与 `storage/` 协调，并依靠 `BookLock` 避免写冲突。fileciteturn4file2L69-L72

短期建议：

1. `BookLock` 保留。
2. 所有重大 storyline 写操作增加 version / revision。
3. `get_story_state` 带 `storyline_revision`。
4. Agent 提交时带 `expected_revision`。
5. revision 不一致则返回：

```json
{
  "error": "stale_storyline",
  "expected": 12,
  "actual": 13,
  "action": "refresh_and_replan"
}
```

这样比单纯依赖文件锁更能解决“浏览器修改 + Agent 写入”时的陈旧状态问题。

---

# 23. dsh Runtime / Skill 镜像优化

当前 `.dsh/skills` 是运行期镜像，缺失时会静默回退到裸 MCP，这是高风险行为。fileciteturn6file2L79-L85

必须改成：

```text
skill missing
   ↓
NOT_FOUND
   ↓
禁止继续执行对应 skill
   ↓
明确错误
```

绝不能静默变成：

> “没有 skill，那我就自己看 MCP 试试看。”

同时建议：

- skill source 带版本号
- `.dsh/skills` 镜像带 source digest
- dsh bridge 启动时检查 digest
- 不一致直接报错而不是默默运行旧版本

---

# 24. Prompt / Persona / Contract 去重

当前 `NOVEL_AGENT.md` 与工具 docstring 存在双份契约；persona 又存在 `_PERSONA`、overlay、`cordis.patch.yml` 三处同步。fileciteturn7file0L9-L15

建议：

### 单一真源

```text
schemas/tool_contracts.py
schemas/story_contract.py
schemas/agent_policy.py
```

### 自动生成

从这些真源生成：

- tool docstring 摘要
- `NOVEL_AGENT.md` 机器可读部分
- skill reference snippets
- MCP tool descriptions

目标：

> 修改一个字段，不再需要手工同步三四处。

---

# 25. 第一阶段实施顺序（最重要）

不要一次重构所有代码。

## Phase 1：工具减负

实现：

- `mcp_server --profile xxx`
- 工具自动分类
- 建书 / 写作 / replan / publish / scout 五个 profile
- 建书隐藏 `arc_material_candidates` 等明显不适用工具
- 运行时 profile 可记录到日志

验收：

> 同一次建书任务，Agent 看见的工具数量 ≤ 12。

## Phase 2：Planning State

实现：

- `storage/agent_runs/<book_id>/planning_state.json`
- `committed_until_word`
- H0 / H1 / H2
- open questions
- future intents
- last replan

验收：

> 新一轮 Agent 不需要靠聊天历史猜“我们走到哪里了”。

## Phase 3：续写自动 Replan

实现：

- boundary detection
- `novel-replan` skill
- 自动扩弧
- `extend_storyline`
- validate

验收：

> 写到已规划区域末尾前，系统自动准备下一小段故事；不需要用户手工要求“再帮我想 5 万字”。

## Phase 4：故事质量循环

实现：

- reader_question
- character_impact
- irreversible_change
- soft review score

验收：

> 连续 10 个 plot 中，不能大量出现“只有信息增加、没有关系/风险/选择变化”的平推剧情。

## Phase 5：状态版本化

实现：

- storyline revision
- optimistic concurrency
- stale-state error

验收：

> Web / MCP / Agent 同时操作时，不会静默覆盖。

---

# 26. 建议保留的原有核心闭环

不要破坏这个已经成熟的写作闭环：

```text
get_writing_context
      ↓
agent 生成 plot
      ↓
save_plot_draft
      ↓
达到章节条件
      ↓
save_chapter_text
      ↓
去 AI 味 / 审查 / 字数门禁
      ↓
角色状态更新
      ↓
promise ledger 更新
      ↓
chapter_quality_gate
```

当前系统已经形成这条链路，应该在它外面增加“planning/replan”，而不是把写作引擎全部推翻。fileciteturn7file0L76-L87

---

# 27. 最终目标架构

```text
                          ┌─────────────────────┐
                          │      User Intent     │
                          └──────────┬──────────┘
                                     ↓
                          ┌─────────────────────┐
                          │   Task / Skill      │
                          │      Router         │
                          └──────────┬──────────┘
                                     ↓
                    ┌─────────────────────────────────┐
                    │     Agent Planning Layer         │
                    │                                  │
                    │ H0 executable                   │
                    │ H1 near-future arc              │
                    │ H2 future intent                │
                    │ story questions                 │
                    │ character intent                 │
                    │ promises / threads              │
                    └──────────────┬──────────────────┘
                                   ↓
                    ┌─────────────────────────────────┐
                    │      Minimal MCP Profile        │
                    │ build / story / replan / etc.   │
                    └──────────────┬──────────────────┘
                                   ↓
                    ┌─────────────────────────────────┐
                    │     Thin Platform Tools         │
                    │ write / query / validate        │
                    │ guard / lock / persistence      │
                    └──────────────┬──────────────────┘
                                   ↓
         ┌─────────────────────────┼─────────────────────────┐
         ↓                         ↓                         ↓
   storyline.json            character_states        promise ledger
         ↓                         ↓                         ↓
         └───────────────────── Story State ─────────────────┘
                                   ↓
                               Review
                                   ↓
                         Boundary / Replan check
                                   ↓
                        extend next story horizon
                                   ↓
                                Continue
```

---

# 28. Local Agent 执行要求

本计划执行时，严格按以下顺序做代码工作：

### Step 1

先扫描并输出当前真实 MCP 工具集合：

```text
TOOL_REGISTRY
EXPECT_MCP_TOOLS
PHASE_GATES
_LOCKED_TOOLS
```

生成机器可读：

```text
storage/tool_profiles.json
```

### Step 2

实现 profile 过滤，但不得修改底层工具行为。

### Step 3

实现 planning_state，但先只读，不接管现有 storyline。

### Step 4

增加 `get_story_state`。

### Step 5

增加 boundary detector。

### Step 6

增加 `novel-replan` skill。

### Step 7

增加 `extend_storyline`；内部复用现有 `save_outlines` + validator，不重复实现规则。

### Step 8

加入 revision/version 检查。

### Step 9

最后才调整 UI / Gantt，让“未来规划”与“已承诺故事线”视觉分开。

---

# 29. 不允许的优化方向

以下方案不要做：

1. 把所有 MCP 工具合并成一个超级万能工具。
2. 把所有故事规划逻辑硬编码进 Python，导致 Agent 失去创作能力。
3. 建书一次生成完整 100k+ 字全部 plot，并把它作为强制完成条件。
4. 把未来预测直接当成正式 storyline，导致后续修改成本越来越高。
5. 把角色状态、风格样文、世界观、线程、全文历史重新全部塞进每轮 prompt。
6. 让 Agent 自己通过试错决定“哪个工具能用”。
7. skill 缺失时静默退化成裸 MCP。
8. 为了“人类思考”要求模型输出私有思维链；只保存结构化决策结果。

---

# 30. 最终成功标准

完成后，NovelEngine 的 Agent 应该表现为：

### 建书时

> “我先明确核心矛盾，再做 2～3 个故事方向比较，然后把前部故事承诺下来；远期只留方向。”

### 写作时

> “我先看现在发生了什么，再决定当前人物最合理的选择；写完后记录这个选择造成的状态变化。”

### 续写时

> “发现规划只剩 2 个 plot，我不等它耗尽，而是根据最新状态重新设计下一段。”

### 故事线变化时

> “以前的预测被新剧情推翻了，我修改未来，不修改已经写成的事实。”

### 工具调用时

> “我知道当前阶段只需要 7 个工具，我不会为了寻找按钮浪费上下文。”

最终形成：

```text
稳定事实
    +
短期确定
    +
中期半确定
    +
远期可变
    +
持续重规划
    =
真正适合长篇小说的 Agent 架构
```

---

# Appendix A：现有架构事实基线

当前系统的正式定义是：平台由外部 Agent 驱动，MCP 为薄工具面，LLM 生成发生在 Agent 侧；Web 与 MCP 双进程运行，写操作依赖 BookLock，storyline / chapters / draft / character state 分文件落盘。fileciteturn1file1L27-L54

当前阶段与 skill 大致为：建书 `novel-build-candidates → novel-build`，写作 `novel-story / novel-write`，发布 `novel-publish`，侦察 `novel-scout`。fileciteturn5file2L64-L73

当前 dsh 采用 persona / workspace instruction / task 三层结构，并通过运行期 overlay 配置 MCP、超时、events runner 等。fileciteturn6file3L97-L105

当前写作上下文已经集中到 `get_writing_context`，并且人物注入、plot 目标、风格引用三条链已经结构化。fileciteturn7file0L109-L135

当前实际建书运行结果证明：即使库素材较少，Agent 仍能基于自身设计生成完整弧树，并最终通过 storyline/world 两类校验。fileciteturn3file3L130-L155

---

# Appendix B：建议第一版先实现到什么程度

**第一版不要追求所有规划器都完成。**

最优先的最小闭环只有 4 件事：

```text
1. MCP profile
2. planning_state
3. boundary detector
4. novel-replan + extend_storyline
```

只要这四项工作完成，就已经能把架构从“整本书一次性规划 + 顺序写”升级成：

> **局部承诺 + 持续续写 + 自动续规划。**

其余质量评分、reader question、高级线程策略、UI 美化都可以第二阶段再做。
