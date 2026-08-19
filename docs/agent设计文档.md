# NovelEngine Agent 设计文档

> 版本：v0.2 ｜ 更新：2026-08-19 ｜ 整理：未初
> 定位：Agent 层**可执行设计**——工具注册表 / Agent 循环 / 提示词资产 / Skill 资产化 / 遥测 / 模型路由 / Fail-Fast / 上下文降级 / 插件契约 / 主编 Agent / 会话记忆 v2。
> 与总览文档关系：待并入 `设计文档-总览-claude.md`（按项目惯例：子文档 → 并入 → 归档）。
> v0.1（2026-08-18）为设计建议，标注「落地前需人工确认」；v0.2 将六条设计决策补全为**可直接照做的数据结构 / 接口签名 / 映射表 / 边界条件**，全部基于当前 HEAD 代码事实（已逐一核对 `core/llm_client.py`、`core/models.py`、`libraries/cost_tracker.py`、`libraries/prompt_harness.py`、`libraries/engine.py`、`libraries/outline_generator.py`、`libraries/outline_agent.py`、`libraries/reviewer.py`、`plugins/agent_loop.py`、`plugins/task_manager.py`、`agent_tools.py`、`mcp_server.py`、`ui/web_blueprints/ctx.py` 等）。
>
> **全局约定**：不引入 jsonschema / fastapi 等重依赖；落盘文件均 UTF-8；所有「待人工确认」项集中在 §十三，全文 `待确认` 标记与之一一对应。

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

## 二、现状盘点（2026-08-19 代码为准）

### 2.1 已有 Agent 资产

| 资产 | 文件 | 说明 |
|---|---|---|
| 共享工具注册表 | `agent_tools.py`（29 工具） | 单一来源 `TOOL_REGISTRY`，schema 用 `inspect.signature` 自动生成；**Web 侧栏 agent 与 MCP server 双端复用** |
| Agent 循环 | `plugins/agent_loop.py` | 原生 function calling 循环，无状态（浏览器持消息）、`MAX_ITERS=12`、temp 0.1、max_tokens 8192、行动优先原则、delete 确认守卫、工具结果摘要压缩（`_compact` 上限 3000 字符、`conv` 保留最近 20 条） |
| MCP 适配层 | `mcp_server.py` | FastMCP（mcp 1.x，**勿升 2.0**）逐个注册 TOOL_REGISTRY，stdio，供 Claude Code 等外部客户端驱动 |
| 提示词分段 | `libraries/prompt_harness.py` | PromptHarness：bible 分段（主角/世界观/风格/POV/时代语言…）、`render_bridge_prompt`/`render_detector_prompt`/`render_summary_prompt`/`render_outline_context`、condensed 降级——**等价于 DSH 的 systemPrompt.section** |
| 成本追踪 | `libraries/cost_tracker.py` | 单书预算门控 + 操作级成本记录（**估算 token，非实测**）；`MODEL_RATES` 价格表有价格无路由 |
| 确定性管线 | `libraries/engine.py` | Phase 状态机（`class Op(Enum)` 含 `PAUSE`，`_prepare_chapter_context`/`_summarize_chapter` 等）+ Op 枚举编排 |
| 任务系统 | `plugins/task_manager.py` | 并发 + 协作式取消 + 单工具互斥（`start`/`ensure_single`） |
| 专用模块 | outline_generator / outline_agent / storyline_writer / gag_injector / reviewer / de_ai | 各管一段的"受管能力" |

### 2.2 大纲编撰 vs 文章撰写：现在怎么分开的

**结论：两者已经是彻底分离的两条确定性管线**，各自有独立文件、独立提示词、独立兜底策略。工具注册表里也分开（规划组 vs 写作组）。

| 维度 | 大纲编撰（规划侧） | 文章撰写（写作侧） |
|---|---|---|
| 核心文件 | `outline_generator.py`（1197 行） | `storyline_writer.py`（521 行） |
| 生成方式 | **6 阶段 LLM 管线**（SSE 流式，每阶段落盘 + 规则兜底）：故事分析→故事线规划→桥段选择→线程与呼应→内涵复查→一致性验证 | **桥段驱动逐短句组**：每桥段内 3-5 短句一组调 LLM（temp 0.7, max 1600），组间空行分段，携带前文 + 上章结尾 + 角色状态 |
| 交互型 agent | `outline_agent.py`（OutlineAgent，Prompt L）：口语意图解析（modify_plot/add_gag/add_plot/…），temp 0.2——唯一接近"对话式 agent"的模块 | 无自由交互 agent；写作走 `write_next_bridge` / `write_chapter` 工具 + 任务系统 |
| 提示词 | `render_outline_context`（Prompt A~L 中的规划组） | `render_bridge_prompt`（Prompt I）+ `gag_injector.detect`（Prompt J）+ reviewer（软门禁）+ de_ai |
| 兜底 | 任意阶段失败/无 LLM → 规则兜底（`_rule_sequence`/`candidates[:2]`/`_default_basic_info`） | 空响应重试 2 次、重复词重试、错词重写、0 字桥段跳过下章重试 |
| 质量校验 | `_validate`（规则：连续性/重叠/覆盖/内涵率）+ `_validate_with_llm` 抽查 | `reviewer.py` 软门禁（字数/AI 痕迹/节奏/对话比/断章，`check_cliffhanger` 章末钩子）→ hint 注入下一桥段 |
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

### 2.4 前置依赖：llm_client 改造（P0，新增）

> 状态：新增 ｜ 优先级 P0 ｜ 待确认：否（但 §4/§5 全部依赖本节，须先落地）

v0.1 的 P0 遥测（§4）与 P0 模型路由（§5）都要求 LLM 调用支持「按调用指定模型」与「读回真实 usage」，但**当前 `core/llm_client.py` 三个方法均硬编码 `self.cfg.model`、均不返回 API 的 `usage`**。这是两个决策的共同前置地基，v0.1 未提及，本节补全。

**现状（已核实）**：`LLMClient.call()` 返回 `str`、`call_tools()` 返回 `choices[0].message` dict、`stream_deltas()` yield `(delta_key, text)`。全部调用方依赖这些返回形态，改造须**向后兼容**。

**2.4.1 统一返回形态：回调钩子（而非改返回结构）**

改 `call` 返回对象会波及 outline_generator / engine / gag_injector / outline_agent / agent_loop 所有调用点，违背最小侵入。采用**回调钩子**：

```python
# core/llm_client.py 新增
@dataclass
class UsageInfo:
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0     # DeepSeek 实际字段名
    prompt_cache_miss_tokens: int = 0    # DeepSeek 实际字段名
    total_tokens: int = 0
    latency_ms: int = 0                  # 方法入口到返回的墙钟毫秒（含重试重发）
    retries: int = 0                     # HTTP 层重试次数（首试成功=0）
    status: str = "ok"                   # ok / error / aborted
    error: str = ""
    request_id: str = ""                 # 响应顶层 id，用于关联日志

class LLMClient:
    def __init__(self, api_config, on_usage=None):
        self.on_usage = on_usage         # callable(UsageInfo)->None
    def set_on_usage(self, cb) -> None: ...
```

- **回调契约**：`on_usage(UsageInfo)` 同步调用；**TelemetryTracker 在回调内部必须 `try/except` 吞掉一切异常**，绝不允许遥测故障污染 LLM 主链路。
- `ctx.get_llm()` 构建单例时注入默认回调 → 全局 TelemetryTracker（§4）。MCP 进程独立 import ctx，同样注入。
- `operation` 字段不进入 `UsageInfo`（llm_client 不知道语义操作），由调用方通过 §4 的 `set_op()` 线程本地栈标记。

**2.4.2 三方法签名草案（新增参数均带默认值，向后兼容）**

```python
def call(self, system_prompt: str, user_prompt: str,
         temperature: float = 0.7, max_tokens: int = 4096,
         model: str = None, op: str = "") -> str
    # model=None → 用 self.cfg.model；op 用于直接给遥测标记（可空，线程本地兜底）

def stream_deltas(self, system_prompt: str, user_prompt: str,
                  temperature: float = 0.7, max_tokens: int = 4096,
                  model: str = None, op: str = ""):
    # 仍是生成器，yield (delta_key, text) 不变

def call_tools(self, messages: list, tools: list,
               temperature: float = 0.2, max_tokens: int = 8192,
               model: str = None, op: str = "") -> dict
    # 仍是返回 choices[0].message dict 不变
```

现有调用方（如 `engine._summarize_chapter` 的 `self.llm.call(..., temperature=0.3, max_tokens=1024)`）位置/关键字均兼容，无需改动即可编译运行；遥测与路由在调用点逐步接入。

**2.4.3 非流式 usage 提取**

- `call`：成功路径在 `json.loads(data)` 后取 `data["usage"]`；构造 `UsageInfo` 并在 `return` 前调用 `self._emit_usage(...)`。
- `call_tools`：同上，从响应顶层取 `usage`。
- 兜底：某些 DeepSeek 版本把命中字段嵌套在 `prompt_tokens_details` 下，需两种位置都查。

**2.4.4 流式 usage：`stream_options` + 生成器内捕获，仍走回调**

- body 增加 `"stream_options": {"include_usage": True}`（DeepSeek/OpenAI 兼容；老 provider 忽略该字段无副作用）。
- `read_chunked` 循环内：每个 chunk 若 `event.get("usage")` 非空 → 暂存 `_last_stream_usage`；`[DONE]` 时停止。
- **转发方式**：不改变 `(delta_key, text)` yield 契约。流正常走完（收到 `[DONE]`）时，`finally` 前调用 `self._emit_usage(...)`，`latency_ms` = 首个 chunk 到 `[DONE]` 的墙钟时长。
- **边界**：客户端提前断开导致生成器 `GeneratorExit`（`resp.release_conn()` 的 `finally` 路径）→ emit `status="aborted"` 且 token 全 0，用于链路审计。

**2.4.5 依赖关系**

- 依赖：`core/models.py` 的 `APIConfig` 不变（模型名仍可传）。路由层（§5）用 `model=` 参数对接。
- 被依赖：§4 TelemetryTracker（消费回调）、§5 模型路由（传 model）、§6 Fail-Fast（错误落日志）。
- **待确认 #7**：`on_usage` 是否同时覆盖 `test_connection()`（建议：覆盖，op="test_connection"，但不落遥测存储）。

---

## 三、Skill 资产化（轻量，不引入 Skill 运行时）

> 状态：重写 v0.1 §3 ｜ 优先级 P1 ｜ 待确认：#4

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

### 3.4 结论与设计

**要 Skill，但做"轻量 Skill 资产化"，不引入 Skill 运行时。**

**3.4.1 目录结构（设计）**

```
skills/
├── outline/
│   ├── SKILL.md                    # 索引：6 阶段方法论 + 各 stage 文件清单
│   ├── stage-1-story-analysis.md
│   ├── stage-2-storyline-plan.md
│   ├── stage-3-bridge-arrangement.md
│   ├── stage-4-threads.md
│   ├── stage-5-theme-review.md
│   ├── stage-6-validate.md
│   └── fallback-rules.md           # 各阶段规则兜底说明
└── writing/
    ├── SKILL.md                    # 写作铁律 7 条 + 桥段驱动方法论 + 重试通道规则
    ├── bridge-driven-method.md
    ├── writing-rules.md
    ├── de-ai-checklist.md
    └── opening-mode.md
```

**SKILL.md 模板**：

```markdown
# <skill 名>
## 用途
## 适用场景（何时注入）
## 引用的文档片段（渐进披露清单）
## 版本
```

**3.4.2 PromptHarness 读取契约（设计）**

- `harness.load_skill(skill, section) -> str`：按需读文件，进程内缓存。
- `render_outline_context` 只注入对应 phase 的 stage 文件；写作只注入 `writing-rules.md` 摘要。渐进披露省 token。
- 收益：改提示词不动代码、可 diff 可回滚、agent_loop/MCP/未来外部 harness 共用一份资产。

**3.4.3 边界**

- skill 化只针对"专业知识文档"；工具（agent_tools）与流程（engine 状态机）保持代码形态，不混入 skill 目录。
- 外部：不引入现成 skill 代码，把「网文工坊（追读诊断/爽点标注）」「天命（渐进式披露/知识库依赖）」「Novel-Claude（事件总线/双模型路由）」列为方法论参照。
- **git 管理（待确认 #4，建议：是）**：`skills/` 进 git（文本资产可 diff/回滚/评审），不随 `books/` 落盘；如书级覆盖，用 `books/<id>/skills_override/<name>.md`，harness 优先读覆盖（P2 可选）。

---

## 四、P0 遥测实测化（TelemetryTracker）

> 状态：重写 v0.1 §4.1 ｜ 优先级 P0 ｜ 待确认：#3（存储选型）

### 4.1 目标与现状差距

现状：`cost_tracker.py` 用 `estimate_tokens_chinese` 估算，无缓存命中、无耗时、无重试。目标：每次 LLM 调用记录**真实 `usage`**（DeepSeek API 返回 `prompt_tokens`/`completion_tokens`/`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`）+ 耗时 + 重试次数 + 操作类型。价值：缓存命中率 = 成本第一杠杆（命中 0.02 元 vs 未命中 1 元/Mtok）；bible 是否该常驻/该 condensed，用数据说话。

### 4.2 数据模型

```python
# libraries/telemetry.py（新增）
@dataclass
class TelemetryRecord:
    ts: str                    # ISO8601 毫秒，如 "2026-08-19T12:00:00.123"
    book_id: str = ""          # 可空（agent 会话级、测试连接）
    operation: str = ""        # 见 OP_NAMES 规范表
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    retries: int = 0
    status: str = "ok"         # ok / error / aborted
    error: str = ""
    endpoint: str = ""         # deepseek / local，由 model_router 推导
    degraded_level: int = 0    # 上下文降级档（§7），默认 0 未降级
```

**OP_NAMES 规范表**（遥测与路由共用，新模块 `libraries/ops.py` 定义字符串常量）：

| operation | 含义 | 采集方 |
|---|---|---|
| `outline_analyze` | 大纲 P1 故事分析 | outline_generator |
| `outline_plan` | 大纲 P2 故事线规划 | outline_generator |
| `outline_plot` | 大纲 P3 桥段编排 | outline_generator |
| `outline_threads` | 大纲 P4 线程与呼应 | outline_generator |
| `outline_theme_review` | 大纲 P5 内涵复查 | outline_generator |
| `outline_validate` | 大纲 P6 LLM 一致性验证 | outline_generator |
| `write_group` | 桥段短句组写作 | storyline_writer / engine |
| `write_diagnosis` | 写前编辑诊断（预留，免费规则为主） | engine |
| `chapter_summary` | 章节语义摘要 | engine |
| `gag_detect` | 笑点探测器 | gag_injector |
| `de_ai` | 去 AI 味 LLM 层 | de_ai |
| `review_llm` | LLM 审校（当前 reviewer 为纯规则，预留） | reviewer |
| `book_meta_title` / `book_meta_synopsis` | 书名/简介 | engine |
| `world_build` / `world_struct` / `world_candidates` / `characters` | 世界观生成系列 | world_builder |
| `outline_agent_parse` | 大纲助手意图解析 | outline_agent |
| `agent_loop_tools` | agent_loop 每次 call_tools | agent_loop |
| `test_connection` | 设置页连接测试（**不落遥测存储**） | settings |

### 4.3 采集方式（设计）

- `TelemetryTracker.set_op(op)` 用**线程本地栈**（`contextvars` 或 `threading.local` 的栈）：进入包 `push`、退出 `pop`；`on_usage` 回调弹出栈顶 op 并落盘。嵌套调用（如 write_group 内又触发 gag_detect）时子调用 push 覆盖，正确归属。
- 采集点清单：`engine._exec_write_storyline_chapter`/`_write_next_bridge_stream` 包 `write_group`；`_summarize_chapter` 包 `chapter_summary`；`_generate_book_meta` 包 `book_meta_*`；`gag_injector.detect` 包 `gag_detect`；outline_generator 六阶段各自 `_stream_decision_content` 调用分别包对应 op；`outline_agent._parse` 包 `outline_agent_parse`；`agent_loop.run_agent_loop` 包 `agent_loop_tools`；world_builder 四个生成方法各包 op。

### 4.4 存储：全局按日分片 JSONL（推荐）

| 方案 | 优点 | 缺点 |
|---|---|---|
| A：按书 `books/<id>/telemetry.jsonl` | 与书数据同址，按书清理方便 | Web/MCP 双进程同写一本书 → 锁面扩大；跨书聚合难 |
| B：全局 `storage/telemetry/<YYYY-MM-DD>.jsonl`（**推荐**） | 单写入点、天然按日轮转、聚合按 `book_id` 字段过滤 | 需要跨进程锁（见下） |

字段 `book_id` 在每行内，查询时过滤。体积：每 LLM 调用一行（约 300 字节），一日几百~几千行，写穿透可接受，无需内存缓冲。

**Windows 并发写**（Web Flask 多线程 + MCP 独立进程同时写），新增 `core/jsonl.py`：

```python
# core/jsonl.py（新增）—— append_jsonl(path, record)
# 1) 进程内：模块级 threading.Lock（同进程互斥）
# 2) 进程间：锁文件 path + ".lock"，open("a") 建文件，
#    Windows 用 msvcrt.locking(fileno, LK_LOCK, 1) 锁首字节，POSIX 用 fcntl.flock(EX)
#    锁内：open(path,"a").write(json.dumps(record)+"\n").flush().close()
#    锁外：解锁并 close 锁文件（不删除锁文件，避免删除竞态）
# 3) 异常降级：锁不可用时，退回 open("a") 单行写（尽力而为，不阻塞主链路）
```

### 4.5 聚合查询 `GET /api/telemetry`（设计）

```
GET /api/telemetry?book_id=&op=&days=7&group_by=op|model
```

```json
{
  "ok": true,
  "days": 7,
  "count": 1234,
  "summary": {
    "total_cost_yuan": 0.85,
    "cache_hit_tokens": 400000, "cache_miss_tokens": 200000,
    "cache_hit_rate": 0.667,
    "p50_latency_ms": 812, "p95_latency_ms": 4320,
    "total_latency_seconds": 520.3,
    "by_op": [
      {"op": "write_group", "calls": 400, "cost_yuan": 0.5,
       "cache_hit_rate": 0.72, "p95_latency_ms": 3100}
    ],
    "by_model": [{"model": "deepseek-v4-flash", "calls": 1100, "cost_yuan": 0.75}]
  }
}
```

- 成本计算：`cost_yuan = (prompt_tokens/1e6)*rate.in + (completion_tokens/1e6)*rate.out`，rate 取 `cost_tracker.MODEL_RATES`。
- p50/p95：手写百分位（排序取 index），不引 numpy。
- 缓存命中率定义：`hit/(hit+miss)`；`hit` 与 `miss` 均缺失时按 `0`（避免除零，返回 `cache_hit_rate: null`）。

### 4.6 与 CostTracker 的关系（明确方案）

- **TelemetryTracker 是成本唯一事实源**（实测 usage）；`CostTracker` 退化为「预算门控器」：
  - 保留 `budget` / `remaining()` / `check_budget(estimate)` 做**调用前预判门控**（只能估算）。
  - `CostTracker.record()` **废弃估算路径**，改为在每次 `on_usage` 落盘时把该记录的实际成本累加到 `spent`。
  - 由 TelemetryTracker 落盘后回调 `cost_tracker.add_measured(cost_yuan)`，或 `engine` 在 `on_usage` 里同时更新两者。
- **边界**：`spent` 只增不减；预算门控仍用 `remaining() <= 0` 触发 `Op.PAUSE`（engine 现有逻辑不动）。`cost.json` 的 save/load 保留（存量迁移：加载时仅恢复 budget 与已累计 spent，records 数组不再维护，改读 telemetry）。

---

## 五、P0 模型路由层

> 状态：重写 v0.1 §4.2 ｜ 优先级 P0 ｜ 待确认：#1（默认表）

### 5.1 多端点配置：新增静态 `models.json`（不动 api.json 的设置页写入）

理由：`ui/web_blueprints/settings.py` 写 api.json 只含单端点；保持其「主端点」语义，路由表独立成 `models.json`（静态，git 管理，无密钥风险走环境变量）。

```json
{
  "endpoints": [
    {"id": "deepseek", "base_url": "https://api.deepseek.com", "url_strict": false,
     "api_key_env": "NOVEL_DEEPSEEK_KEY", "api_key": "",
     "verify_ssl": false, "http_timeout_seconds": 300,
     "models": ["deepseek-v4-flash", "deepseek-reasoner", "deepseek-v4-pro"]},
    {"id": "local", "base_url": "http://127.0.0.1:11434/v1", "url_strict": true,
     "api_key": "ollama", "verify_ssl": false, "http_timeout_seconds": 300,
     "models": ["qwen3.6-35b"]}
  ],
  "aliases": {"flash": "deepseek-v4-flash", "reasoner": "deepseek-reasoner",
              "pro": "deepseek-v4-pro", "local_qwen": "qwen3.6-35b"},
  "prices": {"deepseek-v4-pro": {"input": 3.0, "output": 6.0}},
  "availability": {
    "deepseek-v4-flash": {"enabled": true},
    "deepseek-reasoner": {"enabled": true},
    "qwen3.6-35b": {"enabled": true, "health_check_url": "http://127.0.0.1:11434/v1/models"}
  },
  "fallback_chain": {
    "deepseek-reasoner": ["deepseek-v4-flash", "qwen3.6-35b"],
    "deepseek-v4-flash": ["qwen3.6-35b"],
    "qwen3.6-35b": []
  }
}
```

- `api_key_env` 优先，其次 `api_key` 字面量；api.json 的主 key 作为 deepseek 端点兜底。
- `prices` 与 `cost_tracker.MODEL_RATES` 合并（`MODEL_RATES` 保持为代码内默认，`models.json` 可覆盖）。

### 5.2 Op→模型映射默认表（待确认 #1）

> 铁律：**写作主体一律 `deepseek-v4-flash`**（项目质量基线，见 project memory）；reasoner 只用于规划/审校/意图解析等判断密集处。

| Op | 默认模型 | 理由 | 待确认点 |
|---|---|---|---|
| outline_analyze | reasoner | 世界观/主角判断密集 | |
| outline_plan | reasoner | 大纲模板选择 | |
| outline_plot | **reasoner→flash 观察** | 每个阶段一次、调用次数多，reasoner 成本高 | **是否降 flash（#1）** |
| outline_threads | reasoner | 线程归属判断 | |
| outline_theme_review | reasoner | 内涵复查 | |
| outline_validate | reasoner | 一致性验证 | |
| outline_agent_parse | reasoner | 误判代价高（会改配置） | |
| review_llm | reasoner | 审校=判断密集 | |
| **write_group / write_bridge** | **flash** | 写作铁律 | |
| chapter_summary | flash | 摘要，输入≤500 token 约定 | |
| gag_detect | flash | 探测器约定输入≤500 输出≤150 | |
| de_ai | flash | 规则为主，LLM 层可选 | |
| book_meta_title / book_meta_synopsis | flash | 命名/简介 | |
| world_build / world_candidates / characters | flash | 发散生成 | |
| world_struct | reasoner | 设定文→结构化 JSON 抽取，判断密集 | **是否 reasoner（#1）** |
| agent_loop_tools | flash | 工具选择，MAX_ITERS=12 兜底 | |

### 5.3 `resolve_model` 降级链（设计）

```python
# libraries/model_router.py（新增）
class ModelRouter:
    def __init__(self, models_path="models.json", budget_provider=None): ...
    # budget_provider: ()->float 返回剩余预算（元）；None 时不降级
    def resolve_model(self, op: str, budget_remaining: float,
                      force: str = "") -> str: ...
    def endpoint_for(self, model: str) -> "EndpointConfig": ...
    def client_for(self, op: str, budget_remaining: float,
                   force: str = "") -> tuple[LLMClient, str]: ...
    def is_available(self, model: str, ttl_seconds: int = 60) -> bool: ...
    def health_check(self, endpoint_id: str) -> bool: ...
```

决策顺序（纯函数，测试友好）：
1. `force` 非空 → 直接用 force（跳过降级），如 `agent_loop` 会话临时切 flash。
2. `base = OP_MODEL_DEFAULT.get(op, "deepseek-v4-flash")`。
3. **可用性裁剪**：`base` 不可用 → 沿 `fallback_chain[base]` 取第一个可用。
4. **预算降级档**（阈值取 `budget_remaining` 对**全局预算**的占比；全局预算取自 `api.json`/`models.json` 或 CostTracker.budget，阈值待确认 #2）：
   - `remaining >= 50%`：用第 3 步结果。
   - `20% <= remaining < 50%`：若结果为 reasoner → 降 flash（写作类本来就是 flash，不动）。
   - `5% <= remaining < 20%`：flash → 本地 qwen（若可用）；reasoner 已降 flash，flash 再降 qwen。
   - `remaining < 5%`：一律 qwen；qwen 不可用 → 返回原模型但记 warning（engine 的 `spent>=budget` 兜底会 PAUSE，此处不重复拦）。
5. **本地 qwen 启用条件**：`availability.qwen3.6-35b.enabled == True` 且 `health_check` 通过（启动时测一次 + 缓存 60s）。未满足则跳过 qwen，保持 flash。
6. 兜底：全链不可用 → 返回 `base`（保持原模型），由上层捕获异常落 error.jsonl（§6），不让路由层抛断整链。

**模型不可用兜底**：`client_for` 返回的 LLMClient 调用失败（非致命错误重试 3 次仍失败）时，上层捕获后带 `force=chain_next` 重试一次；再失败才 Fail-Fast。

### 5.4 与 llm_client 对接（设计）

- 调用点写法统一为：

```python
client, model = router.client_for("write_group", engine.cost_tracker.remaining())
text = client.call(system, user, model=model, op="write_group")
```

- `ctx.get_llm()` 保持原签名（主端点单例，向后兼容所有未接路由的旧调用点）；新增 `ctx.get_router()` / `ctx.get_llm_for_op(op)`。
- `LLMClient` 实例按端点缓存（`router._client_cache[ep.id]`），`model=` 只在同一端点内换名。

---

## 六、P1 Fail-Fast 契约

> 状态：重写 v0.1 §4.3 ｜ 优先级 P1 ｜ 待确认：否

### 6.1 轻量校验器（不引 jsonschema）

```python
# libraries/contracts.py（新增）
@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]

def validate(rule, data) -> ValidationResult:
    """rule 可为 callable(data)->(bool, str) 或 dict 规则（见工厂）。"""

# 原子规则工厂
def is_dict(required: list[str]) -> rule
def str_field(name, min_len=1, max_len=None, required=True) -> rule
def int_field(name, min=None, max=None, required=True) -> rule
def enum_field(name, allowed: set, required=True) -> rule
def list_field(name, item_rule=None, min_len=0, max_len=None, required=True) -> rule
def text_contract(min_chars, max_chars) -> rule          # 正文文本
def no_consecutive_dupe() -> rule                        # 连续重复词
def id_in(ids: set) -> rule                              # 桥段 id 存在性
def compose(*rules) -> rule                              # 顺序合并，返回首个错误
```

返回格式：`ValidationResult(ok, errors=[f"{name}: {msg}", ...])`；`errors` 用于拼接重试提示与落错误日志。

### 6.2 校验对象清单（具体规则表）

| 对象 | 规则（字段 / 取值范围） |
|---|---|
| outline analyze JSON | `protagonist`(dict，`name` 1-20 字、`identity/personality/background/golden_finger` 字符串可空、`age` int 0-999、`death_year` int 0-9999)；`world_building`(dict 可空)。 |
| outline plan JSON | `outlines` list 1..`max_outlines`；每项 `template_id ∈ 候选 id`、`name` ≥1 字、`start_chapter` int ≥1、`end_chapter` int ≥ `start_chapter`、`transition_type ∈ {sequential,overlap,merge}`、`reason` ≥1 字。 |
| outline plot JSON | `plot_ids` list 1..3 且 `⊆ 候选 id`（`id_in`）、`reason` 字符串。 |
| outline threads JSON | `threads` list（`{id,name,desc}`，`name` ≥1）、`assignments` list（`{plot_id ⊆ 桥段id, thread, seq int≥1}`）、`splits` int≥0、`reason`。 |
| outline theme_review JSON | `corrections` list（`{plot_id ⊆ 桥段id, theme_hints list, reason}`）、`summary` 字符串。 |
| outline validate JSON | `issues` list（元素为字符串）、`summary` 字符串。 |
| write_group 正文 | 文本契约：非空、3-5 句（按 `count_prose_units` 与换行）、150-250 汉字、`no_consecutive_dupe`。 |
| gag detect JSON | `has_opportunity` bool；true 时 `gag_ids` 非空且 `⊆ pool`（池内 id）、`deploy_hint` ≤120 字、`reason` ≤120 字。 |
| outline_agent 意图 JSON | `intent ∈ {modify_plot,add_gag,remove_gag,add_plot,remove_plot,modify_outline,general}`；非 general 时按意图校验必备字段（如 `modify_plot` 需 `target_index` 或 `target_name`；`add_plot` 需 `new_plot.name` 或 `outline_index`）；`reply` 字符串。 |

### 6.3 重试与分层截断（设计）

- **重试次数 N=2**。每次失败后：把 `errors` 拼进提示词末尾（`【上次输出不符合格式】` + errors + `请按规范重新输出。`），重试；重试不计入遥测的 retries（retries 指 HTTP 层重发）。
- **仍失败后的「即时截断」动作**——分层，不全部抛错：

| 场景 | 截断动作 |
|---|---|
| outline 各阶段 | 走模块既有规则兜底（`_rule_sequence`/`candidates[:2]`/`_default_basic_info`），落错误日志，不打断整链 |
| write_group | 空/重复词 → 现有重试通道（`WRITER_EMPTY_RETRIES=2` 保留）；仍失败 → 该桥段 `bridge_skip`（保留待下章重试），与现状一致 |
| gag detect | 视为未命中（静默，现有行为） |
| outline_agent 意图 | 降级 `intent="general"` + 帮助文本（现有行为） |
| outline validate LLM | 跳过 LLM 抽查，保留规则校验结果 |
| 顶层不可恢复 | `raise ContractError` 交给 engine 上层，任务 `fail()`，落 error.jsonl |

### 6.4 错误日志 schema `books/<id>/errors.jsonl`（设计）

```json
{"ts": "2026-08-19T12:00:00.123", "book_id": "book_001", "op": "outline_analyze",
 "model": "deepseek-reasoner", "stage": "fallback",   // retry|fallback|abort|raise
 "errors": ["protagonist.name: 空"], "retries": 2,
 "attempts": [{"raw_len": 320, "err": "protagonist.name: 空"}],
 "raw_snippet": "{\n  \"protagonist\": {...",   // 前 200 字符
 "trace": "..."}
```

写入复用 `append_jsonl`（§4.4），追加式。

### 6.5 与现有兜底的关系（明确边界）

- **契约层 = 统一失败入口**；各模块兜底函数保留，但由契约层在「重试耗尽」后调度调用。即：LLM 输出 → `contracts.validate` → 失败重试(2) → 仍失败 → 调模块既有兜底 + 落 error.jsonl。
- **不重复实现**：writer 的空响应重试（针对空内容）与契约层文本校验（字/句数/重复）正交，两者并存：先 writer 空检查，再契约文本校验。
- 旧兜底不删除、不改内部逻辑，只改「何时被调用」。

---

## 七、P1 上下文预算自动降级

> 状态：重写 v0.1 §4.4 ｜ 优先级 P1 ｜ 待确认：#2（阈值）

### 7.1 预算来源与估算

- 来源：`api.json.context_budget_tokens`（默认 300000，设置页可改）。
- 估算：复用 `cost_tracker.estimate_tokens_chinese`（约 0.6 token/汉字），乘安全系数 1.3：

```python
def estimate_input_tokens(*texts: str) -> int:
    return int(sum(estimate_tokens_chinese(t or "") for t in texts) * 1.3)
```

- 触发阈值：`ratio = 估算输入 / context_budget_tokens`；`ratio ≥ 0.70` 警告（仅记录）；`ratio ≥ 0.85` 进入降级阶梯。

### 7.2 降级阶梯 L0-L3（设计）

| 档 | bible | 摘要窗口 | 其他裁剪 |
|---|---|---|---|
| L0 全量 | `build_book_bible(max_chars=1200)` | 最近 5 章（现状 limit=5） | 无 |
| L1 | `build_book_bible_condensed(max_chars=600)`（等价现状写作档） | 3 章 | 省略低权重 section（见 7.3） |
| L2 | 核心三件：主角+世界观+风格（`max_chars≈440`） | 2 章 | 上章结尾只取末 100 字、`render_bridge_prompt` 的 `summaries_block` 截到 400 字 |
| L3 | 仅主角+世界观摘要 200 字 | 1 章 | 跳过角色状态块（`roles_block`）、跳过承诺台账块 |

### 7.3 section 权重排序表（先砍的排前面）

依据 `prompt_harness._bible_sections(condensed)` 现有裁剪顺序与语义重要性（待确认 #2 数值）：

| 优先级 | section | 对应方法 |
|---|---|---|
| 最低（先砍） | 基调 | `_tone_bullets` |
| | 内涵 | `_theme_bullets` |
| | 配角 | `_supporting_cast_bullets` |
| | 时间纪律 | `_rebirth_time_bullets`（仅重生文，可空则自然省） |
| | 时代语言 | `_era_language_bullets` |
| | 视角 | `_pov_bullets` |
| | 风格 | `_style_bullets` |
| | 世界观 | `_world_bullets` |
| 最高（最后砍） | 主角 | `_protagonist_bullets` |

注意：`_bible_sections(condensed=True)` 已砍配角+基调，即 L1 与现状 condensed 一致；L2/L3 用新增参数 `keep_keys` 过滤（见 7.4）。

### 7.4 PromptHarness 改造签名（设计）

```python
class PromptHarness:
    def build_book_bible(self, max_chars: int = 1200,
                         keep_keys: tuple = None, level: int = 0) -> str
    def build_book_bible_condensed(self, max_chars: int = 600,
                                   keep_keys: tuple = None, level: int = 0) -> str
    def render_bridge_prompt(self, item, chapter_buffer, prev_ending, bridge_text,
                             budget_remaining: int, character_states="",
                             summaries_context="", inspiration_hint="",
                             is_opening=False, review_hint="", chapter_num=0,
                             budget_level: int = 0, summary_max_chars: int = 600) -> str
    # budget_level 控制 7.2 的 L1/L2/L3 裁剪；summary_max_chars 控制摘要块上限
```

`keep_keys` 取值如 `("主角","世界观","风格")`；`_join_sections` 增加按键过滤。

### 7.5 注入点与边界（设计）

- **注入点**：
  - `engine._prepare_chapter_context`：估算 `prev_ending + char_states + buffer + summaries_context` 输入，选档，把 `budget_level` 传给 `render_bridge_prompt`；`_summarize_chapter` 的 `limit` 改为 `{5,3,2,1}[level]`。
  - `outline_generator`：`render_outline_context` 在 `ratio≥0.85` 时把全量改为 500 字 condensed（`select_plots/thread_split` 已用 500）。
  - `agent_loop`：调用前估算 `conv` 序列化长度；`ratio≥0.70` 从保留最近 20 条缩为 10 条；`_compact` 的 tool 结果上限 3000 降为 1500。
- **估算不准**：估算带安全系数 1.3；真实 usage 由遥测回灌，`TelemetryTracker.avg_prompt_tokens(op)` 用于下一轮校准（读上一本书同类 op 均值）。
- **质量影响回显**：`TelemetryRecord.degraded_level` 记录实际降级档；`GET /api/budget?book_id=` 返回 `{estimated_tokens, budget_tokens, ratio, level, degraded}`；前端写作台显示「已降级 L1」角标（P2 可选 UI）。

---

## 八、P2 插件契约制度化

> 状态：重写 v0.1 §4.5 ｜ 优先级 P2 ｜ 待确认：否

### 8.1 Plugin 契约定义（类式，兼容现有 `BasePlugin` 风格）

```python
# plugins/base.py（新增）
@dataclass
class PluginManifest:
    name: str
    version: str = "0.1.0"
    description: str = ""
    requires: list[str] = field(default_factory=list)   # 依赖的插件/能力名
    surface: str = "runtime"                            # runtime | ui | both

class Plugin:
    manifest: PluginManifest
    def register_tools(self, registry: list[dict]) -> None:
        """向 TOOL_REGISTRY 追加工具条目：{name, description, input_schema, func,
        source:"plugin", surface, plugin:<name>}。"""
    def register_hooks(self, engine: "NovelEngine") -> None: ...
    def register_prompt_sections(self, harness: "PromptHarness") -> None:
        """注册附加 bible section（返回 list[(title, text_fn)]）或 skill 资产路径。"""
    def get_ui(self) -> dict | None:
        """返回 {"panel_id": str, "static_dir": str} 或 None（纯运行时插件返回 None）。"""
    def on_load(self, ctx) -> None: ...
    def on_unload(self, ctx) -> None: ...
```

函数式 vs 类式：**采用类式**（与 `plugins/__init__.py` 现有 `BasePlugin` 一致，一个基类即可）；允许「简单插件」= 模块仅含 `PLUGIN = Plugin()`。

### 8.2 发现机制（设计）

- 约定：`plugins/*_plugin.py` 中模块级 `PLUGIN`（Plugin 实例）或 `plugin()` 工厂为入口。
- `discover_plugins(path="plugins") -> list[Plugin]`：`glob` 匹配 → 逐模块 import → 收集 `PLUGIN` → 按 `manifest.name` 排序。
- manifest 放插件模块内（`PLUGIN_MANIFEST` dict 或 `plugin.manifest`），**不引入 package.json/独立元数据文件**（避免新依赖）。
- 加载时序：应用启动（`web_ui.py`）时先 `discover`，再逐个 `register_tools` → `register_hooks` → `register_prompt_sections` → `get_ui` 挂载静态目录。

### 8.3 Hook 点清单（engine Op 生命周期 + SSE 事件总线）

```python
# NovelEngine 增加
def subscribe(self, event: str, cb) -> None
HOOK_EVENTS = ("before_op", "after_op", "on_error")
# 载荷
before_op: {"op": Op, "book_id": str, "phase": str, "inst": Instruction}
after_op:  {"op": Op, "result": dict}
on_error:  {"op": Op, "error": str, "traceback": str}
```

- **SSE 事件总线**（参照 Novel-Claude）：轻量发布/订阅 `EventBus`（模块级单例），`engine`/`agent_loop` 产出的 `{type, ...}` 事件广播给插件监听（只读，不能改事件流）。插件 `register_hooks` 订阅事件名：`chapter_done / bridge_done / tool_result / budget_paused / complete`。
- 现有 `agent.py` 的 SSE 发送路径在 `emit` 内额外调 `event_bus.publish(evt)` 即可，不改协议。

### 8.4 现有插件适配

| 现有模块 | 适配方式 |
|---|---|
| `fanqie_scout.py`（FanqieCrawler 能力） | 新增 `plugins/fanqie_plugin.py`：manifest name="fanqie"，`register_tools` 注册 `scout_novel`/`import_materials`，`register_hooks` 订阅主编心跳做侦察 |
| `font_decoder.py` | 纯库保留；`fanqie_plugin` 内部调用其解码，无需自建插件 |
| `style_analyzer.py` | 纯库保留；可选 `style_plugin.py` 注册 `analyze_style` 工具 |
| `plugins/__init__.py` 的 `BasePlugin/PLUGIN_REGISTRY` | 保留作「采集能力基类」；`Plugin` 是更上层契约，二者不冲突（Plugin 内部可组合 BasePlugin） |

### 8.5 双 Surface 隔离

- **运行时面**：`register_tools/hooks/prompt_sections` 影响执行链路。
- **UI 面**：插件 `get_ui()` 声明静态目录；Flask 挂 `/static/plugins/<name>/`；`base.html` 预留 `<div data-plugin-slot="<name>">` 槽；前端脚本独立加载。UI 槽只渲染，业务一律走已注册的 API/tool。插件拿不到 Flask app 实例，杜绝插件改运行时。
- 加载/卸载：`on_unload` 移除工具与 hooks（tools 从注册表按 `plugin` 字段过滤移除）。

### 8.6 与 TOOL_REGISTRY 关系

- `TOOL_REGISTRY = _build_registry()`（现有 29 个内建）→ 改为 `BUILTIN_TOOLS + plugin_tools` 两段。
- 内建条目维持现状（无 `source` 字段，默认 `"source":"builtin"`）；插件条目带 `"source":"plugin"`、`"surface":"runtime"`、`"plugin":"<name>"`。
- `_build_registry()` 启动时做**去重 Fail-Fast**：同名同 surface 出现两次 → 启动报错（§11.1 冲突规避）。

---

## 九、P2 主编 Agent（主动层）

> 状态：重写 v0.1 §4.6 ｜ 优先级 P2 ｜ 待确认：否

### 9.1 空闲检测（服务端心跳）

- 新模块 `plugins/editor_in_chief.py`，在 Web 进程内启动（`web_ui.py` 初始化时起一个 daemon 线程：`while True: sleep(check_interval); tick()`）。
- 「空闲」定义：`无运行中任务（task_manager 无 running）` 且 `距最后用户活动 > idle_after_seconds` 且 `主编总开关开启` 且 `存在至少一本允许主编的书`。
- 用户活动：ctx 增加 `last_activity_ts`，任意写类 API（在 `web_blueprints` 各 POST 端点打点，或加轻量装饰器）更新。
- 配置项（`config.json`，新增）：`{"editor_in_chief": {"enabled": false, "check_interval_seconds": 300, "idle_after_seconds": 600, "tasks": {...}}}`。

### 9.2 任务类型优先级表

| 优先级 | 任务 | 触发条件 | 调度频率 | 互斥键 |
|---|---|---|---|---|
| P1 | 审校已写章节 | 该书 `current_chapter≥1` 且最近章节无 `review` 记录，且距上次审校 >1h | 每心跳每书 ≤1 章 | `ensure_single("editor_review_<book_id>")` |
| P2 | 番茄侦察补库 | 距上次库内新增素材 >12h（存 `storage/fanqie_cache/.last_sync`） | 每 12h | `ensure_single("editor_scout")` |
| P3 | 批量续写队列 | 用户把书加入队列（`POST /api/editor/enqueue`），该书无进行中写任务 | 队列 FIFO、串行 | `ensure_single("editor_write_<book_id>")` |

- 所有任务走 `task_manager.start(..., agent="editor_in_chief", book_id=...)`，复用进度/取消/日志。
- P1 审校：复用 `ContentReviewer.review`（规则层，零成本）→ 结果写回 `books/<id>/chapters/<n>.json` 的 `review` 字段（与引擎写章一致），不写正文。

### 9.3 与 task_manager 复用 + 双进程互斥

- 入队：直接调 task_manager API；单工具互斥用 `ensure_single`。
- **双进程同书互斥**（主编在 Web 进程，与 MCP 进程可能撞书）：统一用 `books/<id>/.lock` 文件锁（§11.2 设计）。主编每个任务 `acquire_book_lock(book_id, timeout=30)`，拿不到则跳过该书本轮。

### 9.4 用户控制

- 全局开关：`config.json.editor_in_chief.enabled`。
- 每书 opt-out：`books/<id>/book.json` 加 `"editor_in_chief_enabled": false`。
- 优先级/频率调整：`config.json.editor_in_chief.tasks = {"review_chapter": {"interval_hours": 1}, "scout": {"interval_hours": 12}}`。

---

## 十、会话记忆 v2（只设计，本轮不实现）

> 状态：新增 ｜ 优先级 P2 ｜ 待确认：#6（默认值）

### 10.1 存储 schema `storage/sessions/<id>.json`

```json
{
  "id": "ses_20260819_a1b2c3",
  "created_at": "2026-08-19T12:00:00Z",
  "updated_at": "2026-08-19T12:05:00Z",
  "summary": "",
  "message_count": 8,
  "messages": [
    {"role": "user", "content": "...", "ts": "..."},
    {"role": "assistant", "content": "...", "ts": "..."}
  ],
  "meta": {"book_id": "book_001", "last_intent": ""}
}
```

- 只存 `user/assistant`（工具步骤卡走现有 tool-log 环形缓冲，不进 session，保持 v1 语义）。
- 消息上限 `max_messages_per_session=100`，超出丢最旧（LRU 于会话内）。

### 10.2 API（设计）

- `POST /api/agent/chat` body 增加可选 `session_id`：
  - 无 `session_id` → 现有无状态模式（兼容）。
  - 有 `session_id` 且服务端有记录 → 服务端用存储消息 + 本次 user 消息作 `messages`；运行 `run_agent_loop` 后追加 `user`+`assistant` 回 session 并落盘；SSE 首事件发 `{"type":"session","session_id":"...","created":false}`。
  - 有 `session_id` 但无记录 → 若 body 同时带 `messages`（sessionStorage 迁移兜底）则以传入 messages 建文件（`created:true`）；否则建空新会话。
  - 有 `session_id` + 也带 `messages` → **服务端存储优先**；服务端为空才用传入（迁移路径）。
- `GET /api/agent/sessions?limit=50` → `[{id, updated_at, message_count, preview}]` 按 updated_at 倒序。
- `GET /api/agent/sessions/<id>` → 完整 session（前端切会话渲染）。
- `DELETE /api/agent/sessions/<id>` → 删除文件。

### 10.3 过期策略（设计）

- LRU：按 `updated_at`；总数上限 `max_count=200`，超限在下次写入时删最旧。
- TTL：`ttl_days=7`，启动时 + 每日清扫线程删过期文件。
- 配置：`config.json.sessions = {"max_count": 200, "ttl_days": 7, "max_messages_per_session": 100}`。

### 10.4 从 sessionStorage 迁移路径

- `agent_panel.js`：新增 `ne_agent_session_id`（localStorage 持久）。首次发送若本无 session_id 但 `ne_agent_history` 有历史 → body 带 `messages: history`（不传 session_id）；收到 `session` 事件后存 `ne_agent_session_id` 并清 `ne_agent_history`。
- 兼容：历史优先本地 sessionStorage（v1）直到服务端 session 建立，之后以服务端为准。

### 10.5 多会话切换与前端适配点

- `agent_panel.js`：`current_session_id` 存 localStorage；发送时 body 带 `session_id`。
- 切会话：`GET /api/agent/sessions/<id>` → 渲染消息；clearBtn 调 `DELETE` 并清本地。
- 侧栏加会话下拉（可选，P2 内不含 UI 样式细节）。

---

## 十一、其余开放待办

### 11.1 MCP 与 Web 工具名冲突规避（设计，方案 A 推荐）

- **方案 A（推荐）**：`_build_registry()` 条目加 `surface` 字段（`"web"|"mcp"|"both"`，默认 `"both"` 向后兼容）。
  - `mcp_server.py`：只注册 `surface ∈ {mcp, both}`。
  - `plugins/agent_loop.py._tools_schema()`：只加载 `surface ∈ {web, both}`。
  - 冲突规避靠 surface 分离，**不改名**（保护既有 Claude Code 记忆）。
- **可选扩展**：插件条目可带 `mcp_name` 字段，MCP 端改名展示（不改变函数名）。
- 启动去重 Fail-Fast：同名同 surface 重复 → `_build_registry` 抛错。
- 特殊标记工具 `navigate`/`canvas_command` 标记为 `surface="web"`（MCP 客户端无页面跳转语义）。

### 11.2 双进程同书互斥：`books/<id>/.lock` 文件锁（设计）

```python
# libraries/book_lock.py（新增）
class BookLock:
    def __init__(self, book_id): self.path = f"books/{book_id}/.lock"
    def acquire(self, timeout: float = 30.0, purpose: str = "write") -> bool
    def release(self) -> None
```

- 实现：进程内 `threading.Lock`（每 book 一个，dict 缓存）+ 进程间文件锁（Windows `msvcrt.locking` 锁首字节 / POSIX `fcntl.flock`）。
- 锁文件内容写 `{"pid": ..., "ts": ..., "purpose": ...}`；**不删除锁文件**（避免 TOCTOU 删除竞态）。
- **检查点**：
  - `agent_tools` 所有写工具入口：`write_next_bridge / write_chapter / generate_full_outline / generate_world / world_candidates / confirm_world / outline_agent / fill_plots / fill_gags / generate_book_meta`。
  - `task_manager.start()` 内（任务级），`done/fail/cancel` 释放。
  - `mcp_server.py` 工具调用外层 wrapper acquire。
  - 主编 agent 每个任务。
- 超时：`acquire` 返回 False → 抛 `BookBusyError("另一进程正在操作这本书，请稍后再试")`，落 error.jsonl，任务 `fail()`。
- 读操作不加锁；写文件继续用 `write_json_atomic`（已存在，防半写）。

### 11.3 skill 目录 git 管理（见 §3.4.3，待确认 #4）

- 建议：`skills/` 进 git（文本资产可 diff/回滚/评审），不随 `books/` 落盘；书级覆盖用 `books/<id>/skills_override/<name>.md`，harness 优先读覆盖（P2 可选）。

### 11.4 追读诊断 / 爽点标注（建议立项，P2 最小范围）

> 待确认 #5。参照网文工坊，但只做规则层、LLM 抽查可选。

- **追读诊断 `libraries/retention.py`**：输入 = 最近 N 章正文 + 章摘要；输出 `{chapter_level: [{chapter, hook_strength 0-10, drop_risk 0-10, reason}], suggestions: []}`。规则：章末 200 字钩子信号（复用 `reviewer.check_cliffhanger`）、前 3 章信息密度、对话比。可挂主编 P1 之后。
- **爽点标注 `libraries/tag_generator.py`**：输入 = 单章正文；输出 `{tags: [{start, end, tag, line_text}]}`，`tag ∈ {打脸, 升级, 伏笔回收, 装逼, 甜宠, 反转}`。规则层关键词/句式匹配 + 可选 reasoner 二次确认。落盘 `books/<id>/tags.json`。
- 两者均依赖已写正文，挂主编心跳或写作完成回调。

---

## 十二、优先级与落地顺序

| 批次 | 内容 | 依赖 |
|---|---|---|
| P0-a | §2.4 llm_client 改造（model 参数 + usage 回调 + stream_options） | 无 |
| P0-b | §4 TelemetryTracker + §5 ModelRouter | P0-a |
| P1 | §6 contracts + §7 预算降级 + §3 skill 资产化 | P0-a/b |
| P2 | §8 插件契约 + §9 主编 + §10 会话 v2 + §11 其余 | P0/P1 |

里程碑说明：
- **P0-a** 是纯底层改动（`core/llm_client.py`），不改变任何外部行为，可独立测试（`test_connection` 仍工作、`call_tools` 仍返回 message）。
- **P0-b** 落地后 `GET /api/telemetry` 即可看到每本书真实成本与缓存命中率——这是「用数据说话」的起点（bible 常驻 vs condensed 之争有据可查）。
- **P1** 建立在 P0 之上：契约校验需要真实 usage 校准、预算降级需要实测成本反哺。
- **P2** 是架构级能力（插件生态 / 主动层 / 会话持久化），独立成批，不阻塞 P0/P1。

---

## 十三、待人工确认清单

1. **Op→模型默认表**（§5.2）：`outline_plot` 是否降 flash；`world_struct` 是否 reasoner（成本 vs 质量）。
2. **降级阈值**（§5.3/§7.1）：预算 50%/20%/5% 三档；上下文 70%/85% 两档（当前为建议值）。
3. **遥测存储选全局按日**（§4.4 推荐 B）——若倾向按书，需改为锁面更大的方案 A。
4. **skill 目录 git 管理**（§3.4.3/§11.3，建议：是）。
5. **追读诊断/爽点标注是否立项**（§11.4，建议：是，最小范围）。
6. **会话记忆 TTL/上限默认值**（§10.3，建议 7 天 / 200 会话 / 100 条）。
7. **`on_usage` 是否覆盖 `test_connection`**（§2.4.5，建议：覆盖但不落存储）。

---

## 附：调研来源

- DSH：CSDN《DeepSeek Harness（DSH）架构拆解，三个值得抄的设计》（2026-08-13 前后）、linux.do npm README 转帖、dsh.so（社区资源站，官方仓库直连需代理）
- OpenClaw：本地文档 `agent-runtime-architecture.md` / `concepts/agent-runtimes.md`
- Skill 生态：GitHub API 检索（2026-08-18），各仓库 README

> v0.2（2026-08-19）：六条设计决策补全为可执行设计（数据结构/接口签名/映射表/边界），新增 §2.4 前置依赖、§十 会话记忆 v2、§十二 落地顺序、§十三 确认清单。
