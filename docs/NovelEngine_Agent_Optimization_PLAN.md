# NovelEngine Agent / MCP / 增量故事线架构优化执行 PLAN

> **用途**：把本文件直接交给本地 Coding Agent，在 `NovelEngine` 仓库内执行。
>
> **性质**：这是实施计划，不是讨论稿。Agent 必须先扫描真实代码，再按阶段修改；不得把本文里的示例文件名、工具数、字段名当成比当前代码更高优先级的事实。
>
> **总目标**：让 Agent 从“拿着全部 MCP 工具边试边想、一次性把全书规划完”升级为“先由系统确定当前可用能力，再由 Agent 像长篇作者一样基于事实状态做局部推演、写作、观察结果、持续重规划”。

---

## 0. 执行约束：Local Agent 先读这里

执行本计划时必须遵守以下原则：

1. **先扫描，后修改。** 第一轮只确认真实代码结构、`TOOL_REGISTRY`、`EXPECT_MCP_TOOLS`、`PHASE_GATES`、锁、skill 加载逻辑、storyline/character/promises 的真实落盘位置。
2. **现有护栏优先保留。** `PHASE_GATES`、`BookLock`、`LoopGuard`、结构 validator、用户确认边界不得因优化而被绕过。
3. **不物理删除 MCP 能力作为第一步。** 先减少“当前任务暴露给 Agent 的工具”，稳定后再讨论废弃工具。
4. **不要把创作逻辑硬编码进 Python。** 平台负责事实、状态、路由、权限、校验、持久化；LLM 负责剧情判断、候选比较、人物选择、因果推演与文本生成。
5. **不要要求或持久化私有思维链。** 只保存结构化“决策结果”和“故事状态”。
6. **所有新增状态必须可版本化、可恢复、可兼容旧书。**
7. **每个 Phase 完成后先跑测试再进入下一 Phase。** 不做一次性大重构。
8. **如果本文与当前代码事实冲突，以代码为准，但必须在最终执行报告中列出差异及处理方式。**

### 0.1 最终应交付

Local Agent 完成后至少交付：

- 工具能力元数据与 profile 过滤实现；
- 机器可读的工具 profile 检查输出；
- book-scoped planning state；
- build-session planning draft（建书前无 `book_id` 时可用）；
- story boundary detector（内部纯函数）；
- `novel-replan` skill；
- `get_story_state` 聚合读能力，或等价地扩展现有上下文工具；
- `save_outlines` 的原子 append / revision / planning patch 能力，或经过论证的等价实现；
- 单元测试 + 集成测试；
- 更新后的契约文档/skill 镜像校验；
- 最终变更说明与迁移说明。

---

# 1. 当前运行暴露出的事实问题

本计划以已提供的一次真实建书运行作为行为基线。

该次运行中：

- 加载 `novel-build` skill 后，先读取 `get_build_status`；
- 在 `book_id=""`、书尚未创建的情况下仍调用了 `arc_material_candidates`，直接失败；
- 随后进行了 **7 次 `query_arc_library` + 6 次 `query_plots`** 的相近关键词搜索；
- 又额外查询了一次已经明确指定的笔名；
- `validate_world` 第一次因输入 shape 不符合真实嵌套结构而判定全部人物势力为 orphan，第二次修正后才通过；
- Agent 在“是否能/应该调用 submit”上多次重新解释 skill、工具说明和护栏；
- 最终故事线结构质量是可用的：一次性生成约 **112k 字、5 个顶层弧、15 个叶弧、62 个 plot**，结构校验通过；
- 但为了得到这个结果，Agent 把大量注意力花在“工具能不能用、参数应该怎么放、权限到底怎么解释”上，而不是剧情推演本身。

结论：

> 当前首要瓶颈不是“模型不会讲故事”，而是**系统没有在模型推理开始前，把可用能力、当前事实、权限边界和规划范围整理好**。

---

# 2. 本次优化的核心架构决策

必须落实以下 8 个决策：

1. **底层 MCP 能力保留，Agent 可见工具按任务 profile 动态裁剪。**
2. **工具可用性由规则路由器决定，不允许 Agent 靠调用失败来学习。**
3. **故事状态分成：已发生事实 / 已承诺计划 / 远期预测。**
4. **建书只承诺前部故事区，不强制一次性规划完整 90k～180k。**
5. **写作临近规划边界时自动 Replan，只再承诺下一小段。**
6. **人物未来变化属于规划预测，只有写完后的 character_events 才成为事实。**
7. **线程、promise、reader question 进入 planning state，成为真正的故事记忆。**
8. **重大 storyline 写入增加 revision / expected_revision，避免 Web/MCP/Agent 陈旧状态覆盖。**

目标运行循环：

```text
User Intent
   ↓
Task / Skill Router           ← 纯规则
   ↓
Capability Profile            ← 只暴露当前需要的 4～10 个工具
   ↓
Story State                   ← 事实 + 当前承诺 + 远期预测
   ↓
Creator Decision Loop         ← LLM 做剧情判断
   ↓
Commit / Write
   ↓
Character / Thread / Promise / Chapter 状态更新
   ↓
Review
   ↓
Boundary Detector             ← 纯规则
   ↓
需要时 novel-replan
   ↓
继续
```

---

# 3. MCP：哪些是“可用能力”，哪些“不应默认暴露”

## 3.1 先纠正一个概念：Core Capability ≠ Always Visible

不要再设“所有任务都常驻的 MCP 工具”。

`navigate`、`drive_ui`、`get_book_detail`、`save_outlines` 等即使是平台核心能力，也不代表普通写作轮次应该看到它们。

**目标不是减少平台能做什么，而是减少模型在当前任务里需要理解什么。**

## 3.2 不手工维护“44 个工具总表”

当前文档/运行信息表明工具面约为 44 个，但 Local Agent 必须以代码中的：

```text
agent_tools.TOOL_REGISTRY
EXPECT_MCP_TOOLS
PHASE_GATES
_LOCKED_TOOLS / 对应锁元数据
```

为真值。

第一步生成一次实际清单，并记录每个工具：

```json
{
  "name": "arc_material_candidates",
  "profiles": ["replan", "inspect"],
  "read_write": "read",
  "requires_book": true,
  "requires_storyline": true,
  "allowed_phases": ["ready", "plots"],
  "confirmation": "none",
  "lock": false
}
```

### 重要

`storage/tool_profiles.json` 可以作为**生成的检查产物**，但不应成为新的手工真源。

推荐把 profile / prerequisite / side-effect 元数据放在 `TOOL_REGISTRY` 的 ToolSpec、decorator 或其邻近 schema 中，由运行时生成 profile manifest。

---

# 4. 推荐 MCP Profile

以下是“已知工具”的建议分类。完整工具集由代码扫描补齐。

## 4.1 `build-candidates`

任务：步 1～2，设定候选。

默认可见：

- `get_build_status`
- `navigate`（仅确有页面切换需要时）
- `drive_ui`
- `query_profiles` **仅当 pen 未确定**

默认隐藏：

- 所有正文保存工具
- `save_outlines`
- 发布工具
- scout 工具
- 已有 pen 时的 `query_profiles`

目标可见工具数：**3～4**。

## 4.2 `build`

任务：步 3，世界、势力、前部故事线、人物、校验。

默认可见：

- `get_build_status`
- `drive_ui`
- `query_arc_library`
- `query_plots`
- `validate_storyline`
- `validate_world`

条件可见：

- `query_profiles`：仅 pen 缺失；
- `query_gags`：只有题材/skill 明确需要机制型笑点；
- `query_characters`：只有需要角色原型参考；
- 未来可用 `query_story_materials` 聚合替代多次 query。

**明确禁止：**

- `arc_material_candidates`：`book_id` 为空或无 `storyline.json` 时不得暴露；
- `save_plot_draft`
- `save_chapter_text`
- publish 系列
- scout 系列

目标可见工具数：**6～8**。

## 4.3 `write`

任务：已经有正式 storyline，写当前 plot / chapter。

建议默认只暴露：

- `get_writing_context`
- `save_plot_draft`
- `save_chapter_text`
- `chapter_quality_gate`

如果 `get_writing_context` 已经返回 `style_card`、当前 cast、next_plot，则默认隐藏：

- `get_pen_style`
- `pick_plot_sample`
- `get_book_detail`

只有上下文明确缺失时再条件开放。

**普通写作绝对不要默认看到：**

- `query_arc_library`
- `query_plots`
- `query_gags`
- `query_characters`
- `save_outlines`
- publish/scout 工具

目标可见工具数：**4～6**。

## 4.4 `replan`

任务：临近 committed boundary 或剧情实际结果推翻未来预测时，延伸下一段故事。

默认可见：

- `get_story_state`（新增，见后文）
- `save_outlines`（增强为原子 append + planning patch + revision）
- `validate_storyline`

条件可见：

- `validate_world`：只有势力/人物/世界事实发生结构性变化；
- `query_story_materials`：只有 Agent 判定当前需要外部机制参考；
- 或兼容期内暂时暴露 `query_arc_library` + `query_plots`。

目标可见工具数：**3～6**。

## 4.5 `publish`

只暴露：

- `get_book_detail`
- `save_book_meta`
- `publish_check`
- `publish_book`
- `mark_finished`
- `export_book`

创作和素材库工具全部隐藏。

## 4.6 `scout`

只给侦察/提取：

- `list_rankings`
- `discover_hot`
- `fetch_novel`
- `list_crawled_novels`
- `read_crawled_novel`
- `extract_state`
- `judge_extraction`
- `ingest_library_assets`
- 需要时的 `drive_ui(set_review)`

普通创作 profile 不得看到这些能力。

## 4.7 `inspect`

用于调试、只读检查：

- `list_books`
- `get_book_detail`
- `get_build_status`
- `validate_storyline`
- `validate_world`
- 必要的状态聚合读工具

默认不允许写。

---

# 5. Tool Router：必须是规则，不是 LLM

新增或重构为一个纯规则层，例如：

```text
agent_tool_router.py
```

输入：

```text
task_intent
skill
wizard_step
book_exists
book_phase
storyline_exists
pen_selected
user_confirmation_state
feature_flags
```

输出：

```json
{
  "profile": "build",
  "allowed_tools": ["..."],
  "conditional_tools": ["..."],
  "forbidden_tools": ["..."],
  "allowed_ui_commands": ["set_world", "set_outline", "set_characters"],
  "user_only_actions": ["submit"],
  "reason_codes": ["NO_BOOK_ID", "PEN_ALREADY_SELECTED"]
}
```

## 5.1 必须双层防护

1. **在 `list_tools` / MCP 暴露阶段先隐藏不适用工具。**
2. **工具执行端仍检查 prerequisite。**

不能只做前端隐藏，因为旧客户端或错误 profile 仍可能调用。

## 5.2 `drive_ui` 的子命令必须单独做权限元数据

当前 `drive_ui` 是多命令工具，因此只按工具名过滤不够。

需要有类似：

```python
UI_COMMAND_POLICY = {
    "set_field": {"profiles": {"build-candidates"}},
    "set_world": {"profiles": {"build"}},
    "set_outline": {"profiles": {"build"}},
    "set_characters": {"profiles": {"build"}},
    "set_review": {"profiles": {"scout"}},
    "submit": {"actor": "user_only"},
}
```

### 必须解决 submit 口径冲突

当前 skill/护栏表达的是“正常建书由用户动作触发、agent 不调 submit”，但某些工具说明又容易让模型理解为“确认后 agent 可调用 submit”。

最终必须只有一个机器可读真值：

```text
submit.actor = user_only
```

并由它自动生成：

- Tool description；
- `NOVEL_AGENT.md` 契约片段；
- skill 说明；
- runtime capability 返回。

Agent 不应再花 token 解释这件事。

---

# 6. Tool Contract：从文字说明升级为机器可读元数据

当前运行第一次 `validate_world` 把 `factions` 放到了不正确层级，validator 将其解释成“0 个势力”，导致 7 个 orphan，再重试一次。

优化：

1. 请求 schema 严格化；
2. 对常见错误 shape 给明确错误，而不是继续做语义校验；
3. docstring / skill / workspace contract 从 schema 生成。

例如：

```json
{
  "error": "invalid_input_shape",
  "field": "basic_info.world_building.factions",
  "received": "basic_info.factions",
  "action": "move_factions_under_world_building"
}
```

不要返回一个看起来像“世界观真的有 7 个逻辑问题”的报告。

---

# 7. 库查询：一次侦察，不要 13 次试关键词

## 7.1 当前问题

建书运行里出现了 7 次弧库 + 6 次 plot 库搜索。

这种搜索模式会把模型变成“关键词调参器”。

## 7.2 P1 推荐：`query_story_materials`

这是**可选的新 MCP**，如果现有查询层很容易内部复用，则建议实现。

输入：

```json
{
  "intent": "build_opening",
  "tags": ["穿越", "系统", "星际", "基建", "军事"],
  "keywords": ["轨道电梯", "工程自证", "危机升级"],
  "need": ["arc", "plot"],
  "top_k_each": 5
}
```

输出不要 dump 全库，而是：

```json
{
  "arc_refs": [
    {
      "id": "...",
      "name": "...",
      "mechanism": "先遭污名→通过专业证据自证→反向获得资源",
      "useful_for": ["开篇逆袭", "身份翻转"],
      "avoid_copying": ["宗门背景", "原角色关系"],
      "relevance": 0.86
    }
  ],
  "plot_refs": [],
  "coverage": {
    "enough": true,
    "missing": []
  }
}
```

## 7.3 查询预算

在 skill 中加规则：

```text
首次素材侦察：1 次聚合查询
若 coverage.enough=false：允许第 2 次补搜
默认不再继续换近义词试探
```

如果 Agent 本身已经能设计出合理结构，素材库是“对镜”，不是强制依赖。

---

# 8. 故事线数据模型：Facts / Committed / Forecast

这是本次架构的核心。

## 8.1 Facts：已发生事实

包括：

- 已写章节；
- 已完成 plot；
- 已发生 character_events；
- 已确认世界事实；
- 已形成关系变化；
- 已设下/已兑现 promise 的事实状态。

**Facts 不允许因为未来规划改变而被回写。**

## 8.2 Committed Storyline：已承诺故事区

正式进入现有：

- `outlines`
- `plots`
- `threads`
- `promises`

并必须通过现有硬结构校验。

它代表：

> “如果没有新的重大事实推翻计划，接下来这段就是准备真正写的。”

默认只覆盖：

- 当前可执行 H0；
- 以及足够形成阶段闭环的近期 H1。

## 8.3 Forecast：远期预测

不直接写成正式 plot。

只保存：

- 下一阶段可能的目标；
- 世界升级方向；
- 角色成长方向；
- 可能回收的线索；
- 尚未决定的关键选择；
- 未来 antagonistic pressure。

Forecast 可以被后续实际剧情推翻。

### 核心规则

```text
越近 → 越具体、越承诺
越远 → 越抽象、越可变
```

---

# 9. 目标总字数必须与 committed horizon 分离

这是增量故事线能否正确实现的关键兼容点。

当前系统可能通过 `max(outline.end_word)` / planned words 推断故事规模。Local Agent 修改前必须全仓搜索所有依赖：

```text
total_words
planned_words
max(end_word)
end_word
progress
finished
publish
chapter count
storyline coverage
```

## 9.1 新概念

```text
target_word_budget      预计全书体量，例如 120000
committed_until_word    当前正式承诺到，例如 21000
written_until_word      已经真正写到，例如 9000
```

UI 应表达：

```text
预计全书：120k
已规划承诺：21k
已写：9k
```

而不是因为 storyline 当前只有 21k 就把书误认为总共 21k。

## 9.2 Validator

正式 storyline 只需要在：

```text
[0, committed_until_word)
```

内部连续覆盖。

不要要求 forecast 区拥有正式顶层弧。

如果现有 `validate_storyline` 本身只根据现有 arcs 推断 total_words，则可保持其结构校验逻辑；但所有“全书目标体量”消费者必须迁移到 `target_word_budget`。

---

# 10. 建书前无 book_id：必须有 Build Session Planning State

原草案若只设计：

```text
storage/.../<book_id>/planning_state.json
```

会漏掉一个关键阶段：**步 3 时书尚未创建，`book_id` 为空，但此时已经需要 planning state。**

## 10.1 要求

给新书向导一个稳定：

```text
build_session_id
```

如果现有 wizard 已有 session/state id，复用；不要重复造概念。

建书阶段保存：

```text
build_session / planning_draft
```

提交成功后：

```text
planning_draft
   ↓ migrate / attach
book-scoped planning_state
```

提交失败则保留 session draft，不丢规划。

---

# 11. Book-scoped `planning_state`

planning state 属于“书”，不是一次 Agent 运行的临时日志。

因此存储位置必须是**per-book durable state**。Local Agent 应按当前项目存储习惯选择位置；不要机械放到 `agent_runs`，除非 `agent_runs` 本身就是持久 book state。

建议 schema：

```json
{
  "schema_version": 1,
  "book_id": "book_x",
  "storyline_revision": 12,
  "mode": "open",
  "target_word_budget": 120000,
  "written_until_word": 9000,
  "committed_until_word": 21000,
  "current": {
    "arc_id": "A",
    "plot_id": "p07"
  },
  "horizon": {
    "h0_executable_plots": 3,
    "h1_near_arcs": 2,
    "h2_intents": 4
  },
  "tension": {
    "main_goal": "...",
    "main_obstacle": "...",
    "stakes": "..."
  },
  "story_questions": [
    {
      "id": "q_01",
      "question": "37号舱事故真的是焊接失误吗？",
      "priority": 0.9,
      "status": "open",
      "last_touched_plot": "p05"
    }
  ],
  "active_threads": [],
  "critical_promises": [],
  "character_intents": [],
  "future_intents": [],
  "decision_points": [],
  "last_replan": {
    "reason": "boundary_low",
    "at_plot": "p06",
    "from_revision": 11,
    "result_revision": 12
  }
}
```

### planning_state 不得存

- 大段正文；
- 完整角色卡副本；
- 全量 world 副本；
- 长篇模型思维过程；
- 可以从正式数据实时计算出的巨大冗余结构。

它是**导航索引 + 规划记忆**，不是第二份数据库。

---

# 12. “模拟人类作者思考”应实现为 Creator Decision Loop

不要模拟碎片化的脑内独白。

模拟一个成熟作者会做的决策循环：

```text
1. OBSERVE
   现在真实发生了什么？哪些事实已经不可改？

2. FRAME
   当前最值得解决的叙事问题是什么？

3. PRESSURE
   谁想要什么？谁/什么在阻止？为什么现在必须行动？

4. BRANCH
   生成 2～3 个明显不同的合理下一步。

5. SIMULATE
   每个方向向前预测 1～3 个因果后果：
   - 人物会被迫做什么选择？
   - 代价是什么？
   - 哪条线程推进？
   - 哪个 promise 被兑现/延迟/扭转？
   - 会制造什么新的读者问题？

6. SELECT
   选择当前最有因果力、人物力、阅读拉力的方向。

7. EXECUTION BRIEF
   把选择压成 1～3 个可写 plot。

8. WRITE / COMMIT

9. OBSERVE RESULT
   实际写出来后发生了哪些状态变化？

10. UPDATE
   更新人物事实、线程、promise、reader question。

11. REPLAN CHECK
   未来预测是否仍成立？是否接近 committed boundary？
```

## 12.1 持久化的是“决策卡”，不是思维链

示例：

```json
{
  "current_problem": "主角刚自证焊点无责，但只能证明材料有问题，尚不能证明是谁换料",
  "reader_question": "谁在系统性替换低标钢？",
  "candidate_directions": [
    {
      "id": "d1",
      "direction": "从供应链批次追查",
      "benefit": "因果直接、工程味强",
      "risk": "调查节奏可能偏平",
      "character_effect": "逼主角第一次主动拉队友入局"
    },
    {
      "id": "d2",
      "direction": "先发生一次新的承重危机",
      "benefit": "危机把调查变成必须立即解决的问题",
      "risk": "需要避免重复事故套路",
      "character_effect": "主角从自保转为承担公共责任"
    }
  ],
  "selected": "d2+d1",
  "selection_reason": "先用危机制造时间压力，再让供应链追查成为解决危机的唯一办法"
}
```

这类数据可以存。不要存“我先想到……然后我又想到……”的长推理。

---

# 13. Plot 生成：目标—阻力—选择—代价—结果

每个可执行 plot 在真正写正文前，形成一个**非永久或轻量保存的 execution brief**：

```json
{
  "dramatic_goal": "顾衡要证明七号工区新到钢材存在批次问题",
  "obstacle": "仓管拒绝停工检测，停工会触发巨额违约",
  "stakes": "继续施工可能造成主索节点不可逆损伤",
  "choice": "绕过行政流程直接停掉自己的施工班组",
  "cost": "背上抗命责任，并与管理层公开冲突",
  "irreversible_change": "顾衡从被动自证者变成主动承担工程责任的人",
  "thread_advance": ["main_corruption"],
  "promise_action": "advance:p_low_grade_steel",
  "reader_question": "这批钢为什么能通过总局复验？",
  "ending_hook": "复验报告上的签名来自本不该参与采购的人"
}
```

## 13.1 Plot 必须有变化

不要机械规定“每个 plot 一定爆炸/反转”，但连续 plot 不能只增加解释。

每个 plot 至少改变一个：

```text
关系 / 风险 / 资源 / 信息可信度 / 目标 / 阵营态度 / 空间位置 / power state / 承诺状态
```

Review 可检查最近 N 个 plot 的 `irreversible_change` 是否过度为空或同质。

---

# 14. 初始建书：只构建前部 Committed Storyline

## 14.1 默认 horizon

不要直接生成完整 112k / 62 plot。

第一版建议：

```text
initial_committed_words = clamp(words_per_chapter * 6, 15000, 30000)
```

同时满足：

- 至少形成一个完整开篇阶段；
- 至少完成一次“问题出现 → 升级 → 阶段结果”；
- 至少 1 个完整 top arc，或 2 个存在强因果关系的小弧；
- 大约 6～12 个可执行 plot；
- H1 再保存 1～2 个近期弧方向；
- H2 保存 2～5 个远期 intent，不生成正式 plot。

题材特殊时 Agent 可调整，但必须说明结构原因，不因“全书目标 120k”就一次规划 120k。

## 14.2 建书 Step 3 新流程

```text
[1] get_build_status
    ↓
[2] Router 确定 profile=build
    - book_id 为空 → 不暴露 arc_material_candidates
    - pen 已存在 → 不暴露 query_profiles
    ↓
[3] 一次素材侦察
    ↓
[4] 生成 2～3 个真正不同的开篇/主冲突方向
    ↓
[5] 比较并选定/融合
    ↓
[6] world + core_conflict + factions
    ↓
[7] 生成 15k～30k committed storyline
    ↓
[8] 生成 H1/H2 future intents
    ↓
[9] characters
    ↓
[10] validate_storyline(committed)
     + validate_world
    ↓
[11] soft review
    - causality
    - character pressure
    - reader pull
    - thread/promise balance
    ↓
[12] 只修真正存在的问题
    ↓
[13] 保存 build-session planning draft
    ↓
[14] 向用户汇报，停止
    ↓
[15] 用户自行 submit
    ↓
[16] submit 成功后把 planning draft 迁到 book state
```

---

# 15. 正常写作：不要每次重新规划全书

默认写作循环：

```text
get_writing_context
    ↓
读取 next_plot + active cast + style + 必要状态
    ↓
生成 execution brief
    ↓
写当前 plot
    ↓
save_plot_draft(character_events=...)
    ↓
章满 → save_chapter_text
    ↓
chapter_quality_gate
    ↓
状态机 / promise ledger 更新
    ↓
Boundary Detector
    ↓
足够远 → 继续写
接近边界 / 预测失效 → 切到 novel-replan
```

### 普通写作期间不要做

- 不重新查弧库；
- 不重新查人物库；
- 不重建世界观；
- 不重新把所有 future intents 固化；
- 不因为一个 plot 写完就重规划全书。

---

# 16. Boundary Detector：内部纯函数，不做 MCP

Boundary detector 是确定性状态判断，不需要模型工具。

建议条件：

```text
remaining_committed_plots <= 2
OR
remaining_committed_words <= max(2 * words_per_chapter, initial_horizon_words * 0.25)
OR
current_arc_near_end AND next_arc_uncommitted
OR
major_decision_point == true
OR
actual_plot_result_invalidates_forecast == true
OR
critical_promise_due_without_committed_resolution == true
```

## 16.1 防抖 / 幂等

必须避免每写一个 plot 都 replan：

```text
last_replan_revision
last_replan_at_plot
minimum_extension_words
replan_cooldown
```

如果 committed horizon 已经被另一轮 Agent 扩过，则当前轮读取新 revision 后直接继续，不重复扩。

输出：

```json
{
  "should_replan": true,
  "reasons": ["LOW_REMAINING_PLOTS", "ARC_NEAR_END"],
  "remaining_plots": 2,
  "remaining_words": 4800,
  "current_revision": 12
}
```

---

# 17. `novel-replan`：真正的长篇故事大脑

新增 skill：

```text
novel-replan
```

职责只有：

> 基于最新事实，重新设计下一小段故事，并更新未来预测。

不直接负责长篇正文。

## 17.1 输入

通过 `get_story_state` 聚合：

- 当前 arc / plot；
- 最近章节 summary；
- 已写事实；
- active cast 的动态状态；
- active threads；
- open / due promises；
- reader questions；
- committed 剩余区域；
- H1/H2 future intents；
- target budget；
- storyline revision；
- boundary reason。

## 17.2 决策步骤

```text
A. 当前事实诊断
B. 找最强未解决张力
C. 生成 2～3 个候选方向
D. 模拟每个方向 1～3 步后果
E. 选一个/融合
F. 生成下一 1 个 arc 或 4～10 个 plot
G. 更新 future intents
H. soft review
I. 原子 commit
```

## 17.3 输出结构

```json
{
  "diagnosis": {
    "current_state": "...",
    "main_tension": "...",
    "reader_question": "...",
    "forecast_invalidations": []
  },
  "candidate_directions": [],
  "selected_direction": "...",
  "storyline_patch": {
    "outlines": [],
    "plots": [],
    "threads": []
  },
  "planning_patch": {
    "future_intents": [],
    "story_questions": [],
    "character_intents": []
  }
}
```

---

# 18. `get_story_state`：推荐唯一新增的 P0 聚合 MCP

如果不想继续扩大工具面，P0 只新增这一个 MCP 即可。

返回的是**规划所需的压缩事实**，不是数据库 dump：

```json
{
  "book_id": "...",
  "phase": "ready",
  "storyline_revision": 12,
  "position": {
    "current_arc": {},
    "current_plot": {},
    "written_until_word": 9000,
    "committed_until_word": 21000,
    "target_word_budget": 120000
  },
  "boundary": {
    "should_replan": false,
    "reasons": []
  },
  "recent": {
    "chapter_summaries": []
  },
  "characters": {
    "active": []
  },
  "threads": [],
  "promises": [],
  "questions": [],
  "forecast": {},
  "capabilities": {
    "profile": "replan",
    "allowed_actions": ["append_storyline"],
    "user_only_actions": []
  }
}
```

### 不要让它返回

- 全书全部章节正文；
- 所有角色完整资料；
- 全量素材库结果；
- 全部工具说明。

---

# 19. 不建议新增 `extend_storyline`：优先增强现有 `save_outlines`

原草案提出新增 `extend_storyline`，但从“减少 MCP 数量”的目标看，它与 `save_outlines(mode=append)` 功能高度重叠。

优先方案：增强现有 `save_outlines`：

```json
{
  "book_id": "...",
  "mode": "append",
  "expected_revision": 12,
  "outlines": [],
  "plots": [],
  "threads": [],
  "planning_patch": {},
  "validate": true
}
```

服务端一次完成：

1. revision check；
2. 合并；
3. id / parent / leaf 校验；
4. storyline validation；
5. 保存 storyline；
6. 保存 planning state；
7. revision +1；
8. 返回新的 story state 摘要。

### 原子性

如果任何一步失败：

```text
storyline 和 planning_state 都不得部分更新。
```

如果现有 `save_outlines` 难以安全扩展，才新增一个更清晰的 `commit_storyline_patch`，并在 `replan` profile 中隐藏底层 `save_outlines`。

不要同时让两个几乎相同的写工具都暴露给 Agent。

---

# 20. Thread / Promise / Reader Question：真正的故事记忆

## 20.1 Thread

Replan 前生成紧凑张力面板：

```text
MAIN_THREAD
  goal
  obstacle
  last_advance
  urgency

SIDE_THREAD_X
  status
  last_touched_plot
  silent_for_plots
  next_window
```

## 20.2 Promise

正式 promise 只引用**已经存在的正式 plot**。

不要在 forecast 阶段创建指向未来不存在 plot id 的 `resolves_plot_id`。

Forecast 中可以保存：

```json
{
  "promise_id": "p_x",
  "planned_payoff_intent": "在主索审查阶段揭露复验报告签名人",
  "due_horizon": "H1"
}
```

真正 commit 对应 payoff plot 时再绑定正式 `resolves_plot_id`。

## 20.3 Reader Question

每个关键剧情结果可以：

- 回答一个问题；
- 升级一个问题；
- 新建一个问题。

Replan 优先查看高 priority / 长时间未触碰的问题。

---

# 21. Character：写前预测，写后事实

当前 character state 机制继续保留。

### 写前

只在 planning state / execution brief 保存：

```json
{
  "name": "顾衡",
  "current_goal": "保住工位并查清材料问题",
  "pressure": "停工会被再次追责",
  "expected_change": "从被动自证转为主动担责"
}
```

这是预测，不应直接改 `character_states`。

### 写后

只有实际发生后才通过：

```text
character_events
```

落成事实。

因此形成：

```text
写前：intent / expected impact
写后：event / fact
```

这可以显著减少人物成长被提前“写死”。

---

# 22. 上下文压缩：不要靠聊天历史记故事

`get_writing_context` 已经是正确方向，应继续分层：

```json
{
  "critical": {},
  "current": {},
  "recent": [],
  "forecast": {},
  "background": {}
}
```

优先级：

```text
critical > current > recent > forecast > background
```

规则：

- active cast：完整动态卡；
- referenced cast：紧凑卡；
- inactive cast：默认不注入；
- threads：只给 active / due；
- promises：优先 due/high-risk；
- 历史正文：最近片段 + summary；
- 风格：一张 style card，不重复塞整套规则；
- 远期规划：只给 intent，不给大量未写 plot。

---

# 23. Review：硬校验与软判断分开

## 23.1 平台 Hard Validator

继续负责：

- span / coverage；
- leaf arc；
- plot outline_id；
- id 唯一；
- factions/characters 一致性；
- schema；
- revision；
- phase / lock。

## 23.2 Agent Soft Review

`novel-review` 或 replan/build 内部 review 负责：

```text
causality
character_choice
reader_pull
thread_progress
promise_pressure
repetition_risk
pace
```

输出：

```json
{
  "revision_needed": true,
  "problems": [
    {
      "type": "flat_progression",
      "location": ["p08", "p09", "p10"],
      "reason": "连续三个 plot 仅增加信息，没有人物选择或风险改变",
      "fix": "把 p09 改成需要付出责任代价的停工选择"
    }
  ]
}
```

不要把主观 0.87/0.91 分数作为底层强制门槛；分数最多作为观测指标。

---

# 24. Storyline Revision / 并发控制

`BookLock` 继续保留，但锁只能保证同一时刻不同时写，不能保证 Agent 使用的状态不是旧的。

重大 storyline write 增加：

```text
storyline_revision
expected_revision
```

流程：

```text
get_story_state → revision=12
Agent 规划
save_outlines(expected_revision=12)
```

如果期间 Web 修改成 13：

```json
{
  "error": "stale_storyline",
  "expected": 12,
  "actual": 13,
  "action": "refresh_and_replan"
}
```

禁止静默覆盖。

---

# 25. Skill 重构

建议最终技能职责：

```text
novel-build-candidates  候选世界
novel-build             前部 committed plan + build-session forecast
novel-write             只写当前 plot/chapter
novel-replan            只延伸故事线
novel-review            软质量检查（可内嵌，后续再独立）
novel-publish           发布
novel-scout             外部提取
```

## 25.1 `novel-story`

如果当前大量逻辑集中在 `novel-story`，第一版不要求立刻删除。

可以先让它变成 Router：

```text
ready + boundary=false → novel-write
ready + boundary=true  → novel-replan → novel-write
config/plots            → legacy recovery
```

稳定后再决定是否废弃旧 skill 名。

## 25.2 Skill 缺失必须 fail closed

禁止：

```text
skill 不存在 → 静默使用全部 MCP 自己试
```

必须：

```text
skill 不存在 / digest 不一致
→ 明确错误
→ 停止该任务的写操作
```

---

# 26. Prompt / Contract 单一真源

当前最危险的不是 prompt 长，而是同一规则可能存在多份不同版本。

建立机器可读真源，例如按当前项目结构落在：

```text
schemas/tool_contracts.*
schemas/story_contract.*
schemas/agent_policy.*
```

从真源生成：

- MCP tool description；
- Tool input schema；
- `NOVEL_AGENT.md` 的契约部分；
- skill reference；
- profile manifest；
- smoke test expected metadata。

### Persona

只写身份和总原则。

### Workspace Contract

只写稳定事实契约。

### Skill

只写当前阶段：目标、流程、stop condition、profile。

### Task

只写用户意图 + 当前状态摘要。

不要在四层重复复制同一 submit/字段规则。

---

# 27. 推荐实施阶段

## Phase 0：Repository Audit（只读，不改行为）

- [ ] 扫描 `TOOL_REGISTRY`
- [ ] 扫描 `EXPECT_MCP_TOOLS`
- [ ] 扫描 `PHASE_GATES`
- [ ] 扫描 lock / loop guard
- [ ] 扫描 `drive_ui` 所有 cmd
- [ ] 扫描所有 skill source 与 `.dsh/skills` 镜像关系
- [ ] 定位 `storyline.json`、character state、promise ledger、chapter、draft
- [ ] 搜索所有对 `max(end_word)` / `total_words` 的消费
- [ ] 确认正常建书 submit 的真实 actor 规则
- [ ] 输出 `docs/agent_architecture_audit.md` 或等价报告

**不通过条件：** 没看清这些事实前禁止直接重构。

---

## Phase 1：Tool Capability Metadata + Profile Filtering

- [ ] 为现有 ToolSpec 增加 profile/prerequisite/read-write/confirmation 元数据
- [ ] `list_tools` 按 active profile 过滤
- [ ] 工具执行端重复检查 prerequisite
- [ ] `drive_ui` cmd 增加子命令 policy
- [ ] submit 明确 `user_only`
- [ ] skill router 选择 profile
- [ ] 记录每次 session 的 active profile 到日志
- [ ] 生成 profile manifest 供 smoke test

### 验收

- [ ] step 3、`book_id=""` 时模型根本看不到 `arc_material_candidates`
- [ ] pen 已指定时模型默认看不到 `query_profiles`
- [ ] build profile 可见工具 ≤ 8（兼容期最多 10）
- [ ] write profile 可见工具 ≤ 6
- [ ] 无效 prerequisite 工具调用数 = 0
- [ ] submit 权限不再需要模型解释

---

## Phase 2：状态聚合与契约清理

- [ ] 增强 `get_build_status`：返回 selected candidate / pen / current step / capability summary / build_session_id
- [ ] 常见错误 input shape 改为 schema error
- [ ] 实现 `get_story_state` 或等价聚合接口
- [ ] Skill/docstring 从单一 contract 生成关键片段
- [ ] skill mirror 加 source digest
- [ ] missing/outdated skill fail closed

### 验收

- [ ] Agent 不再因为 `has_world=true` 猜“究竟填了什么”
- [ ] `validate_world` 错 nesting 一次就给 shape error
- [ ] Agent 一次 read 能知道 current/remaining/boundary/revision

---

## Phase 3：Planning State（先不改变旧书行为）

- [ ] 增加 build-session planning draft
- [ ] 增加 book-scoped planning_state
- [ ] schema_version
- [ ] target_word_budget / committed_until_word / written_until_word
- [ ] story questions
- [ ] future intents
- [ ] character intents
- [ ] last replan
- [ ] 为已有书实现 lazy bootstrap

### 旧书 bootstrap

```text
committed_until_word = 当前 storyline 最大 end_word
target_word_budget = 旧系统已有目标；若没有则暂等于 committed_until_word
future_intents = []
mode = legacy_full | open（按检测结果）
```

**不得自动裁掉旧书已经存在的完整 outlines/plots。**

---

## Phase 4：Incremental Build（feature flag）

新增 feature flag，例如：

```text
INCREMENTAL_STORY_PLANNING=1
```

只对新书启用。

- [ ] build step 3 默认 committed 15k～30k
- [ ] H1/H2 进 planning draft
- [ ] target_word_budget 与 committed 分离
- [ ] UI 能显示“预计总量 / 已承诺 / 已写”
- [ ] submit 后 planning draft 迁移到 book

### 验收

同类“2050 星际基建”建书：

- [ ] 不再默认生成 112k / 62 plot
- [ ] committed 约 6～12 plot
- [ ] 至少一个开篇阶段闭环
- [ ] `validate_storyline` 对 committed region 通过
- [ ] world 校验通过
- [ ] H1/H2 至少保留一个可变远期方向

---

## Phase 5：Boundary + Replan

- [ ] 实现内部 boundary detector
- [ ] 实现防抖/幂等
- [ ] 新增 `novel-replan`
- [ ] replan 读取 `get_story_state`
- [ ] 每次只扩 1 个新弧或 4～10 plot
- [ ] future intents 可修改/删除
- [ ] 正式 facts 不回写

### 验收

- [ ] committed 剩余 ≤2 plot 时，在写尽前自动扩一次
- [ ] 同一 revision 不重复扩
- [ ] 实际剧情推翻 forecast 时可改 forecast
- [ ] 不需要用户手工说“再规划后面五万字”

---

## Phase 6：Atomic Story Commit + Revision

- [ ] `save_outlines` 支持 expected_revision
- [ ] storyline + planning patch 原子写
- [ ] stale revision 明确失败
- [ ] 失败不产生半写状态

### 验收

- [ ] Web 修改后旧 Agent 写入被拒绝
- [ ] refresh 后可重新 replan
- [ ] BookLock 仍有效

---

## Phase 7：Story Quality Loop

- [ ] execution brief
- [ ] irreversible/change dimension
- [ ] reader question
- [ ] thread silence / promise due
- [ ] soft review

### 验收

抽查连续 10 个 plot：

- [ ] 不大量出现纯解释平推
- [ ] 角色选择与状态变化可追踪
- [ ] 至少主要线程持续推进
- [ ] 高优先级 promise 不被长期遗忘

---

# 28. 必须编写的测试

## 28.1 Tool Router 单元测试

### Case A：建书 step 3，无 book_id

期待：

```text
arc_material_candidates not exposed
save_chapter_text not exposed
publish_* not exposed
```

### Case B：pen 已选

```text
query_profiles not exposed
```

### Case C：write

```text
query_arc_library not exposed
query_plots not exposed
save_outlines not exposed
```

### Case D：publish

创作工具不可见。

---

## 28.2 UI Command Policy

- [ ] build profile 可以 set_world/set_outline/set_characters
- [ ] agent 不能执行 submit
- [ ] scout 只能 set_review 等允许命令

---

## 28.3 Build Session

- [ ] 无 book_id 可保存 planning draft
- [ ] submit 成功迁移
- [ ] submit 失败 draft 保留

---

## 28.4 Incremental Storyline

创建测试书：

```text
words_per_chapter=3000
target_word_budget=120000
```

期待：

```text
initial committed 15000~30000
formal plot 6~12 左右
target 仍是 120000
```

不得把书识别为“只有 20k”。

---

## 28.5 Replan Boundary

模拟：

```text
remaining_plots=2
remaining_words=4500
```

期待：`should_replan=true`。

扩完后：`should_replan=false`。

同一 revision 再运行不重复 append。

---

## 28.6 Forecast Invalidation

Forecast：

```text
某角色下一弧加入主角阵营
```

实际 plot 写成：

```text
该角色公开背叛
```

期待：

- facts 保留背叛；
- future intent 被标记 invalidated；
- replan 生成新方向；
- 不改写已经发生的 plot。

---

## 28.7 Revision Conflict

Agent A 读 revision=10。

Web 更新到 11。

Agent A 提交 expected_revision=10。

期待：

```text
stale_storyline
```

且无部分写入。

---

# 29. 观测指标

上线后日志记录：

```text
profile_name
visible_tool_count
tool_call_count
tool_precondition_error_count
library_query_count
validation_retry_count
replan_count
replan_reason
committed_words_added
forecast_invalidated_count
storyline_revision_conflict_count
```

重点比较当前基线：

- 建书有 13 次素材库相近查询；
- 有 1 次可提前避免的 prerequisite 失败；
- 有 schema shape 导致的 validator 重试；
- 一次性规划 112k / 62 plot。

优化后的目标：

```text
invalid prerequisite calls = 0
build library query calls <= 2
build visible tools <= 8（兼容期 <=10）
normal write visible tools <= 6
initial committed storyline ~= 15k~30k, not full book
```

不要只用“总 token 更少”作为成功标准。最重要的是：**更多推理预算用于故事因果和人物选择，而不是 API 导航。**

---

# 30. 兼容与迁移策略

## 30.1 Feature Flags

建议至少：

```text
AGENT_TOOL_PROFILES
STORY_PLANNING_STATE
INCREMENTAL_STORY_PLANNING
STORYLINE_REVISION_CHECK
```

逐步启用。

## 30.2 旧书

旧书完整 storyline 不裁剪。

第一次进入新版：

- lazy 生成 planning_state；
- 所有现有未写 plot 可继续视为 committed；
- forecast 初始为空；
- 后续可在原计划用尽后切入增量模式。

## 30.3 回滚

planning state 是附加层，不取代 storyline 事实层。

关闭增量 flag 后，旧写作闭环仍应可用：

```text
get_writing_context
→ save_plot_draft
→ save_chapter_text
→ chapter_quality_gate
```

---

# 31. 明确禁止的优化方向

Local Agent 不要做：

1. 把 44 个工具物理删到只剩几个，造成兼容破坏。
2. 把所有 MCP 合并为一个 `do_everything` 超级工具。
3. 把所有创作选择变成 Python if/else。
4. 建书强制一次生成完整 100k+ 全书 plot。
5. Forecast 直接塞进正式 outlines 冒充确定剧情。
6. 把未来人物变化提前写进 character facts。
7. 每写一个 plot 就重规划全书。
8. 让 Agent 通过“调用失败”探索可用工具。
9. skill 丢失后静默降级成裸 MCP。
10. 同一字段规则在 docstring、skill、NOVEL_AGENT 多份手工维护。
11. 保存模型私有长思维链作为 planning state。
12. 只为了 UI 好看先改 Gantt，而核心状态模型仍没分离。
13. 在没有解决 `target_word_budget` 与 committed 分离前直接把正式 storyline 缩到 20k。
14. 新增 `extend_storyline`、`commit_story_plan`、`append_story` 多个重复写工具同时暴露。

---

# 32. 最小可行版本（优先做这 5 件事）

如果时间有限，只实现：

```text
1. Tool Profile + prerequisite metadata
2. Build-session / book planning_state
3. target_word_budget 与 committed_until_word 分离
4. Boundary Detector + novel-replan
5. save_outlines 原子 append + revision
```

完成这 5 项后，系统就已经从：

```text
一次性全书规划 → 顺序写到底
```

变成：

```text
局部承诺 → 写作 → 状态变化 → 提前续规划 → 局部承诺
```

这才是本次改造最核心的价值。

---

# 33. Local Agent 最终执行顺序

严格按以下顺序：

```text
STEP 1  Audit repo，输出真实工具/phase/lock/skill/state 图
STEP 2  给 ToolSpec 加能力元数据，不改底层功能
STEP 3  实现 profile filter + UI cmd policy + prerequisite guard
STEP 4  清理 submit 单一真源与 validator schema 错误提示
STEP 5  增强 get_build_status，补 build_session_id
STEP 6  实现 build-session planning draft
STEP 7  实现 book planning_state + lazy bootstrap
STEP 8  全仓拆分 target_word_budget / committed_until_word
STEP 9  实现 get_story_state
STEP 10 实现 boundary detector（内部函数）
STEP 11 实现 novel-replan skill
STEP 12 增强 save_outlines：append + planning_patch + expected_revision + atomic validate
STEP 13 feature flag 开启新书 incremental build
STEP 14 写集成测试：建书→写→临界→replan→继续写
STEP 15 再考虑 query_story_materials 聚合和 soft review 增强
STEP 16 最后改 UI/Gantt 展示 forecast/committed 分层
```

每一步都提交小 diff；如果仓库使用 Git，建议按 Phase 分 commit。

---

# 34. 最终成功形态

### 建书

Agent 的行为应接近：

> 我先明确当前书最核心的冲突和开篇承诺，比较几种方向，只把足够支撑前几章/前一两个弧的故事正式承诺下来；更远处只记方向，不假装已经确定。

### 写作

> 我先读取真正发生的事实和人物当前状态，再写当前 plot；写完以后把实际变化记成事实。

### 续写

> 我在规划只剩两三个 plot 时就重新评估，而不是等故事线耗尽；新的计划来自最近发生的结果，而不是照着建书时的 100k 大纲硬走。

### 未来变化

> 预测错了就改预测，不改已经发生的事实。

### 工具调用

> 当前任务只有几个真正相关的工具；“这个工具是否可用”由系统告诉我，不需要我自己试。

最终目标公式：

```text
稳定事实
+ 短期确定
+ 中期半确定
+ 远期可变
+ 人物选择
+ 因果反馈
+ 持续重规划
= 适合长篇小说持续创作的 Agent
```

---

# Appendix A：推荐 Profile Manifest 示例

```json
{
  "profiles": {
    "build": {
      "default": [
        "get_build_status",
        "drive_ui",
        "query_arc_library",
        "query_plots",
        "validate_storyline",
        "validate_world"
      ],
      "conditional": {
        "query_profiles": "pen_selected == false",
        "query_gags": "need_gag_reference == true",
        "query_characters": "need_character_reference == true"
      },
      "forbidden": [
        "arc_material_candidates when book_exists == false",
        "save_plot_draft",
        "save_chapter_text",
        "publish_*",
        "scout_*"
      ]
    },
    "write": {
      "default": [
        "get_writing_context",
        "save_plot_draft",
        "save_chapter_text",
        "chapter_quality_gate"
      ]
    },
    "replan": {
      "default": [
        "get_story_state",
        "save_outlines",
        "validate_storyline"
      ]
    }
  }
}
```

此 manifest 应由 ToolSpec/Policy 生成，不手工当真源。

---

# Appendix B：`novel-replan` Skill 草案

```text
ROLE
你是长篇小说的局部规划器，不写完整正文，不规划整本书。

INPUT
只使用 get_story_state 返回的事实与规划状态。

RULES
1. 已写事实不可改。
2. 正式 committed plot 可以延伸，不要一次规划完整剩余全书。
3. 先找当前最强张力与读者问题。
4. 生成 2-3 个差异明显的方向。
5. 每个方向预测人物选择、代价、线程、promise 和新压力。
6. 选定后只生成下一个弧或 4-10 个 plot。
7. 更新 future intents；允许推翻旧 forecast。
8. 每个新 plot 至少有一个状态变化维度。
9. 写入前做因果/人物/阅读拉力自检。
10. 使用 expected_revision 原子提交；stale 时刷新后重新规划。

STOP
新的 committed horizon 足够、validator 通过后停止，把控制权交回 novel-write。
```

---

# Appendix C：第一轮回归场景——“穿越2050：开局修星港”

用已经运行过的题材做 A/B 回归。

输入保持：

```text
2050
穿越 / 系统 / 星际 / 基建 / 军事
笔名：星烬
候选：穿越2050：开局修星港
```

旧行为基线：

```text
book_id 为空仍尝试 book-dependent 工具
7 次 arc query
6 次 plot query
全书一次规划到 112k / 62 plot
```

新行为期望：

```text
1. Router 直接 profile=build
2. arc_material_candidates 不存在于可见工具
3. pen 已选，query_profiles 默认不可见
4. 1 次聚合素材检索，或最多 arc+plot 各 1 次
5. 生成 2-3 个开篇方向并选定
6. 只 commit 前 18k~24k 左右
7. 正式 plot 约 6~10 个
8. H1 保存下一弧方向
9. H2 保存远期“星港/深空威胁”等 intent
10. world/storyline 校验通过
11. 停在用户确认点，agent 无 submit 能力
12. 用户提交后 planning draft 迁移
13. 写到剩余 2 plot 时自动 replan，再扩下一段
```

A/B 重点不是比较“谁的 100k 大纲更漂亮”，而是比较：

> **哪一种架构更能在后续真实写作发生变化后继续保持因果、人物与长篇连续性。**

