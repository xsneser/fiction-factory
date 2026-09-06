# NovelEngine — Claude Code 工作台

NovelEngine 是「可视化、外部 agent 可驱动的多阶段小说创作平台」。本仓库经 MCP server `novel-engine` 暴露一组工具（数量以 `tools/mcp_smoke.py` EXPECT_MCP_TOOLS 为准），Claude Code 经 `mcp__novel-engine__*` 驱动整本书创作。创作分四阶段，每阶段一个分 skill，由主 skill `novel-master` 统一调度：

> **当前驱动形态**：侧栏聊天大脑 = **dsh**（内置 agent `plugins/agent_loop.py` 已删除，无 builtin 可切回）。`libraries/dsh_bridge.py` 转发 vendored `vendor/dsh-ne/`（精简核心，改名防冲突）headless 子进程，经 MCP 驱动平台；`vendor/dsh-ne/events-runner.mjs` 把 dsh 的每个工具调用/结果实时推成 SSE（tool_call/tool_result/navigate/ui_command），侧栏实时工具卡、导航零延迟；护栏：phase 门控 `tool_policy.py` / MCP 循环熔断 `loop_guard.py` / 建书 reset。agent 架构见 `docs/架构文档-内置agent-dsh.md`；最新交接（2026-09-05 深化并入步3、提交即 ready）见 `docs/交接文档-2026-09-05-深化并入步3生成-提交即ready.md`（前序 2026-09-04 的 plots 草案门/提交后深化已被其取代，历史见 `docs/交接文档-2026-09-04-故事线草案评审门与研究式深化.md`）。
> **架构速览**（系统分层/工具注册表/双通道驱动/各阶段入口/常见坑）：`docs/架构总览.md`——交接/上手先读它，不必重新探索。设计权威仍为 `docs/设计文档-总览-claude.md`。
> **故事线概念研究**（大纲/情节段/线程 三者关系、「大纲≠卷」建模、插叙/并行视角现状）：`docs/研究文档-故事线大纲情节段线程.md`（2026-08-25 只读研究，未改代码）。
> **外部书目提取方法论**（novel-scout **顺序通读式提取**：整本扫读·自由分段·记忆压缩续段(`extract_state`)·入库查重，及弧/情节段/笑点/母题/写作风格判据；`total_words`=该弧真实字数跨度非全书）：`docs/设计文档-外部书目提取-顺序通读式提取.md`。

| 阶段 | 分 skill | 前置 phase | 出口 | 主要工具 |
|---|---|---|---|---|
| 建书 | `novel-build-candidates` + `novel-build` | 无书 / phase=config | `ready`（拆分：`novel-build-candidates` 生成候选并**呈现**（`set_candidates`），停在步 2 等用户挑选；用户点「已挑选完毕」后页面自动触发 `novel-build`——步 3「内容构建工作台」**深化式**构建（差异化命题 core_conflict+world_building.differentiation → 核心矛盾→弧+情节段(每弧 notes 目标+偏离模板点)→势力→人物→其余维度，内联 validate 回打≥1轮）并随提交落库，**书创建即 phase=ready（2026-09-05 深化并入步3、无书详情二次确认/深化段）**；config 兜底补弧走 save_outlines→plots→用户书详情「确认弧+情节段」） | drive_ui（驱动建书向导，含 set_candidates/set_outline）/ query_arc_library / arc_material_candidates / query_plots / query_characters / save_basic_info（旧 world_candidates / generate_core_conflict / generate_outline_preview / generate_factions / generate_characters / generate_rest_world / generate_full_outline / generate_world / confirm_world / confirm_outlines 工具**已删除**（2026-08-24/08-28 大清理，无兜底），步 3 内容全由 agent 自主生成经 set_outline / set_world / set_characters 落表） |
| 弧+写作 | `novel-story` | `ready`（写作；新书提交即 ready）— `config`/`plots` 为补弧兜底（排弧 → 用户在书详情「确认弧+情节段」→ `ready`） | 章节/情节段写完 | **agent 自主生成 → `save_outlines`** / arc_material_candidates / validate_storyline（内联/落地校验回打环）/ **`save_plot_draft` / `save_chapter_text`** / save_book_meta / chapter_quality_gate（完整章节质量门禁）；ready 翻转无 agent 工具——新书=用户在向导点提交即得，config 补弧兜底=书详情 UI 确认（fill_gags/confirm_outlines 已收敛移除） |
| 上架 | `novel-publish` | 已有第 1 章正文 | `published` / `finished` | publish_check / publish_book / mark_finished / export_book |

## 发现与编排规则

> ⚠️ **2026-08-28**：dsh 侧 skill 重写完成（原 2026-08-24 已删、仅 MCP 直驱）：`NOVEL_AGENT.md` **只留定义与契约**，创作流程拆成 **5 个 skill**（`agent-sidecar/skills/novel-*`，同步 `.dsh/skills/` 发现根）——`novel-scout`（侦察抓取）/ `novel-build-candidates`（步1-2）/ `novel-build`（步3）/ `novel-story`（弧+写作合一，原 outline+write 合并）/ `novel-publish`。dsh agent 按 `NOVEL_AGENT.md` §2 分发表用 Skill 工具调用。**Claude Code 侧 `.claude/skills/` 未改动**。

- **意图 → skill 分发表**：「开新书 / 写设定 / 建书 / 构思世界观 / 生成候选」→ dsh 侧 `novel-build-candidates`（生成候选并**呈现**到步 2，**停在交互点等用户挑选，不自动选/跳步**）；**侧栏要求建书先 `navigate("/books/start")` 翻到步 1 表单**（已给全则预填，笔名留用户选），用户填完点「🚀 让 Agent 构建」后按钮路径接管（只生成候选并呈现，停步 2）；「已选候选 / 补全世界观 / 继续建书」→ `novel-build`（步 3 分步建书 + 用户自行提交 + 完整弧）；「生成弧 / 排故事线 / 选情节段 / 一键完整弧 / 续写 / 扩写 / 写正文 / 写下一章 / 继续写 / 写情节段 / 一键写完整章 / 写完整章 / 一键完整章节 / 完整章节构建」→ `novel-story`（弧+写作合一：先 `save_outlines` 排弧 → 逐情节段 `save_plot_draft` → 章满 `save_chapter_text`；完整章节构建含质量门禁 `chapter_quality_gate`，默认执行、只报告不修复，问题作决策点）；「上架 / 发布 / 完本 / 导出 / 生成书名简介 / 检查能否发书」→ `novel-publish`。「删书」**无 skill**——`navigate("/books")` 让用户手动点删除（直删工具不在工具面）。「抓取 / 下载番茄小说 / 侦察热榜 / 读已抓取 / 借鉴参考书 / 提取入库」→ dsh 侧 `novel-scout`（`fetch_novel` 按书名/book_id 下载章节入库 `storage/novels/fanqie/`，无需 LLM，进度在 `/scout` 页实时显示（2026-09-01 合并页：`/scout` 外部书库=热榜+后端直抓下载+已下载书库，点书卡→`/extract` 提取工作台=选书→agent **顺序通读式提取**（整本扫读，上下文变重即 `extract_state` 压缩记忆落盘续段；默认自主入库、可 `set_review` 过目） / `discover_hot` 热榜 / `list_crawled_novels` 列出已抓书库 / `read_crawled_novel` 读目录(一次)/单章/成批窗口正文（无目录回声，供借鉴与整本扫读） / `extract_state` 扫读断点记忆 / `ingest_library_assets` 扫读提炼结果纯规则落四库）。拿不准阶段 → 先 `list_books` + `get_book_detail` 看目标书 `phase` 再定 skill；书多先问「对哪本书操作」。（**Claude Code 侧为交互式 `novel-build`，与 dsh 侧拆分独立演进、不做镜像**；Claude Code 端也可直接跑 `novel-master` 统一调度，dsh 端 skill 重写见 2026-08-28 注记，按 `NOVEL_AGENT.md` §2 分发表自分发。）
- 每个分 skill 先用 `mcp__novel-engine__get_book_detail` / `get_book_state` 做前置 phase 检查；phase 不满足时引导前一阶段，不要跨阶段硬做。
- 状态信号：`storyline.phase ∈ config/outlines/plots/ready`；`book.status ∈ planning/writing/reviewing/finished/published/paused`。
- 写类工具带书级文件锁，冲突抛 `BookBusyError`，稍后重试；`budget_paused` 表示预算/额度触发，停下问用户。
- 需要可视化页面时用 `mcp__novel-engine__navigate` 切站内页（完整路由表见下）。切页与读数据是两回事：即使已用 get_book_state 读过数据，只要用户要「打开页面」就要再调 navigate。
- **护栏（必须遵守）**：建/删书工具不在工具面（无法经 MCP/任何工具面直调）。建书必须走「启动新书」向导（`navigate("/books/start")` + `drive_ui` 填表/点下一步，由系统创建）；**世界观与故事线在向导步 3「内容构建工作台」深化式构建（差异化命题→core_conflict→弧+情节段(每弧 notes)→势力→人物→其余维度，内联 validate 回打≥1轮）并随提交落库，书创建即 phase=ready**（2026-09-05 深化并入步3：用户在向导点提交即解锁写作；ready 翻转无任何 agent 工具，仅 config/补弧兜底须用户书详情「确认弧+情节段」/api/book/&lt;id&gt;/confirm-storyline）（大纲+情节段由 **agent 自主生成**经 `drive_ui(set_outline)` 落表、随 submit 落库；旧 `generate_outline_preview`/`generate_full_outline`/`generate_world` 工具已删，兜底走 agent 自主生成 → `save_outlines`/`save_basic_info`）；删书必须 `navigate("/books")` 让用户手动点删除按钮。agent 不得绕向导直建书、不得代删书。

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
| 情节段库 | `/plots` | 否 | — |
| 角色库 | `/characters` | 否 | — |
| 情节弧库 | `/structures` | 否 | — |
| 笑点库 | `/gags` | 否 | — |
| 样文库（全局词条，场景分类） | `/samples`（写作风格注入的 STYLE REFERENCE；样文已从 `/profiles` 拆出，`/profiles` 只留风格 MD + AI 禁词） | 否 | — |
| 笔名档案 | `/profiles` | 否 | — |
| 新建笔名 | `/profiles/new` | 否 | — |
| 写作工具 | `/write` | 否 | 302→`/books` |
| 去AI味 | `/deai` | 否 | — |
| 审阅测试 | `/review-test` | 否 | — |
| 外部书库（合并页） | `/scout`（热榜侦察 + 后端直抓下载 + 进度 + 已下载书库；点书卡 → `/extract` 提取；`/novels` 302 至此） | 否 | — |
| 提取入库 | `/extract`（书库选书跳入 → agent 整本扫读提炼（顺序通读·记忆压缩续段）→ 默认自主入库，可选 set_review 勾选） | 否 | — |
| 外部书库旧地址 | `/novels`（已并入 `/scout` 外部书库合并页，兼容别名 302） | 否 | — |
| 外部书库阅读器 | `/novels/read?platform=&folder=`（单本阅读，章节懒加载；无 folder 302→`/scout`） | 否 | — |
| 设置 | `/settings` | 否 | — |

（旧/内部路由 `/desk`、`/timeline/<id>/edit`、`/storyline/<id>/edit` 等为引擎内部页，agent 一般不用。）
- 平台 Web 服务端口 `58080`（`launch.bat` 启动）。主 skill 探测到未启动时**必须经 `launch.bat` 拉起**（`cmd //c start "" launch.bat`——可见独立终端窗口，用户可随时关窗停服；**勿用 `run_in_background` 跑 `python ui/web_ui.py`**，无可见窗口用户无法手动关闭）；`launch.bat` 会自动打开浏览器（`start "" http://localhost:58080`），勿重复启动。
