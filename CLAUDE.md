# NovelEngine — Claude Code 工作台

NovelEngine 是「可视化、外部 agent 可驱动的多阶段小说创作平台」。本仓库经 MCP server `novel-engine` 暴露 40 个工具（MCP 面 37，`canvas_command` 仅 Web），Claude Code 经 `mcp__novel-engine__*` 驱动整本书创作。创作分四阶段，每阶段一个分 skill，由主 skill `novel-master` 统一调度：

| 阶段 | 分 skill | 前置 phase | 出口 | 主要工具 |
|---|---|---|---|---|
| 建书 | `novel-build` | 无书 / phase=config | `ready`（世界观+完整大纲由 agent 经 MCP 生成，向导第 4 步 Gantt 实时展示） | drive_ui（驱动建书向导）/ world_candidates / generate_world / generate_full_outline / save_basic_info / confirm_world / generate_title |
| 大纲 | `novel-outline` | `config` 且 basic_info 充实 | `ready` | outline_material_candidates / generate_full_outline / generate_outlines / confirm_outlines / fill_plots / fill_gags / extend_outline |
| 写作 | `novel-write` | `ready` | 章节/桥段写完 | write_next_bridge / write_chapter / generate_book_meta / review_text / deai_text / diagnose_retention / tag_punch_points |
| 上架 | `novel-publish` | 已有第 1 章正文 | `published` / `finished` | publish_check / publish_book / mark_finished / export_book |

## 发现与编排规则

- 用户提「开新书 / 写设定 / 建书」→ `novel-build`；「大纲 / 排故事线 / 选桥段 / 续写」→ `novel-outline`；「写正文 / 写下一章 / 继续写」→ `novel-write`；「上架 / 发布 / 完本 / 导出」→ `novel-publish`。拿不准时先跑 `novel-master`，由它启动平台并分发。
- 每个分 skill 先用 `mcp__novel-engine__get_book_detail` / `get_book_state` 做前置 phase 检查；phase 不满足时引导前一阶段，不要跨阶段硬做。
- 状态信号：`storyline.phase ∈ config/outlines/plots/ready`；`book.status ∈ planning/writing/reviewing/finished/published/paused`。
- 写类工具带书级文件锁，冲突抛 `BookBusyError`，稍后重试；`budget_paused` 表示预算/额度触发，停下问用户。
- 需要可视化页面时用 `mcp__novel-engine__navigate` 切站内页（完整路由表见下）。切页与读数据是两回事：即使已用 get_book_state 读过数据，只要用户要「打开页面」就要再调 navigate。
- **护栏（必须遵守）**：`create_book` / `delete_book` 已从 MCP 面移除。建书必须走「启动新书」向导（`navigate("/books/start")` + `drive_ui` 填表/点下一步，由系统创建）；**建书后世界观+完整大纲由 agent 经 MCP `generate_world` / `generate_full_outline` 生成**（向导不再自动跑 SSE，第 4 步 Gantt 轮询实时填充）；删书必须 `navigate("/books")` 让用户手动点删除按钮。agent 不得绕向导直建书、不得代删书。

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
| 大纲库 | `/structures` | 否 | — |
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
- 平台 Web 服务端口 `58080`（`launch.bat` 启动）。主 skill 会探测并拉起，**拉起成功后自动打开浏览器页面**（复刻 launch.bat 的 `start "" http://localhost:58080` 行为），勿重复启动。
