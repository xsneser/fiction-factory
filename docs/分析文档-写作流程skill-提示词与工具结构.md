# 写作流程 Skill 分析：提示词与工具结构

> 本文档分析 NovelEngine 的**写作流程 skill**——它的流水线组织、外部驱动 agent 会收到的提示词、以及写作链路相关 MCP 工具的结构。
> 写作日期：2026-09-01。当前驱动形态：**dsh**（侧栏聊天大脑）。文档以 dsh 侧为主线，Claude Code 侧（`.claude/skills/`）单列对照，两者独立演进、不做镜像。

---

## 一、写作流程 skill 全景

### 1.1 三处 skill 目录实测对照

| 目录 | 内容 | 说明 |
|---|---|---|
| `agent-sidecar/skills/` | `novel-scout` / `novel-build-candidates` / `novel-build` / `novel-story` / `novel-publish` | dsh 侧 skill 源 |
| `.dsh/skills/` | 同上 5 个 | 与 `agent-sidecar` 版**逐字节相同**（dsh 的 `skill-filesystem` 发现根，按 `<projectRoot>/.dsh/skills` 扫描） |
| `.claude/skills/` | `novel-master` / `novel-build` / `novel-outline` / `novel-write` / `novel-publish` | Claude Code 侧交互式 skill，独立演进 |

- **dsh 侧**：写作 = `novel-story`（**弧 + 写作合一**，原 outline+write 合并）。这是当前侧栏大脑实际用的写作 skill。
- **Claude Code 侧**：写作被拆成 `novel-outline`（排弧）+ `novel-write`（写正文），由 `novel-master` 调度。
- 唯一规则源：根目录 `NOVEL_AGENT.md`（经 dsh 的 `agent-instructions` 插件注入，只留定义与契约，流程拆到 skill）。

### 1.2 写作链路总览

`novel-story` 的完整流水线（两阶段一体）：

```
novel-story 入口
  │
  ├─ 前置检查：get_book_state(book_id)
  │    phase=config → 阶段一（排弧）；phase=ready → 阶段二（写作）
  │
  ├─ 阶段一：弧 + 桥段
  │    自主生成 outlines/plots/threads/themes
  │    → save_outlines 落盘（phase=plots）
  │    → fill_gags 挂内涵/吸睛（phase=ready）
  │    → validate_storyline / validate_world 校验修正
  │
  ├─ 阶段二：写作（逐桥段循环）
  │    一次 get_writing_context（拿全上下文 + 尾部 style_card）
  │    + get_pen_style（笔名全量风格，未拿到不得动笔）
  │    → 自主生成当前桥段正文（next_bridge）
  │    → save_bridge_draft 落草稿（断点续写保底）
  │    → 累计字数达标 → 生成章节摘要(80-150字) → save_chapter_text 整章落盘
  │
  ├─ 质量门禁：chapter_quality_gate（审查/连续性/追读/伏笔/爽点五项，只报告不修复）
  │
  └─ 全书完 → novel-publish
```

核心原则（skill 原文）：**弧+桥段与正文都由 agent 自主生成**（完整上下文连续），生成后调**薄工具**落盘；不要调内部跑 LLM 的旧工具（`write_next_bridge`/`write_chapter`/`generate_*` 已废弃/删除）。

---

## 二、agent 会收到的提示词（dsh 驱动形态）

### 2.1 四层组装结构

dsh headless 子进程（`node vendor/dsh-ne/lib/bin.js --profile headless --patch storage/dsh_runtime.yml "<任务文本>"`）发给模型的上下文由 4 层叠加：

```
┌──────────────────────────────────────────────────────────────┐
│ ① system prompt（persona）                                      │
│    来源：libraries/dsh_bridge.py 的 _PERSONA                   │
│    注入：_write_runtime_overlay() 写入 storage/dsh_runtime.yml │
│         的 system-prompt 段（整体替换，patch 非深合并）         │
│    内容：见 2.2，仅 4 行，指向 NOVEL_AGENT.md                    │
├──────────────────────────────────────────────────────────────┤
│ ② workspace instructions（唯一业务规则源）                      │
│    来源：NOVEL_AGENT.md 全文                                    │
│    注入：agent-instructions 插件 → <\system-reminder>           │
│    config：maxBytes=20000，instructionFileCandidates=['NOVEL_AGENT.md']│
│            localInstructionFileCandidates=[]（明确不注入 CLAUDE.md）│
├──────────────────────────────────────────────────────────────┤
│ ③ MCP 工具 schema（mcp__novelengine__*，35 个）                 │
│    工具描述 = agent_tools.py 各函数 docstring                   │
│    入参 schema = _func_to_schema() 由签名生成                   │
├──────────────────────────────────────────────────────────────┤
│ ④ 任务文本（headless 命令行 positional 参数）                   │
│    来源：_build_task_text(task, history)                       │
│    内容：[用户]/[助手] 历史回放 + 当前任务，≤20000 字符，无前缀    │
└──────────────────────────────────────────────────────────────┘
```

关键事实：
- **persona 原文**（`dsh_bridge.py` `_PERSONA`）：

  ```
  You are a coding agent powered by the {{model}} model. Your working directory is {{cwd}}.
  You drive the NovelEngine novel-creation platform through its MCP tools (mcp__novelengine__*).
  Follow the workflow, routing, and guardrails in NOVEL_AGENT.md (your workspace instructions)
  — it is the single source of truth.
  ```
  注意：必须是普通字符串（非 f-string），保留字面 `{{model}}`/`{{cwd}}` 供 dsh 后续插值。
- **任务文本格式**：每轮 user 消息 → `[用户] <内容>`，assistant 消息 → `[助手] <内容>`，`\n\n` 连接；超 20000 字符截断中间旧历史（保头部 + 尾部最新消息）。规则/路由/护栏**不拼前缀**（原 `_REINFORCEMENT` 已删），全在 NOVEL_AGENT.md。
- **运行时上下文已关**：`system-prompt` 段 `includeRuntimeContext: false`，且 `tool-fs`/`tool-bash`/`subagent` 等 dsh 系统工具全部 `disabled: true`（见 `agent-sidecar/cordis.patch.yml`），dsh 只能经 MCP 驱动平台，不能直操文件/shell。
- **8KB 工具结果裁剪**：`tool-result-pruner`（thresholdChars=8192，head=4096，tail=1024）。这是「>8KB 只保留头尾」的来源，也是 `style_card` 必须放 payload 尾部的原因。

### 2.2 NOVEL_AGENT.md 内容拆解

NOVEL_AGENT.md = dsh 侧唯一规则源（全文 ~116 行），分「第一部分：定义与契约」「第二部分：护栏」。

**1.1 概念定义（术语契约）**：
- **故事线** = 全文大纲，由「情节弧 + 桥段 + 线程」组成；有且只有一条纵轴（0→nk 字数），**任意一点都该被某个顶层弧占据**（否则叙事空白）。
- **弧** = 有方向/目标的情节单元，是**树状目标节点**，可多层嵌套子弧；顶层弧 = 不被其他弧包含的弧。
- **桥段** = 弧内可执行剧情片段/事件（含场景/人物行动/冲突/概要），可直接扩写正文；每个桥段属于一个弧（`outline_id`）和一条线程（`thread_id`）；**仅最底层弧可拥有桥段**。
- **线程** = 贯穿全书的叙事线索（主线/副线/伏笔线/情感线），由散布在不同弧里的桥段组成。
- **设局/收局**：设局桥段埋钩子（`resolves_plot_id` 空），收局桥段 `resolves_plot_id` 指向设局桥段 id → 读者承诺台账自动登记。
- **章节** = 字数大致相等的可发布文本段（2000-6000 字，具体由书目设定）；正文草稿超字数后由 agent 切分。

**1.2 工具参数契约（写作相关关键规则）**：
- **故事线数据规则**：弧用 `start_word/end_word` 标 **0 基字数跨度**（start 含/end 不含，落盘权威）；顶层弧须覆盖全纵轴；桥段仅挂最底层弧；桥段按 `planned_words`（cover_beats × 200，封顶 1200）累计定位；**全书规模口径**：30~60 章 ≈ 9万~18万字，每顶层弧 ≤10 章 / ≤3 万字；生成/修改后必须 `validate_storyline` + `validate_world` 校验修正。
- **落盘工具**：`save_outlines` / `save_bridge_draft` / `save_chapter_text` / `save_book_meta`（全部薄工具，agent 生成内容后调用）。
- 建书 `drive_ui` 命令契约（`set_field`/`set_world`/`set_outline`/`set_characters`/`submit` 的必填键与字段约束）——详见 skill 与 `agent_tools.py` docstring，本文档 3.3 不展开（非写作主链路）。

**第二部分 护栏（写作相关硬约束）**：
- **笔名风格强约束**：写作/续写前先 `get_pen_style(book_id)` 读该笔名**全量风格**，动笔必须逐条遵守，不得以任何理由绕过；每轮 `get_writing_context` 的 `style_card` 是精简提醒（必读，防风格漂移）。**未拿到风格不得写正文**。
- 工具被 phase 门控拒绝或抛 `BookBusyError` 时调整策略或稍后重试；同一只读工具同参调用超 3 次即循环，应停止并如实汇报。
- `budget_paused` 时停下向用户汇报，不继续烧额度。
- 薄工具（`save_outlines`/`save_chapter_text`）可能阻塞数分钟属正常，等待结果，不要反复同参重查。
- 工具结果可能被 dsh 裁剪（>8KB 只保留头尾）：`style_card` 位于 payload 尾部结构性幸存；**不要臆测「spill 文件」**（文件工具已禁，不存在可读 spill 文件）。
- 拿不准阶段 → 先 `list_books` + `get_book_detail` 看 `phase` 再定 skill。

### 2.3 写作时经工具返回值补的上下文

这些**不是 system prompt**，是 agent 调 MCP 工具拿到的返回值：

| 工具 | 返回的写作上下文 | 用途 |
|---|---|---|
| `get_writing_context(book_id)` | 一次拿全：`book`(含 tags) + `storyline` 全量 + `outline` + `chapters`(最近摘要) + `draft` + `synopsis` + `protagonist` + `next_bridge`(第一个未写桥段) + `next_chapter` + `pen_name` + **`style_card`(尾部)** | 逐桥段循环每轮只调一次；不要再单独调 get_book_detail/get_storyline（是其子集） |
| `get_pen_style(book_id)` | 笔名全量风格：`style_rules`(权威全文 `build_writing_prompt()`) + `style`(prefer 句式) + `forbidden`(禁词/禁句式) + `language_hint` + `discipline`(通用写作纪律) | 动笔前必读；被裁剪/信息不足时复读 |

`style_card` 组装（`libraries/profiles.py` `build_style_card`，~220 字一行）：只取 笔名/语言 身份 + 调性 + 前 3 条 prefer 句式 + 前 5 个带替换禁词 + 前 3 条禁句式，`｜` 连接截断 220 字。目的：防 dsh 尾部裁剪、防风格漂移；完整规则走 `get_pen_style` 的 `build_writing_prompt()`（目标 <1.5KB 防裁剪）。

### 2.4 写作规则指令层

agent 生成正文时内嵌到思考的指令（`novel-story`/`novel-write` 原文）：
- **笔名风格强约束**（见 2.3）。
- **一致性铁律**：人名/系统绑定/数值/设定不得与已写冲突；前后呼应伏笔。
- **视角铁律**：全书统一（默认第三人称），不漂移。
- **语言纪律**：禁 AI 味句式（仿佛/似乎/不禁/只见 堆叠），少用破折号，对话占比自然——以 `get_pen_style` 的笔名规则为准。
- **前三章开篇钩子**（第 1-3 章）：首句强钩子、三章内出第一个爽点、章末留钩。
- **每章字数**：约 `words_per_chapter`（get_book_state 看）；一桥段 800-2500 字。
- **章末钩子**：每章最后一句留悬念/反转/爽点，保追读。

### 2.5 写作任务完整提示词拼装示意

一次「写下一章」的 dsh 会话，模型实际看到的上下文构成：

```
[system]
  persona（4 行，指向 NOVEL_AGENT.md）
[<\system-reminder>]
  NOVEL_AGENT.md 全文（定义 + 契约 + 护栏，≤20000 字节）
[<\system-reminder>]
  可用 skill catalog（`novel-story`: <description>，若 dsh 侧 skill 已装回）→ agent 调 skill 工具加载完整正文
[MCP tools]
  mcp__novelengine__* 35 个工具（name + description(=docstring) + input_schema）
[user]
  [用户] <历史消息1>
  [助手] <历史回复1>
  ...
  [用户] 写下一章/继续写/写第 N 章
[agent 写作时]
  → 调 get_writing_context → 返回书+故事线+next_bridge+style_card
  → 调 get_pen_style → 返回全量风格
  → 自主生成桥段正文 → save_bridge_draft → ... → save_chapter_text
```

> 注：`.dsh/skills/` 目录当前实际存在 5 个 skill（2026-08-28 重写后），agent 会先看到 catalog 再调 `skill` 工具加载 `novel-story` 正文。dsh 的 skill 注入机制见 `vendor/dsh-ne/node_modules/@deepseek-ai/dsh-tool-skill`：catalog 以 `<\system-reminder>` + `<available_skills>` 注入，每行 `` `- \`<name>\`: <description>` ``；agent 调用 `skill` 工具后，完整正文以 `skill-invocation` user message 注入。

---

## 三、工具结构

### 3.1 注册机制

- **唯一来源**：`agent_tools.py` 的 `TOOL_REGISTRY = _build_registry()`（35 个工具）。MCP 服务器（`mcp_server.py`）与 dsh 桥共用。
- 每个 entry：`{"name", "description"(=函数 docstring), "input_schema", "func"}`。
- **入参 schema 自动生成**：`_func_to_schema()` 用 `inspect.signature` + `typing.get_type_hints`；无默认值参数进 `required`；类型映射粗粒度（`dict→object`、`list→array`、`str/int/float/bool`），**无字段级明细**——字段明细全部写在 docstring 里（所以 agent 依赖工具描述的 docstring 学习字段）。
- **工具描述 = docstring**：`mcp_server.py` 用 `functools.wraps` 保留 `__name__`/`__doc__`，FastMCP 据此生成工具名/描述/JSON Schema。**改 docstring 即改 agent 看到的工具说明**。
- **注册顺序有讲究**（`_build_registry` 注释）：导航/建书向导驱动排最前（flash 对列表前部工具更敏感），写作链路工具在中间，侦察/上架靠后。
- `mcp_server.py` 顶部注释「32 个」是**旧文案**，实际 35 个。

### 3.2 两层护栏（叠加在工具函数外）

```
外层：phase 门控（libraries/tool_policy.py PHASE_GATES）
  ↓  按 book.phase 校验，不符抛 phase 门控错误
中层：书锁（_LOCKED_TOOLS，_wrap_book_lock）
  ↓  acquire(timeout=30)，失败抛 BookBusyError；进入前落快照（供 preview_diff/rollback）
内层：原函数（写操作）→ 落盘/副作用
```

- **书锁工具**（`_LOCKED_TOOLS`）：`confirm_world`、`fill_gags`、`save_chapter_text`、`save_bridge_draft`、`save_outlines`、`save_book_meta`。冲突抛 `BookBusyError` → skill 失败处置「稍后重试」。
- **phase 门控**：`save_outlines` 门控 `{config,outlines,plots,ready}`；`save_bridge_draft`/`save_chapter_text`/`save_book_meta`/`chapter_quality_gate` 门控 `{ready}`；`fill_gags` 门控 `{plots}`。phase ∈ config/outlines/plots/ready。
- 另有 **LoopGuard 语义环熔断**（`mcp_server.py` `_wrap_logged`）：同一只读工具同参同结果/连续失败即报错（对应 NOVEL_AGENT 护栏「同参调用超 3 次即循环」）。
- `navigate` 返回特殊标记 `{"__navigate__": url}`，MCP 适配层据此落浏览器意图队列。

### 3.3 写作主链路工具逐个拆解

#### 读取类

**`get_writing_context(book_id)`** — 写正文上下文薄工具（纯规则，无副作用，不门控）
- 返回：`get_book_state()` 全量（book/storyline/outline/chapters/draft）+ 就地提取 `synopsis`、`protagonist`(get_mc)、`next_bridge`(第一个未写桥段，含 plot_id/name/roles/outline_id)、`next_chapter`、`pen_name`、`style_card`(尾部)。
- skill 约定：逐桥段循环每轮只调一次；不再单独调 get_book_detail/get_storyline。

**`get_pen_style(book_id="", profile_id="")`** — 笔名全量风格（纯规则，无副作用，不门控）
- 解析：book_id 优先按书绑定笔名 → 否则 profile_id → 都无默认笔名（枫落）。
- 返回：`pen_name`、`language`、`profile_id`、`style_rules`(build_writing_prompt 权威全文)、`style`(prefer 句式列表)、`forbidden`(words/patterns 结构化)、`language_hint`、`discipline`。
- skill 硬约束：未拿到不得写正文。

**`get_book_state(book_id)`** / **`get_storyline(book_id)`** / **`get_book_detail(book_id)`** — 阶段/状态读取（纯规则，只读）
- get_book_state：book + storyline + outline + chapters(最近摘要) + draft。前置检查用（看 phase/current_chapter/草稿）。
- get_storyline：tl.to_dict() 全量（弧/桥段/线程/内涵/基础设定），排弧/续写时读末尾弧。
- get_book_detail：书名/简介/角色/世界观/进度，跨阶段定位用。

#### 落盘类（薄工具，纯规则，无 LLM）

**`save_outlines(book_id, outlines=None, plots=None, threads=None, themes=None, mode="replace")`** — 排弧
- 书锁 + phase 门控 `{config,outlines,plots,ready}`。
- outlines 每项 → `OutlineSlot`：`id`(缺省 outline_N)、`name`、`start_chapter/end_chapter`(可空)、`start_word/end_word`(权威字数坐标)、`stages`、`parent_arc_id`(弧树)、`narrative_target`、`predecessor/successor`、`transition_type`。
- plots 每项 → `PlotSlot`：`id`、`name`、`category`、`outline_id`(必填，指向最底层弧)、`order`、`thread_id`、`resolves_plot_id`、`roles`。
- 副作用：落盘 storyline；`phase = "plots" if plots else "outlines"`；`_drop_engine` 使引擎会话过期。

**`fill_gags(book_id)`** — 挂内涵/吸睛
- 书锁 + phase 门控 `{plots}`。
- 实现：`fill_themes_and_hooks`(compatible_plots 规则) + `annotate_plot_roles`；`phase = "ready" if plots else "gags"`。

**`save_bridge_draft(book_id, chapter_num, plot_id, plot_name, text)`** — 逐桥段落草稿
- 书锁 + phase 门控 `{ready}`。5 参全必填。
- 副作用：`DeAIEngine.process_rule_based(text)` 规则去 AI 味 → 写 `books/<id>/draft_chapter.json`（`{chapter_num, buffer[], words, bridges[]}`）；chapter_num 变化重置 bridges；同 plot_id 重写原位替换（防重复）；空 plot_id 直接追加。
- 断点续写保底：save_chapter_text 失败时草稿还在。

**`save_chapter_text(book_id, chapter_num, text, title="", summary="", bridge_segments=None)`** — 整章落盘
- 书锁 + phase 门控 `{ready}`。`book_id/chapter_num/text` 必填。
- 8 步副作用（代码注释原文编号）：
  1. 规则去 AI 味（词替换+段落节奏）；防静默丢字：`bridge_segments` 总长 ≥ 正文 70%，否则只按整章正文落盘、不挂桥段并回传 `bridge_warning`。
  2. 规则审查（`ContentReviewer.review`，无 LLM）→ review_dict。
  2.5 硬门禁：正文低于字数下限（`target_words * HARD_MIN_RATIO`）→ raise，不落盘（保留草稿）。
  3. 章节落盘（save_chapter，含 review、bridges）。
  4. 书进度/字数（current_chapter/total_words/status="writing"）。
  5. 故事线 `written_chapter` 进度（本桥段标记已写；漏传时回退草稿 bridges）。
  6. 角色状态（`CharacterStateMachine.update_from_chapter`，规则自动机）。
  7. 读者承诺台账（`_update_promises_ledger_thin`，规则）。
  8. 清进行中草稿（删 draft_chapter.json）。
- 返回：`{ok, chapter, word_count, review, bridge_warning}`。summary 语义摘要是 LLM 职责，由 agent 生成传入（80-150 字）。

**`save_book_meta(book_id, title="", synopsis="")`** — 书名+简介（上架阶段用）

#### 质量门禁类（纯规则，零成本，只报告不修复）

**`chapter_quality_gate(book_id, chapter_num=0, recent_n=5)`** — 完整章节质量门禁
- 只读（phase 门控 `{ready}`，不落书锁，爽点只读标注不落盘）。
- 聚合五项：`review`(审查)、`continuity`(连续性)、`retention`(追读，drop_risk≥7 判掉读)、`promises`(伏笔/承诺台账，overdue/stalled/advanced)、`punch_points`(爽点只读)。
- 返回：`{book_id, chapter, word_count, target_words, passed, complete, summary, issue_count, review_score, overdue_count, stalled_count, drop_risk_count, decision_points[](截断20条), checks{...}}`。
- skill 用法：写完整章后跑一次；`passed=false` → 逐条列给用户定夺，**不自动修复**。

**`validate_storyline(book_id)`** / **`validate_world(book_id)`** — 排弧/建书校验（纯规则，只报告不修复）
- 双模式：传 book_id 校验已落盘书；或内联传 outlines/plots / basic_info dict（步 3 未建书时自查用）。
- 硬规则：①顶层弧完整覆盖故事线纵轴（无叙事空白）+ 弧内空白(arc_fill)；②桥段仅挂最底层弧。world 版：势力名唯一 + 每势力≥1人物 + 人物 faction 有归属。

#### 选材/库查询类（纯规则，只读）

- `arc_material_candidates(book_id)`：按书 tags 查情节弧库模板(templates[:10]) + 桥段库(plots[:30])，供预选弧模板作参考。
- `query_arc_library(keyword, tags)` / `query_plots(category, context, keyword)` / `query_gags(category, scene, keyword)` / `query_characters(keyword, tag)`：四库查询（弧库/桥段库/笑点库/角色原型库），最多 20 条。
- `query_profiles()`：笔名档案（含风格摘要 + 平台注册状态）。

### 3.4 已废弃/已删除工具（防误用）

skill 明确点名「不要调」：
- `write_next_bridge` / `write_chapter`（写作旧工具，已废弃留档）
- `generate_full_outline` / `generate_outlines mode=ai` / `generate_core_conflict` / `generate_factions` / `generate_characters` / `generate_rest_world` / `generate_world` / `generate_outline_preview`（内容生成旧工具，2026-08-24/08-28 大清理已删除，无兜底）
- `confirm_world` / `confirm_outlines` / `world_candidates` / `set_picks`（旧选材/确认机制已删）

内容生成一律改为 **agent 自主生成 → save_outlines / save_bridge_draft / save_chapter_text / save_basic_info / save_book_meta** 薄工具落盘。

---

## 四、关键差异与坑

### 4.1 dsh 侧 vs Claude Code 侧

| 维度 | dsh 侧（agent-sidecar / .dsh） | Claude Code 侧（.claude/skills） |
|---|---|---|
| 写作 skill | `novel-story`（弧+写作合一） | `novel-outline` + `novel-write`（拆两阶段） |
| 工具名前缀 | 无前缀（`save_bridge_draft`） | `mcp__novel-engine__`（`mcp__novel-engine__save_bridge_draft`） |
| 弧坐标口径 | `start_word/end_word`（0 基字数坐标，落盘权威） | `novel-outline` 用 `start_chapter/end_chapter`（章节坐标） |
| 候选数量 | frontmatter「3~5」/正文「5~7」口径不一（以正文为准） | 「3~5 个候选」 |

> ⚠️ 两套 skill **独立演进、不做镜像**。改 skill 时注意：`agent-sidecar/skills/` 与 `.dsh/skills/` 必须**同步两份**（当前逐字节一致），`.claude/skills/` 是另一套，不要混改。

### 4.2 8KB 裁剪与 style_card 幸存策略

- dsh `tool-result-pruner`：工具结果 >8192 字符只保留 head 4KB + tail 1KB。
- 对策：`style_card`（精简风格卡）**结构性放在 get_writing_context payload 尾部**，保证尾部幸存；全量风格走独立薄工具 `get_pen_style`（build_writing_prompt 目标 <1.5KB）。skill 护栏明确：**不要臆测 spill 文件**（文件工具已禁，不存在）。

### 4.3 状态口径

- `storyline.phase` ∈ config/outlines/plots/ready；`book.status` ∈ planning/writing/reviewing/finished/published/paused。
- 门禁：`save_outlines` 在 phase 任一态可调（全门控），`fill_gags` 需 plots，写作落盘类需 ready。phase 不过 → 先走对应阶段（config 先排弧再写）。

### 4.4 失败处置速查

| 现象 | 处置 |
|---|---|
| `BookBusyError` | 稍后重试（写类工具带书级文件锁） |
| `budget_paused` | 预算/额度触发，停下问用户 |
| save_chapter_text 失败 | 草稿还在（save_bridge_draft 已落），检查后重试 |
| 内容为空/报错 | 检查 api.json 与 max_tokens（flash 流式需 ≥8192 留推理余量） |
| 同参循环 | 同一只读工具同参调用 >3 次即循环，停止并如实汇报 |
| 弧内空白/桥段错挂 | `validate_storyline` decision_points 定位，补弧/缩弧/移桥段 |

---

## 附：关键文件路径索引

| 文件 | 作用 |
|---|---|
| `agent-sidecar/skills/novel-story/SKILL.md`（= `.dsh/skills/novel-story/SKILL.md`） | 写作主 skill 全文 |
| `NOVEL_AGENT.md` | 唯一规则源（定义/契约/护栏） |
| `.claude/skills/novel-write|outline|master/SKILL.md` | Claude Code 侧对照 |
| `libraries/dsh_bridge.py` | `_PERSONA` / `_write_runtime_overlay` / `_build_task_text` |
| `agent_tools.py` | `_build_registry` / 全部工具实现 / `_LOCKED_TOOLS` / `_func_to_schema` |
| `libraries/tool_policy.py` | PHASE_GATES phase 门控 |
| `libraries/profiles.py` | `build_style_card` / `build_writing_prompt` / `build_language_hints` |
| `libraries/prompt_harness.py` | 内部 LLM 调用的集中式提示词 harness（**不注入 dsh**） |
| `mcp_server.py` | MCP 适配层（`_wrap_logged` LoopGuard + 工具日志） |
| `vendor/dsh-ne/config/agent-presets/standard/agent.cordis.yml` | persona / agent-instructions / skill-filesystem / tool-result-pruner 配置 |
| `agent-sidecar/cordis.patch.yml` | dsh 模板 patch（禁系统工具护栏） |
| `agent-sidecar/README.md` | ⚠️ 含过时注记（「2026-08-24 skill 已删」——2026-08-28 已重写回，实际两目录同步存在） |
