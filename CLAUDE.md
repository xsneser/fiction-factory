# NovelEngine — Claude Code 工作台

NovelEngine 是「可视化、外部 agent 可驱动的多阶段小说创作平台」。本仓库经 MCP server `novel-engine` 暴露 43 个工具，Claude Code 经 `mcp__novel-engine__*` 驱动整本书创作。创作分四阶段，每阶段一个分 skill，由主 skill `novel-master` 统一调度：

> **当前驱动形态**：侧栏聊天大脑 = **dsh**（内置 agent `plugins/agent_loop.py` 已删除，无 builtin 可切回）。`libraries/dsh_bridge.py` 转发 vendored `vendor/dsh-ne/`（精简核心，改名防冲突）headless 子进程，经 MCP 驱动平台；`vendor/dsh-ne/events-runner.mjs` 把 dsh 的每个工具调用/结果实时推成 SSE（tool_call/tool_result/navigate/ui_command），侧栏实时工具卡、导航零延迟；护栏：phase 门控 `tool_policy.py` / MCP 循环熔断 `loop_guard.py` / 建书 reset。agent 架构见 `docs/架构文档-内置agent-dsh.md`，上手交接见 `docs/交接文档-2026-08-25-建书链路Agent修复.md`（2026-08-25 建书链路修复）与 `docs/交接文档-2026-08-25-小说抓取入库.md`。
> **架构速览**（系统分层/工具注册表/双通道驱动/各阶段入口/常见坑）：`docs/架构总览.md`——交接/上手先读它，不必重新探索。设计权威仍为 `docs/设计文档-总览-claude.md`。
> **故事线概念研究**（大纲/桥段/线程 三者关系、「大纲≠卷」建模、插叙/并行视角现状）：`docs/研究文档-故事线大纲桥段线程.md`（2026-08-25 只读研究，未改代码）。

| 阶段 | 分 skill | 前置 phase | 出口 | 主要工具 |
|---|---|---|---|---|
| 建书 | `novel-build-candidates` + `novel-build` | 无书 / phase=config | `ready`（dsh 侧 skill 已删（2026-08-24，仅 MCP），历史拆分：`novel-build-candidates` 生成候选并**呈现**（`set_candidates`），停在步 2 等用户挑选；用户点「已挑选完毕」后页面自动触发 `novel-build`——步 3「内容构建工作台」分阶段构建（core_conflict→大纲+桥段→势力→人物→其余维度）并随提交落库，**书创建即带大纲 phase=ready**，直接进写作台） | drive_ui（驱动建书向导，含 set_candidates/set_outline）/ query_arc_library / arc_material_candidates / query_plots / query_characters / save_basic_info / confirm_world（旧 world_candidates / generate_core_conflict / generate_outline_preview / generate_factions / generate_characters / generate_rest_world / generate_full_outline / generate_world 工具**已删除**（2026-08-24 大清理，无兜底），步 3 内容全由 agent 自主生成经 set_outline / set_world / set_characters 落表） |
| 大纲 | `novel-outline` | `config` 且 basic_info 充实 | `ready` | **agent 自主生成 → `save_outlines`** / arc_material_candidates / confirm_outlines / fill_gags |
| 写作 | `novel-write` | `ready` | 章节/桥段写完 | **agent 自主生成 → `save_bridge_draft` / `save_chapter_text`** / `save_book_meta` / chapter_quality_gate（完整章节质量门禁）/ review_text / deai_text / diagnose_retention / tag_punch_points（旧 write_next_bridge 等已废弃留档） |
| 上架 | `novel-publish` | 已有第 1 章正文 | `published` / `finished` | publish_check / publish_book / mark_finished / export_book |

## 发现与编排规则

> ⚠️ **2026-08-24**：dsh 侧 skill（`novel-build-candidates` / `novel-build` / `novel-outline` / `novel-write` / `novel-publish`）已全部删除，**仅保留 MCP 工具面**；dsh agent 按本文档四阶段直接调 `mcp__novelengine__*` 工具（skill 重写待后续会话）。**Claude Code 侧 `.claude/skills/` 未改动**。

- **意图 → skill 分发表**：「开新书 / 写设定 / 建书 / 构思世界观 / 生成候选」→ dsh 侧 `novel-build-candidates`（生成候选并**呈现**到步 2，**停在交互点等用户挑选，不自动选/跳步**）；**侧栏要求建书先 `navigate("/books/start")` 翻到步 1 表单**（已给全则预填，笔名留用户选），用户填完点「🚀 让 Agent 构建」后按钮路径接管（只生成候选并呈现，停步 2）；「已选候选 / 补全世界观 / 继续建书」→ `novel-build`（步 3 分步建书 + submit + 完整大纲）；「生成大纲 / 排故事线 / 选桥段 / 一键完整大纲 / 续写 / 扩写」→ `novel-outline`；「写正文 / 写下一章 / 继续写 / 写桥段 / 一键写完整章 / 写完整章 / 一键完整章节 / 完整章节构建」→ `novel-write`（完整章节构建含质量门禁 `chapter_quality_gate`，默认执行、只报告不修复，问题作决策点）；「上架 / 发布 / 完本 / 导出 / 生成书名简介 / 检查能否发书」→ `novel-publish`。「删书」**无 skill**——`navigate("/books")` 让用户手动点删除（直删工具不在工具面）。「抓取 / 下载番茄小说 / 侦察热榜」→ `fetch_novel`（按书名/book_id 下载章节入库 `storage/novels/fanqie/`，无需 LLM，进度在 `/scout` 页实时显示）/ `discover_hot`（热榜）；「读已抓取 / 已抓取书库 / 借鉴参考书」→ `list_crawled_novels`（列出已抓书库）/ `read_crawled_novel`（读章节目录或单章正文，供借鉴设定/写法）；「提取入库 / 入库资产 / 提炼桥段大纲笑点角色」→ `ingest_library_assets`（读参考书后自主提炼桥段/大纲/笑点/角色入库四库，纯规则无 LLM）。拿不准阶段 → 先 `list_books` + `get_book_detail` 看目标书 `phase` 再定 skill；书多先问「对哪本书操作」。（**Claude Code 侧为交互式 `novel-build`，与 dsh 侧拆分独立演进、不做镜像**；Claude Code 端也可直接跑 `novel-master` 统一调度，dsh 端无 skill（2026-08-24 已删，仅 MCP 工具面），按上表自分发。）
- 每个分 skill 先用 `mcp__novel-engine__get_book_detail` / `get_book_state` 做前置 phase 检查；phase 不满足时引导前一阶段，不要跨阶段硬做。
- 状态信号：`storyline.phase ∈ config/outlines/plots/ready`；`book.status ∈ planning/writing/reviewing/finished/published/paused`。
- 写类工具带书级文件锁，冲突抛 `BookBusyError`，稍后重试；`budget_paused` 表示预算/额度触发，停下问用户。
- 需要可视化页面时用 `mcp__novel-engine__navigate` 切站内页（完整路由表见下）。切页与读数据是两回事：即使已用 get_book_state 读过数据，只要用户要「打开页面」就要再调 navigate。
- **护栏（必须遵守）**：建/删书工具不在工具面（无法经 MCP/任何工具面直调）。建书必须走「启动新书」向导（`navigate("/books/start")` + `drive_ui` 填表/点下一步，由系统创建）；**世界观在向导步 3「内容构建工作台」分阶段构建（core_conflict→大纲+桥段→势力→人物→其余维度）并随提交落库，书创建即带大纲 phase=ready 直接进写作台**（大纲+桥段由 **agent 自主生成**经 `drive_ui(set_outline)` 落表、随 submit 落库；旧 `generate_outline_preview`/`generate_full_outline`/`generate_world` 工具已删，兜底走 agent 自主生成 → `save_outlines`/`save_basic_info`）；删书必须 `navigate("/books")` 让用户手动点删除按钮。agent 不得绕向导直建书、不得代删书。

## 站内页面路由表（navigate 用；无书时部分页 302 重定向）

| 页面 | URL | 需要书 | 无书时 |
|---|---|---|---|
| 仪表盘 | `/` | 否 | — |
| 书库 | `/books` | 否 | — |
| 建书向导 | `/books/start` | 否 | — |
| 大纲生成 | `/books/generator` | 否 | 302→`/books/start` |
| 书详情 | `/books/<book_id>` | 是 | — |
| 世界观设定 | `/books/<book_id>/world` | 是 | — |
| 写作台 | `/books/<book_id>/continue` | 是 | — |
| 单本上架 | `/books/<book_id>/publish` | 是 | — |
| 上架总览 | `/publish` | 否 | — |
| 桥段库 | `/plots` | 否 | — |
| 角色库 | `/characters` | 否 | — |
| 情节弧库 | `/structures` | 否 | — |
| 笑点库 | `/gags` | 否 | — |
| 笔名档案 | `/profiles` | 否 | — |
| 新建笔名 | `/profiles/new` | 否 | — |
| 写作工具 | `/write` | 否 | 302→`/books` |
| 去AI味 | `/deai` | 否 | — |
| 审阅测试 | `/review-test` | 否 | — |
| 提取 | `/extract` | 否 | — |
| 番茄侦察兵 | `/scout` | 否 | — |
| 设置 | `/settings` | 否 | — |

（旧/内部路由 `/desk`、`/timeline/<id>/edit`、`/storyline/<id>/edit` 等为引擎内部页，agent 一般不用。）
- 平台 Web 服务端口 `58080`（`launch.bat` 启动）。主 skill 探测到未启动时**必须经 `launch.bat` 拉起**（`cmd //c start "" launch.bat`——可见独立终端窗口，用户可随时关窗停服；**勿用 `run_in_background` 跑 `python ui/web_ui.py`**，无可见窗口用户无法手动关闭）；`launch.bat` 会自动打开浏览器（`start "" http://localhost:58080`），勿重复启动。
