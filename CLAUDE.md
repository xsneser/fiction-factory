# NovelEngine — Claude Code 工作台

NovelEngine 是「可视化、外部 agent 可驱动的多阶段小说创作平台」。本仓库经 MCP server `novel-engine` 暴露 36 个工具（MCP 面 35，`canvas_command` 仅 Web），Claude Code 经 `mcp__novel-engine__*` 驱动整本书创作。创作分四阶段，每阶段一个分 skill，由主 skill `novel-master` 统一调度：

| 阶段 | 分 skill | 前置 phase | 出口 | 主要工具 |
|---|---|---|---|---|
| 建书 | `novel-build` | 无书 / phase=config | `config` + `_world_generated` | create_book / world_candidates / generate_world / save_basic_info / confirm_world / generate_title |
| 大纲 | `novel-outline` | `config` 且 basic_info 充实 | `ready` | outline_material_candidates / generate_full_outline / generate_outlines / confirm_outlines / fill_plots / fill_gags / extend_outline |
| 写作 | `novel-write` | `ready` | 章节/桥段写完 | write_next_bridge / write_chapter / generate_book_meta / review_text / deai_text / diagnose_retention / tag_punch_points |
| 上架 | `novel-publish` | 已有第 1 章正文 | `published` / `finished` | publish_check / publish_book / mark_finished / export_book |

## 发现与编排规则

- 用户提「开新书 / 写设定 / 建书」→ `novel-build`；「大纲 / 排故事线 / 选桥段 / 续写」→ `novel-outline`；「写正文 / 写下一章 / 继续写」→ `novel-write`；「上架 / 发布 / 完本 / 导出」→ `novel-publish`。拿不准时先跑 `novel-master`，由它启动平台并分发。
- 每个分 skill 先用 `mcp__novel-engine__get_book_detail` / `get_book_state` 做前置 phase 检查；phase 不满足时引导前一阶段，不要跨阶段硬做。
- 状态信号：`storyline.phase ∈ config/outlines/plots/ready`；`book.status ∈ planning/writing/reviewing/finished/published/paused`。
- 写类工具带书级文件锁，冲突抛 `BookBusyError`，稍后重试；`budget_paused` 表示预算/额度触发，停下问用户。
- 需要可视化页面时用 `mcp__novel-engine__navigate` 切站内页（`/books/start` 建书、`/books/generator` 大纲、`/books/<book_id>` 详情、`/books/<book_id>/continue` 写作台、`/publish` 上架）。切页与读数据是两回事：即使已用 get_book_state 读过数据，只要用户要「打开页面」就要再调 navigate。
- 平台 Web 服务端口 `58080`（`launch.bat` 启动）。主 skill 会探测并拉起，勿重复启动。
