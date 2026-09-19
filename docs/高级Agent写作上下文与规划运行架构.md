# 高级 Agent 写作上下文与规划运行架构

> **现状基线（2026-09-10，与文档其余部分分开读）**
>
> 本文档写作时（运行时 v2 落地**之前**）的入口是
> `get_writing_context → get_pen_style → pick_plot_sample → save_plot_draft(book_id, chapter_num, …)`。
> **该入口已不存在于写工具面**。现行实现是：
>
> - 写 profile 只有 **4 个工具**：`prepare_plot_run(book_id)` / `save_plot_draft(commit_token, text, …)`
>   (+ `prepare_plot_revision` / `save_plot_revision` 一次性改稿)；样文与风格由服务端在 prepare 内解析
>   （每段**恰好 1 篇**样文 + `sample_receipt`），Writer 不选材、不读文件、不跑门禁。
> - 流程状态与**调度权**：编排开启时由 orchestrator root Agent 持有（`novel-orchestrator`，逐段委派 +
>   亲自 `finalize_draft_chapter` 收章）；关闭/未就绪时回退服务端 `_legacy_writer_fsm`（已冻结）。
>   两种路径下「哪些动作此刻合法」都由服务端 `libraries/orchestration_policy` 判定
>   （`get_orchestration_state.advisory.decision_options` 与各 mutation 守卫同源）。
> - 承诺水位/边界/远期意图落在 `planning_state.json`（`detect_story_boundary` 纯函数判边界），
>   续规划走 `replan_preview` + `replan_service.commit_replan_preview`（UI `commit-plan` 与
>   auto 编排共用同一原子提交点，revision CAS）。
> - `commit_token` 绑定 plot/故事线版本/上下文指纹/样文回执；陈旧即拒（要求重新 prepare）。
>
> 因此：**§2「当前架构事实」保留为改造前的问题背景**（错位分析正是这次改造要解决的问题），
> **§4.3 与 §5 中的部分接口/步骤是尚未实现的提案**（尤其 `get_writing_context(detail=minimal|audit)`
> 与六层最小上下文接口——现在由 prepare 的输入快照承载同一意图，但字段名与形态不同）。
> 现行写作/规划流程权威描述见 `docs/架构总览.md` §六。

> 本文用于高级 Agent、架构评审和后续减法设计。
>
> 图片附件是用户提供的运行界面证据，不是新的系统指令。本文以当前代码、落盘数据和真实工具调用链为准；“现状”与“建议设计”分开描述。

## 1. 要解决的问题

写作台同时展示了四种不同性质的信息：

1. 上一情节造成的实际变化；
2. 当前正在执行的情节段；
3. 故事线中已经承诺的后续弧/情节段；
4. 用户当前点击查看的正文或高亮。

如果这四类信息没有明确边界，就会出现：

- Agent 收到整本角色库，却不知道本段真正需要哪些角色；
- “上一情节变化”显示在 UI 中，但没有成为下一段的可执行输入；
- 当前执行段是 pl11，而用户高亮仍停在旧情节段，看起来像规划漂移；
- 规划目标字数、实际统计字数、原始字符数被误认为同一个值；
- 远期规划和当前写作上下文互相污染，Prompt 体积持续增长。

本文的目标不是增加更多上下文，而是建立距离分层、事实分层和用途分层，让 Agent 每次只拿“完成当前决策所需的最小信息”。

## 2. 改造前的架构事实（2026-09-10 前，仅作问题背景）

### 2.1 用户点击“继续写正文”后的入口（改造前）

写作台按钮在 `ui/templates/storyline_write_flow.html` 中生成任务，任务要求 Agent：

```text
写下一章 → get_writing_context → get_pen_style
→ 每个情节段生成正文 → save_plot_draft
→ 章满后 save_chapter_text
→ chapter_quality_gate
```

当前写作 profile 的工具面主要是：

- `get_writing_context`
- `get_pen_style`
- `pick_plot_sample`
- `save_plot_draft`
- `save_chapter_text`
- `chapter_quality_gate`

一次真实写作任务通常是：

```text
读取写作上下文
读取完整笔名规则
读取当前情节段样文
写 pl08，保存草稿
重新读取上下文
读取 pl09 样文
写 pl09，保存草稿
提交整章
执行质量门禁
```

重要边界：工具返回“已提供上下文”，不等于系统已经证明 Agent 在语义上遵守了上下文。可验证的只能是调用收据、版本、角色列表、样文 ID 和结构化结果。

### 2.2 `get_writing_context` 当时提供的内容（该工具已移出写工具面）

当前上下文主要由 `agent_tools.py:get_writing_context` 组装，来源包括：

| 层 | 内容 | 性质 | 用途 |
|---|---|---|---|
| 书配置 | 书名、平台、章节号、每章字数 | 事实 | 章节任务与门禁 |
| 故事线 | 弧、情节段、线程、承诺、世界观 | 事实/规划 | 当前段定位和约束 |
| `next_plot` | 第一个未写且不在草稿中的情节段 | 动态事实 | 决定下一段写什么 |
| `plot_run` | 当前 plot、弧目标、线程、承诺、角色包、样文 query | 运行快照 | 当前段最小执行上下文 |
| 前章信息 | 章节摘要、草稿、已写字数 | 历史事实 | 连贯性与断点续写 |
| `character_intents` | 章末规划更新的人物意图 | 规划预测/事实观察 | 下一阶段方向提示 |
| `style_card` | 笔名精简规则 | 风格约束 | 防风格漂移 |
| `context_fingerprint` | 服务端签发的上下文指纹 | 调用收据 | 保存时校验版本一致性 |

现有实现已经加入运行态投影，返回：

```json
{
  "planned_prose_units": 1300,
  "committed_words": 11667,
  "draft_words": 0,
  "display_written_words": 11667,
  "committed_raw_codepoints": 14985,
  "draft_raw_codepoints": 0
}
```

其中 `planned_prose_units` 是规划目标，`committed_words` 是系统实际统计字数，`raw_codepoints` 是原始 Unicode 字符数，三者不能混用。

### 2.3 “上一情节造成的变化”的真实流转

当前事实流转如下：

```text
Agent 生成 outcome / character_events
        ↓
save_plot_draft 写入 draft_chapter.json
        ↓
save_chapter_text 搬运结构化 facts
        ↓
reconcile Prediction → Fact
        ↓
角色状态机 + planning_state.character_intents
        ↓
下一次 get_writing_context
```

这意味着“上一情节造成的变化”不是一个独立的长期记忆库，而是由三部分组成：

- `facts`：已经发生的结构化事实；
- `character_states`：人物的动态状态；
- `planning_state.character_intents`：下一阶段方向和观察。

UI 中的“上一情节造成的变化”由最近 bridge 的事实和 reconcile 结果渲染。它是可视化结果，不应被视为另一份事实源。

### 2.4 当前情节段的真实来源

当前情节段由 `_next_plot(tl, draft)` 动态计算：

```text
written_chapter == 0
且不在当前 draft bridges 中
的第一个 plot
```

因此要区分：

- `next_plot`：系统认为下一步应写什么；
- `plot_run`：当前一次运行的上下文快照；
- 用户点击的高亮 plot：页面交互状态。

三者不是同一个状态。当前实现已在写作台增加“执行”和“选中”的并行标记，避免 Agent 正在写 pl11、用户却仍查看旧段时产生误解。

### 2.5 远期规划的真实来源

远期规划来自 `planning_state.json` 和 storyline：

- `committed_until_word`：已经正式规划并承诺的字数边界；
- `horizon`：近期可执行情节段、近弧和远期意图的数量层级；
- `future_intents`：还没有展开成 plot 的方向；
- `story_questions`：读者问题状态机；
- `character_intents`：人物下一阶段意图；
- `decision_points`：需要 Agent 或用户处理的决策点。

远期规划不是当前段的正文 Prompt。它只负责回答：

```text
当前段写完后，故事允许往哪些方向继续？
哪些问题必须保持开放？
哪些承诺临近边界，需要触发 replan？
```

## 3. 当前实现中已经暴露的错位

### 3.1 角色上下文过宽且标注不完整

角色信息理论上由 `plot.roles` → `cast_pack` 过滤，但旧数据中很多 plot 只有主角，例如多人场景仍然是：

```json
"roles": ["沈炽"]
```

这会导致：

- `cast_pack.active` 为空；
- `style_query.cast` 错误地变成 `solo`；
- 样文选成单人场景；
- UI “出场人物”与实际正文不一致。

因此角色减法的第一步不是压缩角色卡，而是先保证 plot 角色标注可信。

### 3.2 当前 plot 的详细规划字段经常为空

`execution_brief`、`character_impact`、`expected_facts` 都可能为空。UI 虽然有展示位，但空字段不能被解释为“Agent 已经用过”或“没有规划”，只能说明建线时没有提供该字段。

其中：

- `execution_brief` 适合描述本段戏剧目标、冲突、选择和结尾钩子；
- `character_impact` 是自然语言预测，可为空；
- `expected_facts` 是机器对账预测，可为空，但有值时必须可校验。

### 3.3 规划状态不是全部事实源

`planning_state` 是可重建导航状态，storyline、章节和结构化 facts 才是事实源。`current`、`active_threads`、`tension` 如果被 Agent 自由修改，很容易变成第二套陈旧事实。

建议把当前 plot 和活跃线程优先动态投影；规划文件只保存预测、问题和意图。

### 3.4 字数不是一个数字

当前系统至少存在三种字数：

1. `planned_words`：情节段规划目标；
2. `count_prose_units`：中文字符 + 英文单词，用于门禁和进度；
3. `len(text)`：原始 Unicode code point 数，用于 spans 和原文长度。

页面高亮还会受 CSS columns 分页影响，当前屏幕看到的只是 bridge 的可见片段，不代表整个情节段只有这些字。

## 4. 面向高级 Agent 的“减法”上下文设计

### 4.1 设计原则

每次上下文只回答当前决策需要的问题：

```text
我现在写哪个 plot？
这段必须延续哪些事实？
这段需要哪些人物？
这段处于哪个弧/线程/承诺链？
前文哪些内容必须逐字保留，哪些只需摘要？
写完后下一步可能往哪里走？
```

不再默认发送整本角色库、整条故事线和全部正文。

### 4.2 上下文分层

建议将每次写作输入拆成六层：

#### A. 当前执行层（必须完整）

- `plot_id`、名称、类别、目标字数；
- 当前弧目标和当前线程；
- 本段 `execution_brief`；
- 本段 hook、设局/收局关系；
- 本段 `context_fingerprint`。

这是唯一直接决定“本段写什么”的层。

#### B. 当前人物层（必须完整，但只给出场角色）

只发送 `plot.roles` 对应的角色卡：

- 身份、势力、与主角关系；
- 性格；
- 口头禅和禁用表达；
- 当前目标、位置、弧阶段、信任状态；
- 本段允许发生的变化。

主角不再天然获得所有配角信息。只有满足以下条件才追加角色：

- plot.roles 明确标注；
- 当前正文/当前 bridge 直接涉及；
- 当前承诺或收局关系需要其出现。

#### C. 最近剧情层（前两段完整）

保留：

- 当前情节段前一段的完整正文或高保真摘要；
- 前两段的正文末尾和结构化 facts；
- 当前草稿中本段已经写出的内容。

这里用于保证场景连续、人物接续和即时因果。

#### D. 中程记忆层（按距离压缩）

建议按 plot 距离分层：

| 距离 | 发送内容 | 压缩程度 |
|---|---|---|
| 前 1–2 个 plot | 正文/高保真摘要 + facts | 不压缩或轻压缩 |
| 前 3–6 个 plot | 每段 100–250 字摘要 + 变化事实 | 中压缩 |
| 当前弧更早部分 | 弧目标、已完成事件、未兑现承诺 | 高压缩 |
| 更早弧 | 弧摘要、关键人物变化、世界规则变化 | 极高压缩 |
| 已闭合且无后续影响内容 | 只保留索引和结果 | 可省略 |

压缩结果必须是结构化摘要，不允许把推断伪装成事实。建议每个摘要包含：

```json
{
  "plot_id": "pl08",
  "summary": "……",
  "facts": ["……"],
  "character_changes": ["……"],
  "open_questions": ["……"],
  "promises_touched": ["……"],
  "based_on_revision": 3
}
```

#### E. 远期规划层（只保留导航）

不要把未来所有 plot 正文送入 Prompt，只保留：

- 当前弧之后 1–2 个弧的目标；
- `horizon.h0` 的可执行 plot 名称和目标字数；
- 未解决读者问题；
- 临近兑现的承诺；
- 触发 replan 的边界和原因。

远期层不应包含所有配角卡，也不应覆盖当前弧的即时冲突。

#### F. 风格与样文层

- 笔名硬规则单独读取一次；
- 当前 plot 每次只取一篇样文；
- 样文 query 使用已有高置信维度；
- 样文只负责语言惯性，不负责剧情模板；
- 保存 `sample_receipt`，便于审计 Agent 到底拿了哪篇样文。

### 4.3 建议的最小上下文接口（**尚未实现**；现行由 `prepare_plot_run` 快照承载）

可将 `get_writing_context` 的完整返回拆成默认精简模式和审计模式：

```text
get_writing_context(
  book_id,
  plot_id?,
  detail="minimal" | "audit",
  recent_plots=2,
  middle_summary_depth="auto"
)
```

`minimal` 返回当前写作所需的六层最小块；`audit` 才返回完整故事线、全量规划和字段诊断。

最小模式必须返回：

```json
{
  "current_plot": {},
  "current_arc": {},
  "current_thread": {},
  "previous_change": {},
  "cast_pack": {},
  "recent_context": [],
  "compressed_context": [],
  "planning_horizon": {},
  "style_query": {},
  "context_fingerprint": "..."
}
```

## 5. 推荐的完整运行流程（阶段 1–3 已按 prepare/save 落地，其余为推荐目标）

### 阶段 0：用户启动

1. 页面读取当前书籍状态；
2. 从 storyline + draft 动态计算下一 plot；
3. UI 显示“下一待写”和当前用户选中状态；
4. Agent 任务携带 book_id、chapter_num，不携带整本正文。

### 阶段 1：Agent 建立本次 Plot Run（现行= `prepare_plot_run`，一次一段）

1. `get_writing_context(detail=minimal)`；
2. 校验 `phase`、章节号、边界、draft 和 `context_fingerprint`；
3. 读取 `get_pen_style(no_ref=true)`；
4. 根据 `style_query` 调 `pick_plot_sample`；
5. 保存样文收据和当前 plot 收据。

### 阶段 2：组装写作输入

Agent 按顺序合并：

```text
当前 plot 目标
→ 当前弧/线程/承诺
→ 上一情节结构化变化
→ 当前出场人物卡
→ 最近两段正文/摘要
→ 距离分层的中程摘要
→ 远期规划导航
→ 笔名规则与唯一样文
```

禁止把“远期规划”当成当前段硬事件；禁止把人物预测当成已经发生的事实。

### 阶段 3：生成并保存情节段（现行= `save_plot_draft(commit_token, …)`，服务端校验令牌）

Agent 生成正文时同时产出：

- `outcome`：选择、信息、关系、资源、承诺和新问题；
- `character_events`：本段真正造成的人物变化；
- `expected_facts`：可比较的写前预测；
- `run_id`、`based_on_storyline_revision`；
- `context_fingerprint`、`sample_receipt`。

调用 `save_plot_draft` 后，系统校验：

- 当前 plot 未被其它运行抢占；
- run 和 revision 匹配；
- 收据匹配；
- draft 章节号匹配；
- 角色和结构化字段格式正确。

### 阶段 4：段后自检与下一段

保存成功后，Agent 重新读取精简上下文，而不是继续依赖旧 Prompt。重新读取的目的只有三个：

1. 确认上一段事实已经落账；
2. 获取下一个 plot；
3. 重新计算最近正文窗口和人物动态。

### 阶段 5：章末提交（现行=服务端 FSM 调 `finalize_draft_chapter`，Writer 不参与）

1. 从 draft bridges 组装 canonical content；
2. 校验所有本章 bridges 均已提交；
3. 执行规则去 AI 味和章节门禁；
4. 落盘章节、bridge、spans、角色事实和 planning patch；
5. 重算章节和全书实际字数；
6. 清理 draft；
7. 返回 Prediction → Fact reconcile。

### 阶段 6：质量门禁

`chapter_quality_gate` 只做规则聚合和决策点输出：

- 字数；
- 风格违规；
- 连续性；
- 追读与爽点；
- 承诺兑现；
- 上下文/结构化收据完整度。

它不能声称已经证明 Agent 在语义上遵守了所有上下文，只能报告“哪些输入被读取、哪些结构化结果已提交”。

### 阶段 7：章末规划与 replan（现行=FSM 自动交接计划器 + `replan_service` 原子提交）

章末只上报增量：

- 新读者问题；
- 已推进/已回答问题；
- 人物下一阶段意图；
- 临近承诺；
- 需要人工决策的漂移。

当 `planning.boundary.needs_replan=true` 且当前无可写承诺 plot：

1. 先保存当前章；
2. 不在 write 轮内扩弧；
3. 输出 `[NEED_REPLAN]`；
4. 由 replan 流程生成新的弧/plot 预览；
5. 用户确认或按策略原子提交；
6. 新规划提高 storyline revision，下一写作轮重新建立上下文。

## 6. 状态与字段归属建议

| 状态 | 权威来源 | 是否进入当前 Prompt |
|---|---|---|
| 已发生剧情事实 | chapter bridges / reconcile facts | 只取相关事实 |
| 人物动态状态 | character_states | 只取出场角色 |
| 当前 plot | storyline + draft 动态投影 | 必须 |
| 当前弧/线程 | storyline 动态投影 | 必须 |
| 最近正文 | chapters + draft | 最近两段 |
| 中程记忆 | 结构化压缩摘要 | 按距离取 |
| 远期规划 | planning_state forecast | 只取导航摘要 |
| 风格规则 | profile/style md | 必须 |
| 样文 | sample pool | 每段一篇 |
| 用户选中高亮 | 浏览器本地状态 | 不进入 Agent Prompt |

## 7. 实施优先级

### P0：先保证不再错位

- plot.roles 与角色 Bible 校验；
- 当前执行、下一待写、用户选中三状态分离；
- 字数三口径明确显示；
- 当前 plot / arc / thread 统一动态投影。

### P1：做上下文减法

- 新增 minimal context；
- 最近两段完整、中程摘要、远期导航分层；
- 当前角色只发送 plot.roles 对应角色；
- 保存上下文和样文收据。

### P2：增强可靠性

- protocol v2 收据硬校验；
- canonical bridge 提交；
- pending commit 恢复；
- 影子校验指标和审计报告。

### P3：历史迁移

只自动做确定性操作：

- 重算字数；
- 有 bridges 时重建 spans；
- 从 storyline + draft 投影当前 plot。

不从旧正文自动猜测角色、线程、人物意图、tension 或新的剧情事实；不确定内容进入人工复核清单。

## 8. 验收标准

- 高级 Agent 默认拿到的是当前 plot 所需的最小上下文，而不是整本角色库；
- 上一情节变化能明确区分“已发生事实”和“下一步意图”；
- 当前 plot、当前弧、当前线程和远期规划在三个 API 中一致；
- 前两段正文高保真保留，更早内容按距离逐级压缩；
- 当前执行情节与用户高亮情节可以同时存在，不互相覆盖；
- 规划目标、实际统计字数和原始字符数不会再混称；
- 样文、角色包、上下文版本都有可追溯收据；
- 旧书迁移不产生未经确认的语义事实；
- Agent 未提交必要收据或 bridge 时，协议 v2 任务不能落盘。
