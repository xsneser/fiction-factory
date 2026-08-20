# NovelEngine — Claude Code 工作台

NovelEngine 是「可视化、外部 agent 可驱动的多阶段小说创作平台」。本仓库经 MCP server `novel-engine` 暴露 42 个工具（MCP 面 40，web-only 2 个：`create_book`/`delete_book`），Claude Code 经 `mcp__novel-engine__*` 驱动整本书创作。创作分四阶段，每阶段一个分 skill，由主 skill `novel-master` 统一调度：

> **当前驱动形态**：侧栏聊天大脑 = **dsh**（内置 agent `plugins/agent_loop.py` 已删除，无 builtin 可切回）。`libraries/dsh_bridge.py` 转发 vendored `vendor/dsh-ne/`（精简核心，改名防冲突）headless 一次性子进程，经 MCP 驱动平台；护栏：phase 门控 `tool_policy.py` / MCP 循环熔断 `loop_guard.py` / 建书 reset。dsh 替换决策见交接文档。
> **架构速览**（系统分层/工具注册表/双通道驱动/各阶段入口/常见坑）：`docs/架构总览.md`——交接/上手先读它，不必重新探索。设计权威仍为 `docs/设计文档-总览-claude.md`。

| 阶段 | 分 skill | 前置 phase | 出口 | 主要工具 |
|---|---|---|---|---|
| 建书 | `novel-build` | 无书 / phase=config | `ready`（3 步向导：步 3「内容构建工作台」分阶段构建世界观并随提交落库，提交后入库跳书详情；完整大纲由 agent 经 MCP `generate_full_outline` 生成后进入写作台） | drive_ui（驱动建书向导）/ world_candidates / generate_core_conflict / generate_factions / generate_characters / generate_rest_world / generate_full_outline / generate_world（仅世界观单薄时兜底）/ save_basic_info / confirm_world |
| 大纲 | `novel-outline` | `config` 且 basic_info 充实 | `ready` | outline_material_candidates / generate_full_outline / generate_outlines / confirm_outlines / fill_plots / fill_gags / extend_outline |
| 写作 | `novel-write` | `ready` | 章节/桥段写完 | write_next_bridge / write_chapter / generate_book_meta / review_text / deai_text / diagnose_retention / tag_punch_points |
| 上架 | `novel-publish` | 已有第 1 章正文 | `published` / `finished` | publish_check / publish_book / mark_finished / export_book |

## 发现与编排规则

- **意图 → skill 分发表**（Claude Code 与 dsh 共用）：「开新书 / 写设定 / 建书 / 构思世界观 / 借鉴已有书 / 写开头几章」→ `novel-build`；「生成大纲 / 排故事线 / 选桥段 / 一键完整大纲 / 续写 / 扩写」→ `novel-outline`；「写正文 / 写下一章 / 继续写 / 写桥段」→ `novel-write`；「上架 / 发布 / 完本 / 导出 / 生成书名简介 / 检查能否发书」→ `novel-publish`。「删书」**无 skill**——`navigate("/books")` 让用户手动点删除（delete_book 不在 MCP 面）。拿不准阶段 → 先 `list_books` + `get_book_detail` 看目标书 `phase` 再定 skill；书多先问「对哪本书操作」。（Claude Code 端也可直接跑 `novel-master` 统一调度，dsh 端无此 skill，按上表自分发。）
- 每个分 skill 先用 `mcp__novel-engine__get_book_detail` / `get_book_state` 做前置 phase 检查；phase 不满足时引导前一阶段，不要跨阶段硬做。
- 状态信号：`storyline.phase ∈ config/outlines/plots/ready`；`book.status ∈ planning/writing/reviewing/finished/published/paused`。
- 写类工具带书级文件锁，冲突抛 `BookBusyError`，稍后重试；`budget_paused` 表示预算/额度触发，停下问用户。
- 需要可视化页面时用 `mcp__novel-engine__navigate` 切站内页（完整路由表见下）。切页与读数据是两回事：即使已用 get_book_state 读过数据，只要用户要「打开页面」就要再调 navigate。
- **护栏（必须遵守）**：`create_book` / `delete_book` 已从 MCP 面移除。建书必须走「启动新书」向导（`navigate("/books/start")` + `drive_ui` 填表/点下一步，由系统创建）；**世界观在向导步 3「内容构建工作台」分阶段构建（core_conflict→大纲/桥段 picks→势力→人物→其余维度）并随提交落库，提交后入库跳书详情；完整大纲由 agent 经 MCP `generate_full_outline` 生成**（`generate_world` 仅世界观单薄时兜底）；删书必须 `navigate("/books")` 让用户手动点删除按钮。agent 不得绕向导直建书、不得代删书。

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
