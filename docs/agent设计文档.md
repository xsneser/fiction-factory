# NovelEngine Agent 设计文档 v0.4

> 版本：v0.4 ｜ 更新：2026-08-20 ｜ 整理：Claude
> 定位：**可视化、外部 agent 可驱动 + 系统内自主 agent 的多阶段 skill 创作平台**——外部 harness（Claude Code / OpenClaw）经 MCP 工具 + navigate 桥驱动整本书创作；**系统内自主 agent**（侧栏交互 + 后台批量）在 Web 进程内跑同样的工具链自主推进全流程；浏览器页面实时可视化每步操作；右侧工具日志记录每次工具调用；每个阶段（建书/大纲/写作/上架）是一个可编排的 skill——**agent 管决策点，确定性管线管批处理**。
> 与总览文档关系：待并入 `设计文档-总览-claude.md`（按项目惯例：子文档 → 并入 → 归档）。
> v0.3（2026-08-19）定位从 v0.2「确定性引擎 + 内部 agent 三层模型」演进为「外部可驱动的 skill 平台」：新增 §〇 目标架构、§一 外部驱动层、§二 Skill 层、§四 信息工具层、§六 落地路线图；原 §2.1-§2.4 与 §3-§11 重组为 §三 批处理层与 §五 基础设施；原 §12/§13 更新为 §六/§七。
> **v0.4（2026-08-20）**：新增「系统内自主 agent」层（§一 1.7 部署方案 + §五 5.9-5.11 落地设计 + §六 路线图 P5-P7），定位补充「外部可驱动 + 系统内自主」双轨；借鉴 deepseek-harness / OpenClaw 开源架构移植模式（事件日志派生会话、工具白名单、写栅栏、护栏），全部 **Python 重写、不引 Node/TS 依赖**。本版仍为设计文档，P5-P7 实现留待立项。
>
> **全局约定**：不引入 jsonschema / fastapi 等重依赖；落盘文件均 UTF-8；所有「待人工确认」项集中在 §七，全文 `待确认` 标记与之一一对应。

---

## 〇、定位与目标架构

### 0.1 一句话定位

**NovelEngine = 可视化、外部 agent 可驱动 + 系统内自主 agent 的多阶段 skill 创作平台。**

它的工作方式：外部 agent（Claude Code / OpenClaw 等 harness）先运行 `launch.bat` 启动项目 → 读取用户需求 → 通过暴露的 MCP 工具 + navigate 桥，一步步驱动浏览器走完建书→大纲→写作→上架每个阶段；**系统内自主 agent**（§一 1.7）在 Web 进程内跑同样的 40 工具注册表，支持侧栏交互（人在环）与后台批量产书（无人值守）两种形态自主推进全流程。每一步操作在浏览器页面实时可视化，右侧工具日志记录每一次工具调用。每个阶段是一个独立的 skill——agent 在决策点（选什么素材 / 要不要重写）上思考并决定，确定性管线在批处理点批量执行。

### 0.2 架构总览（四层 + 系统内自主层）

```
┌─ 外部驱动层（P0，已落地）───────────────────────┐
│  launch.bat 入口                                │
│  MCP 工具面（40 工具，MCP 面 37，surface 分离）   │
│  navigate 外部驱动桥（意图队列 → 浏览器轮询）      │
│  外部工具日志打通（mcp 调用进 tool-log）           │
│  外部 harness 接入（Claude Code / OpenClaw ACP） │
├─ 系统内自主 Agent 层（v0.4 新增，设计未实现）──────┤
│  AgentLoop（现有单轮工具循环）                    │
│  AgentRuntime（会话 + 记忆 + 护栏，状态化包装）    │  ← 新增
│  AgentTask（自主任务编排 + 队列/后台/心跳）        │  ← 新增
│  形态：侧栏交互（人在环） + 后台批量产书（无人值守） │
├─ Skill 层：四阶段（每阶段 = 决策点 + 批处理）──────┤
│  建书 skill：5步向导（已具备）+ 外部驱动           │
│  大纲 skill：agent 选材决策点 + 6阶段批处理        │  ← 选材决策点新增
│  写作 skill：分角色扩充 + 有界自评 + 桥段批处理     │  ← 新增
│  上架 skill：publisher 规则 + agent 参与          │
├─ 批处理层（保留内核，不重写）────────────────────┤
│  outline_generator 6阶段 / storyline_writer       │
│  reviewer / de_ai / publisher                   │
├─ 信息工具层 ────────────────────────────────────┤
│  get_book_state / get_storyline / borrow_preview │
│  + 书详情/库查询 细粒度工具                       │  ← 新增
└────────────────────────────────────────────────┘
```

### 0.3 设计原则：决策点 + 批处理分离

每个 skill 的内部结构一致：

```
[agent 决策点] → [确定性批处理] → [agent 决策点] → ...
```

- **agent 管「判断密集、低频率」的决策**：选哪套大纲模板、选哪些桥段、这段要不要重写、要不要发布。
- **管线管「高频、可复现」的批量**：6 阶段大纲填充、桥段逐段生成、规则审校。
- 依据：写作铁律（§0.5）+ 开源调研——harness 靠「架构性减少模型往返」防失控（如 Tura 省 83% 往返），而确定性管线本身就是这种批处理。外部 agent 只做决策点，不微编排每个 LLM 调用。

### 0.4 与 v0.2 的关系

| 旧内容 | v0.3 去向 |
|---|---|
| §2.1 现状盘点（工具注册表/agent_loop/MCP/PromptHarness/成本/引擎/任务） | 保留，重组入 §一/§三/§五 |
| §2.2 大纲编撰 vs 文章撰写（分离判断） | 保留，入 §三 3.4 |
| §2.3 三层 Agent 模型（受管/自由/主编） | 重构：受管 → 批处理层，自由 → 外部驱动层（v0.4 起同时承接为系统内自主 agent，§1.7），主编 → §5.7/§5.11 |
| §2.4 llm_client 改造 | 保留，入 §五 5.1 |
| §3 Skill 资产化 | 重构：Skill 理念升级为「可编排的阶段工作流」，见 §二 |
| §4-§10（遥测/路由/契约/降级/插件/主编/会话） | 保留，入 §五 |
| §11（surface 分离/书锁/skill git/追读诊断） | surface+书锁 → §一 1.6；追读诊断 → §四 4.3 可选 |
| §12 优先级 | 重写为 §六 路线图 |
| §13 待确认 | 更新为 §七 |

### 0.5 外部循环控制的立场

外部 agent 的「自由循环防失控」**不自己造**，交给外接 harness 自带护栏（调研实证，2026-08）：
- **硬上限**：`max_turns` / `maxToolCallingRounds` / 子会话 `timeout` / token 与成本预算
- **语义环检测**：工具调用重复/循环/结果停滞的熔断（killcord、loop-guard 类，提示 → 拦截 → 终止）
- **确定性门禁**：工具白名单（OpenClaw `runtimeToolPolicy`，per-spawn 不可变）
- NovelEngine 侧只需：MCP surface 分离 + 书级文件锁（§1.6）+ 写作有界自评（§2.3）

**教训**：没有一个 harness 靠「让 LLM 自判好坏」来防失控——终止机制全在 LLM 外面，由运行时/代理/契约强制。自评只用于「质量」判断，不用于「循环」控制。

> **v0.4 更新**：上述「不自造」适用于**外部** harness（Claude Code/OpenClaw 自带护栏）。系统内自主 agent 运行在平台自己进程里，**护栏必须自建**——按同一原则（终止机制在 LLM 外面）移植 OpenClaw/dsh 的 max_turns / 工具白名单 / 语义环 / 预算熔断到 §5.10，不靠「让模型自判」防失控。

---

## 一、外部驱动层（P0）

### 1.1 入口

- `launch.bat`（Windows）/ `launch.sh`（POSIX）启动 Web 进程（`ui/web_ui.py`，端口 58080）。
- MCP server 独立 stdio 进程（`mcp_server.py`），外部 harness 接入：
  ```bash
  claude mcp add --scope project novel-engine -- python mcp_server.py
  ```

### 1.2 MCP 工具面（现状，已核实）

- `agent_tools.py` 的 `TOOL_REGISTRY`（40 工具，MCP 面 37）是单一来源；`mcp_server.py`（纯适配）逐个 `mcp.tool()` 注册（按 surface 过滤 + `functools.wraps` 落日志），stdio；Web 侧栏 `agent_loop`（`plugins/agent_loop.py`）复用同一注册表。**双端共享同一工具面。**
- 工具分组（`_build_registry` 顺序）：
  - 导航/向导：`navigate` / `canvas_command` / `drive_ui`（`canvas_command`/`create_book`/`delete_book` 为 web-only——建书/删书必须走系统界面，MCP 面 37 不含；`drive_ui` 外部经意图桥驱动建书向导表单）
  - 只读：`list_books` / `get_book_state` / `get_storyline` / `borrow_preview` / `get_book_detail` / `query_structures` / `query_plots` / `query_gags` / `query_profiles` / `query_characters`
  - 建书规划：`create_book` / `save_basic_info` / `generate_title` / `generate_outlines` / `generate_full_outline` / `extend_outline` / `confirm_outlines` / `fill_plots` / `fill_gags` / `outline_agent` / `outline_material_candidates` / `generate_world` / `world_candidates` / `generate_characters` / `confirm_world`
  - 写作：`write_next_bridge` / `write_chapter` / `generate_book_meta`
  - 上架/审查/去AI/质量分析：`publish_check` / `mark_finished` / `publish_book` / `export_book` / `review_text` / `deai_text` / `diagnose_retention` / `tag_punch_points` / `delete_book`（`delete_book` 需 `confirm=True`）
- schema 由 `_func_to_schema`（`inspect.signature` + type hints + docstring）自动生成。
- 进程间协调：Web 进程内工具走 `ui/web_blueprints/ctx.py` 单例（共享内存态）；MCP 独立进程 import 后各自建副本，靠 `books/` JSON 落盘协调。

### 1.3 navigate 外部驱动桥 ★（设计）

**现状缺口**：`navigate` 返回 `{"__navigate__": url}`（`agent_tools.py` 约 729 行），仅内部 `agent_loop` 能消费——它把该标记转成 SSE `navigate` 事件，`agent_panel.js` 的 `handleNavigate` 才翻页。**外部 harness 没有浏览器通道，`navigate` 对外部是哑工具。**

**设计：JSON 意图队列 + 浏览器轮询**

```
外部 agent → MCP navigate(目标页) → 写 storage/nav_intent.json（追加 intent）
                                      {id, ts, url, tab, params}
Web 侧 agent_panel.js 现有 3s 轮询通道（tool-log 轮询）复用/扩展
   → 消费到 intent → handleNavigate / 切标签 → 标记已消费（删除/打勾）
```

- 与内部 agent_loop 的 SSE 路径**并存**：内部走 SSE 直达（现状不动），外部走文件队列。
- 边界：
  - intent 带 `id` 去重（消费后删除该条或标记 `consumed`）
  - TTL：未消费超过 30s 自动过期（外部 agent 已转投它处时不残留脏意图）
  - 写入用 `write_json_atomic`（复用现有原子写，防半写）
  - `tab` 支持切右侧工具日志/侧栏页签

### 1.4 外部工具日志打通 ★（设计）

**现状缺口**：tool-log 是 `plugins/agent_loop.py` 的内存环形缓冲（`_TOOL_LOG`，上限 200，`log_tool_call` 写入，`/api/agent/tool-log` 读取），**只有内部 agent_loop 写入**。外部 MCP 调用不落日志，Web 端看不到外部 agent 在干什么。

**设计**：
- `mcp_server.py` 在每个工具执行前后也调用 `log_tool_call(name, status, summary)`，让外部调用与内部调用进同一个 `/api/agent/tool-log`。
- 日志条目加 `source: "web" | "mcp"` 字段，前端区分渲染（🔧 工具日志页签展示来源）。
- 边界：MCP 进程与 Web 进程内存不共享——若跨进程实时展示，可先落 `storage/tool_log.jsonl`（复用 `core/jsonl.py` 追加式），Web 端 `GET /api/agent/tool-log` 合并读取。**简化方案（建议首版）**：MCP 进程内维护自己的环形缓冲 + 同结构 `/api`（MCP 无 HTTP，改由 Web 进程定时扫 `storage/tool_log.jsonl`）。

### 1.5 外部 harness 接入路径

- **直接（最小闭环验证路径）**：Claude Code 挂 MCP → 用户 prompt 驱动 → Claude Code 调 36 工具（MCP 面）。这是 P1 路线图的验证入口。
- **多层（长期）**：OpenClaw 作 meta-agent（自由循环）→ ACP `sessions_spawn({runtime: "acp"})` 拉起 Claude Code → Claude Code 挂 NovelEngine MCP。OpenClaw 自带 loop 护栏（`maxToolCalls` / `timeout` / `runtimeToolPolicy`），工具白名单可限到写作组，`delete_book` 这类高风险工具不暴露给外部。

### 1.6 外部安全前置（设计，保留 v0.2 §11.1/§11.2）

- **surface 分离**：`_build_registry()` 条目加 `surface` 字段（`"web"|"mcp"|"both"`，默认 `"both"` 向后兼容）。
  - `mcp_server.py` 只注册 `surface ∈ {mcp, both}`；`agent_loop._tools_schema()` 只加载 `surface ∈ {web, both}`。
  - `canvas_command` 标记 `surface="web"`（MCP 无画布语义）；`navigate` 保持 `surface="both"`——内部走 SSE 直达、外部（MCP）经 §1.3 意图桥驱动浏览器（同一函数写意图队列）。
  - 同名同 surface 重复 → `_build_registry` 启动去重 Fail-Fast。
- **书级文件锁 `books/<id>/.lock`**：进程内 `threading.Lock`（每 book 缓存）+ 进程间文件锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`），锁内容写 `{pid, ts, purpose}`，**不删除锁文件**。检查点：`agent_tools` 所有写工具入口 + `task_manager.start()` + `mcp_server.py` 外层 wrapper。超时返回 False → `BookBusyError` 落 error.jsonl，任务 `fail()`。
- **外部 harness 工具白名单**（配合 OpenClaw `runtimeToolPolicy`）：外部会话默认禁 `delete_book`；`confirm_outlines` / `mark_finished` 等确认型工具需白名单显式开启。

### 1.7 系统内自主 Agent 部署 ★（v0.4 新增，设计未实现）

**现状缺口**：`plugins/agent_loop.py`（`run_agent_loop(messages, emit)`）已是最小 function-calling 循环，但无状态（messages 由浏览器 `sessionStorage` 持有，关窗即失）、无会话持久化、无记忆、无护栏（仅 `MAX_ITERS=12` 硬上限）、无自主任务编排、无后台批量。外部驱动（MCP）虽闭环，但平台自身不能脱离 Claude Code 会话自主产书。

**设计：三层可组合架构（全部在 Web 进程内，复用 ctx 单例，不新增进程）**

```
AgentTask(plugins/agent_task.py)          ← 自主任务编排 + 队列/后台/主编心跳
  调用
AgentRuntime(libraries/agent_runtime.py)   ← 会话 + 记忆 + 护栏（状态化包装）
  包装
AgentLoop(plugins/agent_loop.py)          ← 现有单轮 function-calling 循环（主体不动）
```

| 层 | 文件 | 职责 | 改动性质 |
|---|---|---|---|
| AgentLoop | `plugins/agent_loop.py` | 单轮循环 | **增量**：`run_agent_loop(..., tools=None, persist=None)`——tools 供决策子环用工具子集；persist 是会话写入回调，循环内每构造一条 conv 消息调一次。改动 ≤15 行 |
| AgentRuntime | `libraries/agent_runtime.py`（新） | 持有 session_id，组装 system prompt + 会话摘要 + 最近消息 → 跑 loop → 增量持久化；执行前套护栏（ToolPolicy/BudgetGuard），注入 `guard_warn`/`budget_paused` 事件 | 新增 |
| 会话存储 | `libraries/session_store.py`（新） | `storage/sessions/<id>.json` 读写、`derive_messages()` 崩溃恢复、滚动摘要（dsh event-log 模式） | 新增 |
| 护栏 | `libraries/agent_guards.py`（新） | `ToolPolicy`（白名单）/ `LoopGuard`（语义环）/ `BudgetGuard`（预算预检） | 新增 |
| 自主编排 | `libraries/agent_pipeline.py`（新） | 脚本骨架（建书→世界→大纲→写作→上架）+ 决策点子环；phase 校验/断点续跑 | 新增 |
| 任务运行时 | `plugins/agent_task.py`（新） | 任务生命周期（复用 `plugins/task_manager.py`）、后台 worker、`storage/task_queue.json`、主编心跳 drain | 新增 |
| Web 适配 | `ui/web_blueprints/agent.py` | `/api/agent/chat` 增可选 `session_id`；新增 `/api/agent/sessions` CRUD、`/api/agent/tasks` | 修改 + 新增 |
| 前端 | `ui/static/js/agent_panel.js` | 会话 id 存 localStorage、「新会话」按钮、新 SSE 事件分支 | 修改 |
| 工具日志 | `libraries/tool_log.py` | `source` 增 `"task"`，`_append_ext` 对 task/scheduler 也写 JSONL | 修改（2 行） |
| 摘要 prompt | `libraries/prompt_harness.py` | 新增 `render_session_summary_prompt`（仿 `render_summary_prompt`，默认不启用） | 修改 |
| 配置 | `config.json`（项目根，新） | `{agent:{max_iters, wall_timeout, concurrency}, sessions:{ttl_days, max_messages, llm_summary}, editor_in_chief:{enabled, check_interval, idle_after}}` | 新增 |

**复用清单（精确到函数）**：`ctx.get_llm()` / `sse_stream_response()`；`json_store.write_json_atomic`；`safe_paths.ensure_child_path`；`book_lock.BookLock`/`BookBusyError`；`nav_intent.push_nav_intent`；`cost_tracker.CostTracker.load(books/<id>/cost.json).remaining()`；`task_manager.start/progress/log/done/fail/cancel/is_cancelled`；`agent_tools.TOOL_REGISTRY` / `tools_for_surface("web")` / `consume_dict_stream`；`storyline.basic_info_world_done`；`outline_generator.basic_info_is_rich`；`prompt_harness.render_summary_prompt`。

**create_book 双轨决策（待确认 #6）**：护栏本意是「create_book 不进 MCP 面」；系统内 agent 分两档——
- 聊天 agent（人在环）：保留 `create_book`（现状 Web 面即有，用户实时看可中断）。
- 自主任务（无人值守）：直接 `create_book` 但**强校验**（genre/pen_name 非空 + basic_info 带 `world_building.description`，拒绝裸建「(待定)」书）；`delete_book` 一律默认 deny，聊天场景需会话级显式授权。
- drive_ui 向导路径保留给外部 MCP 与可视化聊天，两者并存，**40/37 语义零回归**。

**护栏与编排**：per-session 工具白名单（ToolPolicy）、语义环检测（LoopGuard）、预算预检（BudgetGuard）见 §五 5.10；自主任务骨架 + 决策点子环 + 断点续跑见 §五 5.12；后台批量 + 主编心跳见 §五 5.11。落地路线 P5-P7 见 §六。

---

## 二、Skill 层：四阶段

> Skill = 一个可被 agent 编排的创作阶段，内部固定为「决策点 + 批处理」。生态定义（v0.2 §3）：Skill = 一个目录 + SKILL.md（指令 + 示例 + 可选脚本/引用文件），按需注入模型上下文。本文档的 Skill 是该定义的**升级形态**——不仅是指令资产，还包裹可调用的管线批处理（§三），agent 在决策点介入。

### 2.1 建书 skill

- **已具备**：5 步向导 `GET /books/start`（一句话设定 → AI 候选世界观/借书 → 微调设定 → 建书 → 前三章）。
- **决策点（submit 硬前置）**：世界观候选选择必须经用户确认，未挑候选不得提交。
- **批处理**：建书由系统向导完成（`POST /books/start`，phase=config，向导**不再自动跑 SSE**）；世界观与完整大纲由 agent 经 MCP `generate_world` / `generate_full_outline` 生成（逐步落盘），向导第 4 步内嵌故事线 Gantt（`window.StoryLine`）轮询 `GET /api/storyline/<id>` 实时填充。
- **外部驱动**：外部 agent 用 navigate（§1.3）翻到 `/books/start`，经 `drive_ui`（§1.6）逐面板填表单/点下一步/提交；`create_book`/`delete_book` 为 web-only 护栏（建书走系统向导、删书走书库页手动）；向导状态存 `sessionStorage`（`ne_wizard_state`），外部驱动时以服务端接口为准。

### 2.2 大纲 skill（agent 选材决策点 ★）

**现状**：`outline_generator.py`（1197 行）6 阶段（故事分析 → 故事线规划 → 桥段选择 → 线程与呼应 → 内涵复查 → 一致性验证）是确定性启发式；模板/桥段由管线内部规则选（`_rule_sequence` / `candidates[:2]`），**agent 不参与选材思考**——这是「大纲简陋」的实质：选材是启发式，不是判断。

**设计：注入 agent 选材决策点（不重写管线）**

| 决策点 | 位置 | agent 动作 | 输出 |
|---|---|---|---|
| A：选结构模板 | 阶段 2（故事线规划）前 | 从 `structures` 大纲库候选模板中比选（可多次思考保证稳定） | `template_id` + 理由 |
| B：选桥段 | 阶段 3（桥段选择）前 | 从 `plots` 桥段库 + 大纲匹配候选中选 | `plot_id` 序列 |

- **批处理**：管线按 agent 选定素材继续填充；`select_plots` / `thread_split` 改为**优先使用 agent 选定项**，其余走原规则。
- **兜底**：agent 决策失败 → 原规则兜底（现状行为），不打断整链，落 error.jsonl（§5.4）。
- **复用**：`outline_agent.py`（OutlineAgent，意图解析）可复用其对话/解析能力；候选来源复用 `struct_lib` / `plot_lib` 单例。
- **成本**：每本书 2 个决策点，reasoner 各 1 次调用，量级可控。模型映射见 §五 5.3。

### 2.3 写作 skill（分角色扩充 ★ + 有界自评 ★）

**现状**：`storyline_writer.py`（521 行）桥段驱动逐短句组（每桥段 3-5 短句一组调 LLM，temp 0.7, max 1600），携带前文 + 上章结尾 + 角色状态；无质量自评，审校为纯规则软门禁（`reviewer.py`）。

**设计 A：分角色扩充**
- 每桥段生成前展开「角色态势表」：主角/配角各自动作、去向、内心历程、语气——落盘角色状态，注入 `render_bridge_prompt`。
- 扩充后按角色约束写：每个角色的行动线 / 心声 / 语气在段落内保持一致并互相区分（主角与配角不混腔调）。
- 与现状 `character_states` 的关系：升级其内容维度（现状有状态，无「心路历程/语气」维度）。

**设计 B：有界自评（flash 自检 + 限 1 次重写）**
- 每短句组写完后：flash 自检（0-10 分 + 是否重写 + 一句理由，输入 ≈ 当前组 + 写作铁律摘要，输出 ≤150 字）。
- 低分（<6 或 `has_rewrite=true`）→ 带自检提示重写 1 次 → 再检；仍低 → 原样落盘。
- **成本上限**：每章 ≈ 原 ¥0.005 + 自检 N×≈¥0.002 + 重写 ≤1×≈¥0.005 ≈ **¥0.01~0.02 量级**。
- **待确认 #1**：此项反转 memory「写作质量优化不加 LLM 后处理」纪律——见 §七。

### 2.4 上架 skill

- **已具备**：`publish_check`（纯规则）/ `publish_book` / `mark_finished` / `export_book`（`libraries/publisher.py` 纯规则无 LLM）。
- **决策点**：agent 决定发布/导出；`publish_check` 报告给 agent 判断是否可发。
- **批处理**：`publisher.py` 规则检查与落盘。
- **外部参与**：上架阶段整体暴露为工具（现状已是），agent 可参与。

---

## 三、批处理层（保留内核，不重写）

> **铁律**：以下管线是「确定性流程 + 定点 LLM 调用」，不自由化。它们是 skill 的批处理内核，也是本项目的成本与可复现性来源（¥0.005/章）。

### 3.1 大纲管线 `outline_generator.py`（1197 行）

6 阶段 SSE 流式，每阶段落盘 + 规则兜底：故事分析 → 故事线规划 → 桥段选择 → 线程与呼应 → 内涵复查 → 一致性验证。`render_outline_context`（Prompt A~L 规划组）。Agent 选材决策点见 §2.2。

### 3.2 写作管线 `storyline_writer.py`（521 行）

桥段驱动逐短句组（每桥段 3-5 短句一组调 LLM，temp 0.7, max 1600，空响应重试 2 次），组间空行分段，携带前文 + 上章结尾 + 角色状态。`render_bridge_prompt`（Prompt I）+ `gag_injector.detect`（Prompt J，笑点探测器环）+ reviewer 软门禁 + de_ai。

### 3.3 审校/去AI/发布

- `reviewer.py`：纯规则软门禁（字数 / AI 痕迹 / 节奏 / 对话比 / 断章，`check_cliffhanger` 章末钩子）→ hint 注入下一桥段。
- `de_ai.py`：去 AI 味（规则为主，LLM 层可选）。
- `publisher.py`：上架规则检查（纯规则）。
- `cost_tracker.py`：预算门控（`Op.PAUSE`），`libraries/engine.py` Phase 状态机编排。

### 3.4 大纲 vs 写作分离判断（保留 v0.2 结论）

两条管线彻底分离（独立文件 / 提示词 / 兜底），工具注册表也分规划组 vs 写作组。**不要改成自由循环**——需要判断力的地方（大纲对话编辑、审校、救火）用 agent 形态，即本文档的决策点 / 外部驱动层。

---

## 四、信息工具层

### 4.1 现状

`list_books` / `get_book_state` / `get_storyline` / `borrow_preview` —— agent 可查书状态、故事线、借书预览。

### 4.2 新增细粒度查询 ★（设计）

供外部 agent 选材 / 续写 / 上架决策时获取上下文：

| 工具 | 用途 | 复用 |
|---|---|---|
| `get_book_detail` | 书详情（书名/简介/角色/世界观/进度） | `book_mgr` |
| `query_structures` | 按关键词/标签查大纲库 | `struct_lib` |
| `query_plots` | 查桥段库 | `plot_lib` |
| `query_gags` | 查笑点库 | `gag_lib` |

- 只读、无副作用，`surface="both"`。
- **待确认 #4**：是否全要，还是先最小两枚（`get_book_detail` / `query_plots`）。

### 4.3 追读诊断 / 爽点标注（可选，建议立项，最小范围）

参照网文工坊，但只做规则层、LLM 抽查可选（保留 v0.2 §11.4）：
- `libraries/retention.py`：最近 N 章输入 → 章级钩子强度 / 掉读风险 / 建议；复用 `reviewer.check_cliffhanger`。
- `libraries/tag_generator.py`：单章正文 → 爽点标签（打脸/升级/伏笔回收/装逼/甜宠/反转），规则层 + 可选 reasoner 二次确认，落盘 `books/<id>/tags.json`。
- 挂主编心跳或写作完成回调。

---

## 五、基础设施（保留 v0.2 设计，按新定位调整优先级）

> 以下为 v0.2 已定稿的可执行设计。优先级按新定位调整：外部驱动桥（P1）优先于遥测/路由（不构成 P1 的依赖）；遥测/路由落地后用于**观测外部驱动的真实成本**。

### 5.1 llm_client 改造（P0-a，保留）

`core/llm_client.py` 三方法均硬编码 `self.cfg.model`、不返回真实 usage。改造为**向后兼容**：

```python
@dataclass
class UsageInfo:
    model: str = ""; prompt_tokens: int = 0; completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0; prompt_cache_miss_tokens: int = 0
    total_tokens: int = 0; latency_ms: int = 0; retries: int = 0
    status: str = "ok"; error: str = ""; request_id: str = ""

class LLMClient:
    def __init__(self, api_config, on_usage=None): ...
    def set_on_usage(self, cb) -> None: ...

# 三方法均加默认参数（向后兼容）：
def call(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096,
         model=None, op="") -> str
def stream_deltas(self, system_prompt, user_prompt, temperature=0.7,
                  max_tokens=4096, model=None, op="")  # 仍是生成器
def call_tools(self, messages, tools, temperature=0.2, max_tokens=8192,
               model=None, op="") -> dict  # 仍返回 choices[0].message
```

- 回调契约：`on_usage(UsageInfo)` 同步调用，遥测内部 `try/except` 吞异常，绝不让遥测故障污染 LLM 主链路。
- 流式：body 加 `"stream_options": {"include_usage": True}`，`[DONE]` 时 `finally` 前 emit；客户端断开 `GeneratorExit` → emit `status="aborted"`。

### 5.2 遥测 TelemetryTracker（P0-b → P1，随外部驱动观测落地）

- `libraries/telemetry.py`：`TelemetryRecord`（ts/book_id/operation/model/各 token/latency/retries/status/endpoint/degraded_level）。
- OP_NAMES 规范表（`libraries/ops.py`）：`outline_analyze` ~ `agent_loop_tools` / `test_connection`（不落存储）。
- 采集：`TelemetryTracker.set_op(op)` 线程本地栈（contextvars/threading.local），`on_usage` 回调弹栈顶 op 落盘。
- 存储：**全局按日 JSONL** `storage/telemetry/<YYYY-MM-DD>.jsonl`；`core/jsonl.py` 追加写（进程内 `threading.Lock` + 进程间锁文件 `msvcrt.locking`/`flock`；异常降级为单行 open("a")）。
- 聚合：`GET /api/telemetry?book_id=&op=&days=7&group_by=op|model` → cost/cache_hit_rate/p50/p95/by_op/by_model（手写百分位，不引 numpy）。
- 与 CostTracker 关系：TelemetryTracker 为成本唯一事实源；CostTracker 退化为预算门控（保留 budget/remaining/check_budget，`record()` 改由 on_usage 累加实测成本）。

### 5.3 模型路由 ModelRouter（P0-b → P1）

- 静态 `models.json`（多端点 + `aliases` + `prices` + `availability` + `fallback_chain`），不动 api.json 设置页写入。
- `libraries/model_router.py`：`resolve_model(op, budget_remaining, force)` 决策链：force → OP_MODEL_DEFAULT → 可用性裁剪（fallback_chain）→ 预算三档降级（≥50% 原样 / 20-50% reasoner 降 flash / 5-20% flash 降本地 qwen / <5% 一律 qwen）→ 兜底返回 base。
- **铁律**：写作主体一律 `deepseek-v4-flash`（项目质量基线）；reasoner 只用于规划/审校/意图解析等判断密集处（§2.2 大纲决策点用 reasoner）。
- 调用点统一：`client, model = router.client_for("write_group", cost_tracker.remaining())`；`ctx.get_router()` / `ctx.get_llm_for_op(op)`。

### 5.4 契约 Fail-Fast（P1，保留）

- `libraries/contracts.py`：`validate(rule, data) -> ValidationResult(ok, errors)` + 规则工厂（`is_dict` / `str_field` / `int_field` / `enum_field` / `list_field` / `text_contract` / `no_consecutive_dupe` / `id_in` / `compose`）。
- 校验对象清单：outline 六阶段 JSON、write_group 正文（非空/3-5 句/150-250 字/无连续重复）、gag detect JSON、outline_agent 意图 JSON（intent 枚举 + 按意图必填字段）。
- 重试 N=2（errors 拼进提示词）；仍失败走**分层截断**：outline 各阶段走模块规则兜底、write_group 走现有重试通道/bridge_skip、gag 视为未命中、outline_agent 降 general、顶层不可恢复 `raise ContractError` 任务 fail。
- 错误日志 `books/<id>/errors.jsonl`（append_jsonl）。

### 5.5 上下文预算降级（P1，保留）

- 估算 `estimate_input_tokens(*texts)` = `sum(estimate_tokens_chinese)×1.3`；`ratio≥0.70` 仅记录，`≥0.85` 进降级阶梯。
- L0-L3：bible 1200→600→440→200、摘要窗口 5→3→2→1 章、L2 上章结尾截末 100 字、L3 跳角色状态/承诺台账块。
- `PromptHarness.build_book_bible(max_chars, keep_keys, level)` / `render_bridge_prompt(..., budget_level, summary_max_chars)` 增加裁剪参数。
- 注入点：`engine._prepare_chapter_context` / `_summarize_chapter` / `outline_generator`（ratio≥0.85 改 500 字 condensed）/ `agent_loop`（conv 保留 20→10 条、tool 结果 3000→1500）。

### 5.6 插件契约（P2，保留）

- `plugins/base.py`：`Plugin` + `PluginManifest`（name/version/description/requires/surface），方法 `register_tools` / `register_hooks` / `register_prompt_sections` / `get_ui` / `on_load` / `on_unload`。
- `discover_plugins(path="plugins")`：模块级 `PLUGIN` 约定；启动时 register_tools → hooks → prompt_sections → get_ui。
- Hook 点：`NovelEngine.subscribe(event, cb)`，`HOOK_EVENTS = ("before_op", "after_op", "on_error")`；SSE 事件总线（`chapter_done`/`bridge_done`/`tool_result`/`budget_paused`/`complete`）。
- 双 Surface 隔离：UI 槽只渲染，插件拿不到 Flask app。
- 现有适配：`fanqie_scout.py` → `plugins/fanqie_plugin.py`；`style_analyzer.py` 可选 style_plugin。

### 5.7 主编 Agent（P2，保留——新定位下为「主动层」，优先级不变）

- `plugins/editor_in_chief.py`：Web 进程内 daemon 线程心跳（`sleep(check_interval); tick()`）。
- 空闲定义：无运行任务 + 距最后用户活动 > `idle_after_seconds` + 总开关开 + 存在允许主编的书。
- 任务表：P1 审校已写章节（`ContentReviewer.review` 规则层零成本，写回 `review` 字段）/ P2 番茄侦察补库（>12h）/ P3 批量续写队列（`POST /api/editor/enqueue`）。
- 复用 `task_manager` + 书级文件锁；用户控制走 `config.json.editor_in_chief` + 每书 `book.json.editor_in_chief_enabled`。

### 5.8 会话记忆 v2（P2，只设计不实现）→ 已被 §5.9 会话记忆 v3 替代

- `storage/sessions/<id>.json`：`{id, created_at, updated_at, summary, message_count, messages[], meta}`，只存 user/assistant。
- API：`POST /api/agent/chat` 增可选 `session_id`（服务端存储优先 / sessionStorage 迁移兜底）+ `GET/DELETE /api/agent/sessions`。
- 过期：LRU 上限 200 / TTL 7 天 / 消息上限 100（`config.json.sessions`）。

### 5.9 会话记忆 v3（v0.4 落地设计，实现 P5）

> 替代 §5.8。移植 deepseek-harness 的 **event-sourced 会话日志 + deriveMessages** 模式（`Model-visible means logged`），Python 重写。

- **存储** `storage/sessions/<id>.json`：`{id, created_at, updated_at, message_count, summary, meta:{last_book_id, mode}, events[]}`。
- **只存 model-visible 消息**：`events` 每条约 `{ts, type: user|assistant_text|assistant_toolcalls|tool, content, tool_calls?, tool_call_id?}`。相比现状（浏览器只持 user/assistant 文本），补上 tool 消息后**跨轮模型能看到自己调过哪些工具**——这是长程自主任务的记忆底座。书内容不进会话（`books/<id>/` 是事实源），`meta.last_book_id` 只存轻量指针。
- **崩溃恢复（补 turn/end）**：`session_store.derive_messages()` 返回 `(messages, recovery_note)`——从 events 顺序重建消息；发现末尾 `assistant_toolcalls` 缺对应 `tool` 结果（SSE 中断）时**丢弃该孤儿 tool_calls 消息**，置 `recovery_note="上一次会话在工具调用『<tool>』后中断，结果未确认——建议先 get_book_state 核对再继续"`；下轮把 `recovery_note` 拼进 system prompt。诚实补 turn、不虚构结果、不把未确认副作用当真相。
- **滚动摘要**：MVP 纯截断（只留最近 `config.sessions.max_messages`（默认 40）条 + summary），不额外 LLM 调用（守「不加后处理」纪律）；`config.sessions.llm_summary=true` 时（可选 P7）超上限触发一次 `llm.call` 用 `render_session_summary_prompt`（仿 `render_summary_prompt` 的 80-150 字格式）压缩旧段。成本有界（每会话约 1 次）。
- **过期**：TTL 7 天 / LRU 上限 200（`config.sessions`）。

### 5.10 护栏落地（v0.4，实现 P5）

> 移植 OpenClaw `ToolDescriptor.availability` + dsh `schemas()` 白名单投影 + `max_turns`；`libraries/agent_guards.py`。

- **ToolPolicy（per-session 工具白名单）**：`{allowed:set, deny:set, require_confirm:set, max_iters:int=12, max_calls:int=0(会话累计上限), wall_timeout_s:int=0}`，`deny` 优先。
  - 聊天 agent：`allowed = tools_for_surface("web") − {delete_book}`；`delete_book` 需会话级显式授权一次才放行（现状仅靠 `confirm=True` 参数，自主场景太弱）。
  - 自主任务：`allowed = web_tools − {delete_book, canvas_command}`；`create_book` 放行但强校验（§1.7 待确认 #6）。
- **LoopGuard（语义环检测，纯规则零 LLM）**：工具签名 `(tool, json.dumps(args, sort_keys=True))` 近 5 步内 ≥3 次重复 → `guard_warn`；再犯 → `error` 熔断；连续 ≥5 次工具失败 → 熔断。
- **BudgetGuard（预算预检）**：写类工具（`_LOCKED_TOOLS` 交集）执行前 `CostTracker.load("books/<id>/cost.json")`；`remaining()<=0` → 返回 `tool_result{ok:false}` + `budget_paused` 事件，任务置暂停态。引擎侧 `write_next_bridge` 的 `budget_paused` 已透传，双保险。
- **超时/迭代**：`MAX_ITERS` 改读 `config.agent.max_iters`（默认 12）；决策子环独立小上限（默认 8）；墙钟超时 `wall_timeout_s` 熔断。

### 5.11 主编 Agent 落地（v0.4 最小版，实现 P7）

> 落地 §5.7 的 daemon 心跳，但先只做**队列 drain + 定时批量**两条，P1 审校/番茄补库留 v0.4 之外。

- `plugins/editor_in_chief.py`（Web 进程内 daemon 线程）：`sleep(check_interval); tick()`；tick 内调 `agent_task.drain_queue()` + 可选 `config.agent.schedule` 间隔批量（如每 6h 跑一条 spec）。
- 并发上限 `config.agent.concurrency`（默认 2）；同书书锁互斥（复用 `BookLock`）；`task_manager` 状态机（新增 `pause` 预算态 / `interrupted` 崩溃态）。

### 5.12 自主任务编排 AgentPipeline（v0.4，实现 P6）

> **脚本骨架 + 决策点子环**（非硬编码、非全自驱）。确定性骨架按序调用 `agent_tools` 现有工具，agent 只在选材/发布判断点介入（每次决策 = 受限工具子集跑一次小循环，失败回落规则兜底）。骨架**不接触** `generate_full_outline` 内部 6 阶段、**不接触** `storyline_writer` 逐桥段 LLM——完全符合「管线不重写、agent 只做决策点」。

- **骨架伪码** `AgentPipeline.run(spec)`（`spec={genre, sub_genre, pen_name, title?, idea?, tags?, target_chapters}`）：
  ```
  phases = [create → world → outline → write → meta → publish]
  for phase_name, step in phases:
      _assert_phase(book_id, phase_name)   # get_book_detail().phase 校验，不符则纠正/跳过
      step()
  ```
- `_world_phase`：`world_candidates` → 决策子环选候选 → `generate_world` → `confirm_world`；`_outline_phase`：`outline_material_candidates` → 决策子环选模板/桥段（picks）→ `generate_full_outline`。
- `_write_phase`：**while 循环**逐桥段调 `write_next_bridge`（内部已有有界自评），累计 `book.current_chapter` 达标即停；每 N 次检查 `task_manager.is_cancelled` 与 `budget_paused`。**不指望单次 agent 调用写三章**。
- **断点续跑 = 书自身是检查点**：`storyline.json.phase` + `book.json.current_chapter` 即事实源；崩溃后扫描 `storage/tasks/` 标 interrupted，续跑按 phase 重入。
- **BookBusyError 重试**：写工具 `try/except BookBusyError` 指数退避 3 次（5s 起），仍忙则任务 `fail`（agent 循环内已由 `entry["func"]` 捕获转为 tool_result）。

---

## 六、落地路线图（步骤规划）

| Phase | 内容 | 优先级 | 依赖 | 验证 | 状态 |
|---|---|---|---|---|---|
| P1 | **外部驱动桥**：navigate 意图队列（§1.3）+ 外部工具日志（§1.4）+ MCP surface 分离与书锁（§1.6）→ 跑通「外部 agent 建书→大纲→写作」最小闭环 | P0 | 无 | Claude Code 挂 MCP，prompt「新建一本 X 小说并写前三章」，浏览器跟随可视化 + tool-log 记录外部调用 | ✅ 已落地 |
| P2 | **大纲 skill**：agent 选材决策点（§2.2，选模板/选桥段） | P1 | P1 | 对比有无选材决策点的故事线质量与一致性 | ✅ 已落地 |
| P3 | **写作 skill**：分角色扩充 + 有界自评（§2.3） | P1 | P1 | 对比有无自评的章节审校分 / AI 痕迹 | ✅ 已落地 |
| P4 | **上架 skill + 信息工具补全**（§2.4/§4.2）+ 追读诊断（§4.3 可选） | P2 | P1 | agent 完成发布流程 | ✅ 已落地 |
| P5 | **系统内会话 + 护栏 MVP**（v0.4）：`session_store` / `agent_guards` / `agent_runtime` / `/api/agent/chat?session_id` + sessions CRUD / 前端 localStorage + 新会话按钮 / `config.json` | P1 | P1 | 无 LLM 单测（Phase 12/13：崩溃恢复/白名单/LoopGuard/BudgetGuard）；手动：刷新浏览器对话仍连续 | 📋 设计（§5.9/§5.10） |
| P6 | **自主任务编排**（v0.4）：`agent_pipeline` 骨架 + 决策子环 + `agent_task` 生命周期 + 任务面板 + `source=task` 日志 | P1 | P5 | 无 LLM 单测（Phase 14：rule 模式跑通 create→world→outline→write 3 章、断点续跑、BookBusyError 重试） | 📋 设计（§5.12） |
| P7 | **后台批量/主编心跳**（v0.4）：队列 + `editor_in_chief` drain + 定时批量 + `budget_paused` 任务态 | P2 | P6 | 无 LLM 单测：入队 2 本并发消费、取消、预算耗尽停；`publish_check` 通过 | 📋 设计（§5.11） |

**里程碑**：
- **P1 落地 = 「外部可驱动的 skill 平台」的最小闭环成立**，是本次定位的起点。
- P2/P3 落地 = 「决策点 + 批处理」原则在创作链上兑现。
- P4 落地 = 全链路可被外部 agent 驱动。
- **P5-P7（v0.4）落地 = 平台自身具备系统内自主 agent（侧栏交互 + 后台批量），不依赖外部 harness 会话。** 当前仅设计，未实现。

**实现记录（2026-08-19，dev 分支）**：
- P1：`libraries/book_lock.py`（书级文件锁）、`agent_tools` surface 分离 + `tools_for_surface` + 写工具书锁包装、`libraries/nav_intent.py`（意图队列 + TTL）、`navigate` 写意图 + `tab` 参数、`GET /api/agent/nav-intents`、`libraries/tool_log.py`（web/mcp 合并日志）、`mcp_server` functools.wraps 落 source=mcp 日志。
- P2：`OutlineGenerator.generate(agent_picks=)`——决策点 A 预选模板（`_sequence_from_picks`）+ 选材复查 pass（`_review_outline_sequence`，多次思考保证稳定）；决策点 B 预选桥段（`_arrange_plots_for_outline` 逐阶段校验）；工具 `outline_material_candidates` 暴露候选池。
- P3：`prompt_harness._roles_status_block` 分角色态势表（规则层零成本）；`storyline_writer` flash 自检（`_self_check_group`）+ 限 1 次重写（`_rewrite_group_once`），`SELF_CHECK_ENABLED` 开关。
- P4：工具 `get_book_detail` / `query_structures` / `query_plots` / `query_gags` / `query_profiles`（只读 both）+ `libraries/retention.py` 追读诊断 + `libraries/tag_generator.py` 爽点标注（工具 `diagnose_retention` / `tag_punch_points`）。注册表 29→36 工具。

**外部驱动验收记录（2026-08-19）**：
- **MCP 注册**：`.mcp.json`（project 级），Claude Code 重启会话后自动加载，MCP 面 **35 工具**（`navigate`/`drive_ui` 在列；`create_book`/`delete_book`/`canvas_command` web-only 不在列——建书/删书护栏）。
- **`tools/mcp_smoke.py` 协议验收 17/17 通过**：`initialize` 握手 → `tools/list`（36 工具）→ `tools/call` 真实往返（create_book → save_basic_info → generate_outlines(rule) → confirm_outlines → fill_gags → get_book_detail）→ navigate 写入意图队列 → tool-log `source="mcp"`（14 条）→ delete_book 安全门。零 LLM 成本，跑完清理。
- **过程中修复两处 MCP 面 bug**：
  1. `navigate` 误标 `surface="web"` → 外部 MCP 调不到、意图桥失效 → 改 `surface="both"`（内部 SSE 直达 + 外部意图桥，同一函数写 `nav_intent.json`）。
  2. `_wrap_book_lock` 手写包装只设 `__name__`/`__doc__` → `inspect.signature` 看到 `(**kwargs)` → FastMCP 给 `fill_gags` 等 10 个锁定工具生成错误 schema → 改 `functools.wraps` 保留 `__wrapped__` 签名链。
- **真实 LLM 驱动延后**（用户择机）：`generate_full_outline`（6 阶段）/ `write_next_bridge` 经 MCP 的 LLM 往返，以及 Claude Code 新会话 prompt 驱动（浏览器可视化 + tool-log `source=mcp`）——含 deepseek-v4-flash 成本。

**与 v0.2 批次映射**：v0.2 的 P0-a/P0-b（llm_client / 遥测 / 路由）在 v0.3 中并入 §五，**不阻塞 P1**——P1 外部驱动桥依赖的是现状 40 工具（MCP 面 37），不依赖遥测/路由。P1 落地后再补 §5.1-5.3，用真实 usage 观测外部驱动的成本与缓存命中率。

---

## 七、待人工确认清单

1. **有界自评 vs 成本纪律**：✅ 已按建议落地（`SELF_CHECK_ENABLED=True`，flash 自检 + 限 1 次重写 + 成本有界）；此实现反转 memory「写作质量优化不加 LLM 后处理」，**仍需用户复核是否保留**（如不认可可把常量改 False）。
2. **大纲选材决策点**：✅ 已落地（阶段 2 前选模板 + 阶段 3 前选桥段，`agent_picks` 外部预选 + 选材复查 pass）；模型沿用管线默认 flash（reasoner 可选，未强制）。
3. **navigate 桥实现形态**：✅ 已按建议落地（JSON 意图队列 + 浏览器 2.5s 轮询 + TTL 30s）。
4. **信息工具粒度**：✅ 已全量落地（`get_book_detail` / `query_structures` / `query_plots` / `query_gags` / `query_profiles`，另加 `diagnose_retention` / `tag_punch_points`）。
5. 原 v0.2 §十三 遗留项（保留，按 v0.2 建议值）：
   - Op→模型默认表：`outline_plot` 是否降 flash；`world_struct` 是否 reasoner
   - 降级阈值：预算 50%/20%/5%；上下文 70%/85%
   - 遥测存储选全局按日（推荐 B）
   - skill 目录 git 管理（建议：是）
   - 追读诊断/爽点标注是否立项（建议：是，最小范围）
   - 会话记忆 TTL/上限默认值（7 天 / 200 会话 / 100 条）
   - `on_usage` 是否覆盖 `test_connection`（建议：覆盖但不落存储）
6. **create_book 内部授权 / 外部禁止双轨**（v0.4，§1.7）：系统内自主任务直接 `create_book`（强校验 basic_info，拒绝裸建），drive_ui 向导路径保留给外部 MCP 与可视化聊天。建议：采纳——护栏语义是「不进 MCP 面」，内部 Web 进程 create_book 本就是 sanctioned 路径；40/37 语义零回归。
7. **会话滚动摘要 `llm_summary` 默认关**（v0.4，§5.9）：MVP 纯截断，语义摘要为 config 开关默认关，守「不加后处理」纪律。建议：采纳。
8. **后台并发 `config.agent.concurrency` 默认 2**（v0.4，§5.11）：同书书锁互斥，异书并发上限 2。建议：采纳，可在设置页暴露。
9. **P5-P7 是否立项**（v0.4）：系统内自主 agent 从「设计」转「实现」需立项；建议按 P5(MVP 会话+护栏)→P6(自主任务)→P7(批量/心跳) 分期实施。

---

## 附：调研来源

- DSH：CSDN《DeepSeek Harness（DSH）架构拆解，三个值得抄的设计》（2026-08-13 前后）、linux.do npm README 转帖、dsh.so
- OpenClaw：本地文档 `agent-runtime-architecture.md` / `concepts/agent-runtimes.md`；[ACP agents 文档](https://docs2.openclaw.ai/tools/acp-agents)；[ACP agents setup](https://docs2.openclaw.ai/tools/acp-agents-setup)；[per-spawn tool policies PR #78441](https://github.com/openclaw/openclaw/pull/78441)；[iteration budget PR #97485](https://github.com/openclaw/openclaw/pull/97485)
- 自由循环防失控（2026-08 检索）：[harness-sdk max_turns/max_token_budget](https://github.com/strands-agents/harness-sdk/issues/2124)、[killcord 语义环检测](https://www.npmjs.com/package/killcord)、[Tura 架构性减少模型往返](https://www.aitoolnet.com/tura)
- Skill 生态：GitHub API 检索（2026-08-18），各仓库 README
- **v0.4 系统内自主 agent（2026-08-20 检索）**：
  - deepseek-harness（[github.com/deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，TS/Node，MIT，developer preview）——移植 event-sourced 会话日志 + `deriveMessages`（`packages/core/session`）、工具 `defineTool`/`schemas()` 白名单投影（`packages/core/tools`）、compaction seam（`packages/compaction`）。
  - OpenClaw（[github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)，TS/Node，MIT）——移植 `runEmbeddedAgent` 写栅栏思路（`activeWriterRunId`，与 NovelEngine 书锁同构）、工具 `ToolDescriptor.availability` 门控、上下文 compaction、任务级护栏（max_turns/timeout/tool policy）、记忆分层（书级 chapter summary 已等价实现，会话级用滚动摘要）。
  - 两者均为 TS/Node → **只移植架构模式，Python 重写，不引 Node/TS 依赖**；砍掉消息渠道/语音/多租户/通用 shell/embedding 向量检索/插件 runtime。

> v0.3（2026-08-19）：定位演进为「可视化、外部 agent 可驱动的多阶段 skill 创作平台」。新增 §〇 目标架构、§一 外部驱动层、§二 Skill 层、§四 信息工具层、§六 落地路线图；§三/§五 承接 v0.2 批处理内核与基础设施设计；§七 更新待确认清单（新增有界自评成本纪律反转、选材介入点、navigate 桥形态、信息工具粒度）。
> **v0.4（2026-08-20）：定位补充「系统内自主 agent」双轨。新增 §一 1.7 部署方案（三层可组合架构）、§五 5.9-5.12 落地设计（会话记忆 v3 / 护栏 / 主编心跳最小版 / 自主任务编排）、§六 路线图 P5-P7、§七 待确认 #6-#9；附录补 deepseek-harness / OpenClaw 架构模式借鉴。本版为设计文档，P5-P7 未实现。**
