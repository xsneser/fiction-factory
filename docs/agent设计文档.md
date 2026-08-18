# NovelEngine Agent 设计文档

> 版本：v0.1 ｜ 更新：2026-08-18 ｜ 整理：未初（OpenClaw 侧）
> 定位：Agent 层专项设计——工具注册表 / Agent 循环 / 提示词资产 / Skill 问题。
> 与总览文档关系：待并入 `设计文档-总览-claude.md`（按项目惯例：子文档 → 并入 → 归档）。
> 代码事实以当前 HEAD 为准；本文件为设计建议，落地前需人工确认。

---

## 一、背景：两大开源 Agent 架构对照（2026-08 调研）

### 1.1 DeepSeek Harness（DSH）是什么

2026-08-13 前后随 V4 Pro 正式版开源（npm `@deepseek-ai/dsh`，Node.js monorepo，底座为 Koishi 团队的 Cordis 4.0 IoC 微内核）。官方公式：**Model + Harness = Agent**——模型之外的一切工程化（读文件、调工具、上下文管理、失败重试、长任务）都归 Harness。

四层架构：
1. **宿主内核层**：依赖注入容器、工具注册中心、系统提示词管理（`systemPrompt.section` 按权重分段注入）、schemastery 参数校验
2. **LLM 适配层**：原生适配 DeepSeek 全系，多模型协议互转
3. **网络能力层**：网页抓取、DeepSeek 搜索、MCP 桥接
4. **统一契约层**：invariant 断言 + **Fail-Fast**（长链路出错即时截断，不带病前进）

三个值得抄的设计：
- **双 Surface 物理隔离**：Host 运行时（工具注册/提示词分段/执行器）与 Client Web GUI（插件槽注入 UI）是两套独立插件体系，改 UI 永不碰执行链路
- **遥测一等公民**：turns 与 steps 分开统计、单次工具耗时、上下文占用率、KV 缓存命中率（实测 66%）、token 精确计量，GUI 实时可见
- **模型路由**：Flash/Pro 价差 3 倍（缓存未命中输入 1 元 vs 3 元/Mtok，输出 2 元 vs 6 元），Harness 的核心工作之一是把任务路由到性价比正确的模型

已知坑：高思考强度 + 40 轮以上超长循环存在提前 finish 的早停现象（样本少、未锁定根因）；官方跑分强依赖执行框架，换壳可差十几个点。

### 1.2 OpenClaw 是什么

多渠道个人助理网关 + 可插拔 Agent 运行时。四层正交抽象：**Provider（认证）→ Model（模型）→ Agent Runtime（循环执行者）→ Channel（消息进出）**。

- **Harness 注册表**（`src/agents/harness/`）：内置 `openclaw` 运行时，插件可注册 `codex`/`copilot` 等运行时；按 `agentRuntime.id`（provider/model 级策略）选择，`auto` 模式兜底。CLI 后端（Claude CLI）走 ACP 适配
- **OpenClaw 拥有的面**：模型循环（内置）、会话持久化（SQLite transcript）、工具（原生 + 插件动态工具 + 调用前后 hooks + 工具策略）、上下文组装、compaction、记忆（MEMORY.md + 语义检索）、cron/心跳、子代理（sessions_spawn）
- **插件资源**：extensions / skills / prompts / themes 四类资源清单（package.json manifest），skills 按需注入指令
- **模型路由**：fallbacks 链 + provider 抽象，中立不绑单家

### 1.3 对照表

| 维度 | OpenClaw | DSH |
|---|---|---|
| 出身 | 独立开源社区（多渠道网关） | 模型厂商自研（V4 配套） |
| 核心抽象 | Provider/Model/Runtime/Channel 四层正交 | 万物皆插件（host + client 双体系） |
| 模型绑定 | 中立，几十家 provider | DeepSeek 优先，多协议适配 |
| 循环所有权 | 可插拔 harness（openclaw/codex/copilot/ACP） | 单一自研 loop，深度调优自家模型 |
| 提示词/上下文 | context 组装 + skills + 记忆 + compaction | systemPrompt.section 权重分段注入 |
| 遥测 | 会话级 usage | 一等公民（turns/steps/缓存命中/成本实时） |
| 调度 | cron + 心跳 + 子代理 | profile = 插件组组合 |
| UI | 渠道优先（TUI/webchat/IM） | Web GUI 插件槽 |
| 模型路由 | fallbacks + 手动策略 | 内建 Flash/Pro 成本路由 |

**趋同点（行业共识）**：MCP 桥接、工具注册中心、热重载、提示词分段管理、成本可观测、运行时与 UI 隔离。

### 1.4 对本项目的三条可抄结论

1. **遥测实测化**：KV 缓存命中率直接决定单章成本（缓存命中输入 0.02 元 vs 未命中 1 元，50 倍价差）——我们的 bible 每章都注入，命中率是成本第一杠杆
2. **能力梯度 = 成本空间**：模型路由不是优化项，是成本项
3. **Fail-Fast 契约**：写作链一步脏全链错，必须每步校验 + 即时截断

---

## 二、现状盘点（2026-08-18 代码为准）

### 2.1 已有 Agent 资产

| 资产 | 文件 | 说明 |
|---|---|---|
| 共享工具注册表 | `agent_tools.py`（29 工具） | 单一来源 `TOOL_REGISTRY`，schema 用 `inspect.signature` 自动生成；**Web 侧栏 agent 与 MCP server 双端复用** |
| Agent 循环 | `plugins/agent_loop.py` | 原生 function calling 循环，无状态（浏览器持消息）、`MAX_ITERS=12`、temp 0.1、行动优先原则、delete 确认守卫、工具结果摘要压缩 |
| MCP 适配层 | `mcp_server.py` | FastMCP（mcp 1.x，**勿升 2.0**）逐个注册 TOOL_REGISTRY，stdio，供 Claude Code 等外部客户端驱动 |
| 提示词分段 | `libraries/prompt_harness.py` | PromptHarness：bible 分段（主角/世界观/风格/POV/时代语言…）、bridge prompt、outline context、condensed 降级模式——**等价于 DSH 的 systemPrompt.section** |
| 成本追踪 | `libraries/cost_tracker.py` | 单书预算门控 + 操作级成本记录（估算 token，非实测） |
| 确定性管线 | `libraries/engine.py` | Phase 状态机（outline/writing/reviewing/de_ai/complete）+ Op 枚举编排 |
| 任务系统 | `plugins/task_manager.py` | 并发 + 协作式取消 + 单工具互斥 |
| 专用模块 | outline_generator / outline_agent / storyline_writer / gag_injector / reviewer / de_ai | 各管一段的"受管能力" |

### 2.2 大纲编撰 vs 文章撰写：现在怎么分开的

**结论：两者已经是彻底分离的两条确定性管线**，各自有独立文件、独立提示词、独立兜底策略。工具注册表里也分开（规划组 vs 写作组）。

| 维度 | 大纲编撰（规划侧） | 文章撰写（写作侧） |
|---|---|---|
| 核心文件 | `outline_generator.py`（1197 行） | `storyline_writer.py`（521 行） |
| 生成方式 | **6 阶段 LLM 管线**（SSE 流式，每阶段落盘 + 规则兜底）：故事分析→故事线规划→桥段选择→线程与呼应→内涵复查→一致性验证 | **桥段驱动逐短句组**：每桥段内 3-5 短句一组调 LLM（temp 0.7, max 1600），组间空行分段，携带前文 + 上章结尾 + 角色状态 |
| 交互型 agent | `outline_agent.py`（OutlineAgent，Prompt L）：口语意图解析（modify_plot/add_gag/add_plot/…），temp 0.2——唯一接近"对话式 agent"的模块 | 无自由交互 agent；写作走 `write_next_bridge` / `write_chapter` 工具 + 任务系统 |
| 提示词 | `render_outline_context`（Prompt A~L 中的规划组） | `render_bridge_prompt`（Prompt I）+ `gag_injector.detect`（Prompt J）+ reviewer（软门禁）+ de_ai |
| 兜底 | 任意阶段失败/无 LLM → 规则兜底（`_rule_sequence`/`candidates[:2]`/默认档案） | 空响应重试 2 次、重复词重试、错词重写、0 字桥段跳过下章重试 |
| 质量校验 | `_validate`（规则：连续性/重叠/覆盖/内涵率）+ `_validate_with_llm` 抽查 | `reviewer.py` 软门禁（字数/AI 痕迹/节奏/对话比/断章）→ hint 注入下一桥段 |
| 落盘 | 每阶段 `storyline.json` | 每桥段 `written_chapter`（断点续写）+ 章摘要 + 承诺台账 |

**分层判断**：两条管线都是"确定性流程 + 定点 LLM 调用"，不是自由 agent loop——这是对的，**不要改成自由循环**（¥0.005/章 的性价比、可复现性、可取消性都在确定性上）。需要判断力的地方（大纲对话编辑、审校、救火）才用 agent 形态。

### 2.3 建议的三层 Agent 模型

```
┌─ 受管 Agent（引擎 Op 管线）─────────────┐  无人值守、固定流程、预算门控、可取消
│  规划: outline_generator 6 阶段         │  ← 保持确定性，不自由化
│  写作: storyline_writer 短句组循环       │
│  审校: reviewer 软门禁 → hint 注入       │
├─ 自由 Agent（侧栏 / MCP）───────────────┤  用户在场、高权限、关键操作确认
│  agent_loop（工具循环, MAX_ITERS=12）    │  ← 已具备，补: 长任务委托给任务系统
│  MCP（Claude Code 外部驱动）             │
├─ 主编 Agent（新增, 空闲驱动）────────────┤  借鉴 OpenClaw cron + 子代理
│  空闲心跳: 审校已写章节 / 番茄侦察补库    │  ← 项目缺的"主动层"
│  批量生产: 队列化多书续写                │
└────────────────────────────────────────┘
```

---

## 三、Skill 问题：大纲编撰与文章撰写是否需要 Skill

### 3.1 什么是 Skill（生态定义）

OpenClaw / Claude Code 生态中，**Skill = 一个目录 + SKILL.md**（指令 + 示例 + 可选脚本/引用文件），在相关任务出现时按需注入模型上下文。本质是**把可复用的专业知识从代码和对话里抽出来，做成按需加载的提示词资产**。与硬编码 prompt 相比：可维护、可版本化、可按场景渐进加载（省 token）。

### 3.2 项目现状：PromptHarness ≈ 代码内嵌的 Skill 系统

你的 `prompt_harness.py` 已经是一个 Skill 系统的等价物——分段 bible、按场景渲染、condensed 降级。**缺的不是能力，是资产形态**：大纲方法论（6 阶段）、写作铁律（7 条）、去 AI 味规则目前散在代码字符串里，改一次提示词要改代码、重启、清浏览器缓存。

### 3.3 网络现成 Skill 调研（GitHub，2026-08-18 检索）

| 仓库 | 定位 | 大纲/写作拆分方式 | 可借鉴点 |
|---|---|---|---|
| `bw448/novel-writing-skills` | Claude Code 长篇三件套 | **bestseller-novel-outline（世界观/人物/全书大纲/伏笔）→ outline-refinement（每章拆写作提纲）→ novel-chapter-write（照提纲写正文）** | 与你 outline_generator → 桥段 → storyline_writer 的分解完全同构，验证了拆分正确 |
| `tance-mang/chinese-webnovel-skills`（网文工坊） | 中文网文全流程工具包（33 skills） | 选题→灵感→大纲→黄金开篇→金手指→人设→正文扩写→爽点打脸→节奏标注→追读诊断→**去AI味**→投稿过稿→平台趋势；有 CLI 可接 DeepSeek/本地 Ollama | 「追读诊断/爽点标注」是你没有的阶段；去AI味方法论可对照 de_ai.py |
| `zy-zmc/tianming-skill`（天命） | 长篇小说协同创作 skill v2.0 | 995 行单提示词重构为 **30+ 协议文件 + 渐进式披露 + 指令路由**（「天命：大纲」/「规划」/「目录」/「草案」/「正文」/「体检」/「存档」）；5 件套知识库（世界基石/世界观/角色档案/事件/文风样本） | **token 降 50-70%**——渐进式披露正是你的 bible 该走的方向；知识库强制依赖（缺文风样本禁写正文）值得抄 |
| `wzxsph/Novel-Claude` | Python 长篇生成框架（学习项目） | 世界构建→分卷规划→章节写作→记忆检索（ChromaDB RAG）；**Skill 插件 + 事件总线 + 热重载**；`MODEL_ID`/`FLASH_MODEL_ID` 双模型路由 | 与你架构最像：事件总线插件化 + 双模型路由就是 DSH 模型路由的落地形态 |
| `mane23-ai/claude-novel-skill` | 韩语小说创作 skill | 13 指南 + 6 追踪系统（角色/物品/主题/场所/冲突/读者体验）+ 连载/出版管线 + 4 类 genre 预设 | 追踪系统 = 你的承诺台账 + 角色状态的更完整版 |
| `PhosAQy/novel-skills` | Claude Code 全流程系统 | 分卷规划 → 章节生成 → 质量审查 → 数据分析 | 审查/分析闭环与你 reviewer 对应 |
| `Calliope-Editor/writing-skills` | 只审不改 | 编辑模拟器/批评模拟器/角色工作/写作训练，明确不代笔 | 可作审校 agent 的提示词参考 |
| `vagabondshun/snowflake-writer` | 雪花写作法 skill | 渐进展开大纲 | 大纲生成另一种范式，可对照 |
| `ricky-theseus/DaisyWriter` | 写作工具包合集 | 网文/短篇/博客/出版自动化，OpenCode/Claude Code/Codex 通用 | 多 harness 兼容的 skill 组织方式 |

**生态共识**：① 大纲编撰与文章撰写必须分离成独立阶段（甚至独立 skill），且写作侧再拆"规划→正文"两级——你的项目天然符合；② 头部项目都在把大提示词拆成按需加载的小文件（渐进式披露），省 token 是硬收益；③ 中文网文向工具已覆盖"追读诊断/爽点标注/去AI味/投稿"等完整商业闭环。

### 3.4 结论与建议

**要 Skill，但做"轻量 Skill 资产化"，不引入 Skill 运行时。**

1. **内部：把提示词资产抽成 `skills/` 文档目录**（不进代码，或与代码分离）：
   - `skills/outline/`——大纲 6 阶段方法 + 各阶段 Prompt + 兜底规则说明
   - `skills/writing/`——写作铁律 + 桥段驱动方法论 + 重试通道规则 + 去 AI 味清单
   - 由 PromptHarness 按需读取（渐进式披露），`SKILL.md` 只做索引
   - 收益：改提示词不动代码、可 diff 可回滚、agent_loop/MCP/未来外部 harness 共用一份资产
2. **外部：不引入现成 skill 代码**，但把「网文工坊（追读诊断/爽点标注）」「天命（渐进式披露/知识库依赖）」「Novel-Claude（事件总线/双模型路由）」列为方法论参照，落地进 roadmap（§五）
3. **边界**：skill 化只针对"专业知识文档"；工具（agent_tools）与流程（engine 状态机）保持代码形态，不混入 skill 目录

---

## 四、Agent 设计决策（按优先级）

### 4.1 P0 遥测实测化（TelemetryTracker）

现状：`cost_tracker.py` 用 `estimate_tokens_chinese` 估算，无缓存命中、无耗时、无重试。
目标：每次 LLM 调用记录真实 `usage`（DeepSeek API 返回 `prompt_tokens`/`completion_tokens`/`prompt_cache_hit_tokens`）+ 耗时 + 重试次数 + 操作类型。
价值：缓存命中率 = 成本第一杠杆（命中 0.02 元 vs 未命中 1 元/Mtok）；bible 是否该常驻/该 condensed，用数据说话。

### 4.2 P0 模型路由层

现状：`MODEL_RATES` 有价格表无路由逻辑。
目标：Op → 模型映射：规划/大纲/审校/救火 → `deepseek-reasoner`（或 v4-pro）；节拍生成/笑点注入/去 AI 味/摘要 → flash。按预算剩余自动降级链（pro → flash → 本地 qwen3.6-35b）。
参照：DSH 的 Flash/Pro 路由；Novel-Claude 的 `MODEL_ID`/`FLASH_MODEL_ID`。

### 4.3 P1 Fail-Fast 契约

现状：大纲侧已有规则兜底，写作侧已有重试通道，但未系统化。
目标：每步输出 schema 校验（章节字数范围、JSON 结构、桥段 id 存在性、角色状态可解析）→ 失败重试 N 次 → 仍失败即时截断并落错误日志（不带着脏状态往下跑）。

### 4.4 P1 上下文预算自动降级

现状：`build_book_bible_condensed` 已存在，无触发逻辑。
目标：每步调用前估算输入 token，超预算自动切换 condensed bible / 裁摘要窗口（最近 5 章 → 3 章）/ 省略低权重 section。
参照：DSH 上下文占用率遥测 + 天命渐进式披露（token 降 50-70%）。

### 4.5 P2 插件契约制度化

现状：`plugins/` 已有（fanqie_scout 等），但 agent_tools 的 29 工具是硬编码注册表。
目标：定义 Plugin 契约（`register_tools / register_hooks / register_prompt_sections`），新能力（新平台爬虫、新写作风格、新阶段）以插件注册，不碰核心。
参照：DSH 双 Surface 隔离 + 热重载；Novel-Claude 事件总线。

### 4.6 P2 主编 Agent（主动层）

现状：引擎被动（用户点按钮）、侧栏被动（用户发消息）。
目标：空闲心跳驱动——自动审校已写章节、从番茄侦察兵补桥段库、批量多书续写队列。借鉴 OpenClaw cron + 子代理（sessions_spawn）模式，与 task_manager 复用。

---

## 五、开放决策与待办

- [ ] 总览文档 §8.5 已记录 Agent 接口现状，本文件待并入（按项目惯例）
- [ ] 会话记忆 v2（服务端持久化）：总览 §8.5 标注"后续参考开源 deepseek harness 再改"
- [ ] skill 目录是否 git 管理、是否随 books/ 落盘备份
- [ ] 模型路由默认值（各 Op 的模型映射表）需一次人工确认
- [ ] 追读诊断/爽点标注阶段是否立项（参照网文工坊）
- [ ] MCP 工具名与 Web 工具名冲突规避（双进程同书操作已有警告）

---

## 附：调研来源

- DSH：CSDN《DeepSeek Harness（DSH）架构拆解，三个值得抄的设计》（2026-08-13 前后）、linux.do npm README 转帖、dsh.so（社区资源站，官方仓库直连需代理）
- OpenClaw：本地文档 `agent-runtime-architecture.md` / `concepts/agent-runtimes.md`
- Skill 生态：GitHub API 检索（2026-08-18），各仓库 README
