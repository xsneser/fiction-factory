# NovelEngine 设计文档（总览 · 当前状态）

> 版本：v1.1 ｜ 更新：2026-08-17 ｜ 整理：Claude
> 定位：**唯一主设计文档**。本文档合并吸收并取代以下源文档（已归档至 `docs/archive/`）：
> `项目规划.md`（v0.6）· `交接文档.md` · `harness重构交接文档.md` · `新书创建-Harness架构与LLM提示词.md` · `优化方案-2026-08-04.md` · `优化方案核对-2026-08-04.md` · `待codex处理-2026-08-04.md` · `UX报告-2026-08-05.md` · `task-system-spec.md` · `ui-notes.md` · `novel-factory-timeline.html` · `设计文档.md`（另一会话合并版，v1.1 已并入并退役）
>
> **代码是最终真相**：本文档所有架构事实均以当前代码为准（HEAD `53cee50`）。若与代码冲突，以代码为准并回写本文档。
> 测试基线：`python test_all.py` **94/94** 通过。

---

## 目录

1. [项目定位与理念](#一项目定位与理念)
2. [架构总览（当前实际）](#二架构总览当前实际)
3. [核心数据模型与资产库](#三核心数据模型与资产库)
4. [创作管线](#四创作管线)
5. [LLM 提示词架构](#五llm-提示词架构)
6. [工程实践现状](#六工程实践现状)
7. [质量体系与测试](#七质量体系与测试)
8. [UI/UX](#八uiux)
9. [技术决策记录](#九技术决策记录)
10. [运行与测试 · 工程约定](#十运行与测试--工程约定)
11. [数据现状](#十一数据现状)
12. [开放决策与余量](#十二开放决策与余量)
13. [路线图](#十三路线图)
14. [文档索引与参考项目](#十四文档索引与参考项目)

---

## 一、项目定位与理念

**全自动网文量产系统**：AI 拟人写作 × 桥段驱动生成 × 外部采集 + 分析入库。

### 核心理念

每个笔名 = 一个独立的 AI 作家，拥有独立的记忆、风格、桥段库和创作习惯。不是"一个生成器生成多本书"，而是"一群 AI 作家同时开工"。

### 不是

- 不是 MOSS 项目的子模块（完全独立）
- 不是"按模板生成一段，人工编排"的半自动工具（已全自动）
- 不是简单 prompt 拼接（有完整架构层）

### 1.1 核心设计决策（harness 三决策，2026-08-03）

| 项 | 决策 | 落地 |
|---|---|---|
| **笑点** | 完全涌现，大纲不参与 | 删除大纲固定笑点分配（`gag_ids` 字段保留但不再写入）；写作时每写完一组短句由探测器判断是否注入（§4.6） |
| **内涵** | 跟随桥段 | 用 `THEME_PLOT_COMPAT`（storyline.py:255，桥段模板 id → 可承载母题名）免费规则把母题挂到能承载它的桥段，不强挂（§3.6） |
| **长程记忆** | LLM 语义摘要 | 每章写完生成 80-150 字客观摘要存 `summary` 字段，写作时注入最近 5 章（§4.5） |
| **架构** | 集中式 harness | `prompt_harness.py` 统一产出书级设定卡与全部 prompt（§5） |

### 1.2 硬约束（用户反复强调，任何改动不得违反）

1. **保持 `deepseek-v4-flash` 模型**，不换模型。
2. **只优化 prompt / 生成参数 / 免费规则层**；不加"写完质量重写"型 LLM 后处理（探测器/摘要属于生成过程的一部分，不算后处理）。
3. 探测器/摘要调用小而轻：**输入 ≤500 token、输出 ≤256**。

---

## 二、架构总览（当前实际）

### 2.1 技术栈（当前实际）

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.10+ | — |
| Web | **纯 Flask 3 + Jinja2**（端口 58080） | FastAPI / main.py / run.py 已随 v2 收敛**删除**；`ui/web_ui.py` 仅 35 行壳，注册 10 蓝图 |
| LLM | DeepSeek API（`deepseek-v4-flash`，兼容 OpenAI 格式） | `core/llm_client.py` 同步 + 流式 |
| 存储 | JSON 文件系统 | 无数据库；`core/json_store.py` 原子写 |
| 采集 | SSR 解析 + PUA 字体解码（fonttools） | 番茄搜索 API 已失效，走 Bing + SSR |
| 依赖 | urllib3 / requests / flask / jinja2 / fonttools | `requirements.txt` 仅 5 项 |

### 2.2 目录结构（当前实际）

```
D:\NovelEngine/
├── ui/                          # Web 层（纯 Flask，无 main.py/run.py）
│   ├── web_ui.py                # Flask app 创建 + 日志 + 蓝图注册（35 行）
│   ├── web_blueprints/          # 10 个蓝图（74 路由）
│   │   ├── ctx.py               # 共享：全局服务/LLM/引擎缓存/故事线统一存取/sse_stream_response
│   │   ├── dashboard.py         # 仪表盘 + 新书启动（3 路由）
│   │   ├── storyline.py         # 故事线编辑器/详情 + 全部编辑 API，含 /timeline 302 兼容别名（19 路由）
│   │   ├── desk.py              # 写作台（三栏）+ SSE 写作端点（9 路由）
│   │   ├── books.py             # 书库/详情/书名简介生成（4 路由）
│   │   ├── libraries.py         # 四大库 + 笔名管理页（12 路由）
│   │   ├── publish.py           # 上架检查/发布/导出（7 路由）
│   │   ├── settings.py          # 设置 + 任务状态 API（6 路由）
│   │   ├── tools.py             # 侦察兵/提取/审阅/降重（9 路由）
│   │   └── world_builder.py     # 世界观设定卡（5 路由）
│   ├── templates/               # 24 个 Jinja2 模板
│   └── static/                  # base.css / story_line.css / workspace.css；base.js / story_line.js / library_review.js
├── libraries/                   # 业务引擎（唯一写作核心 = 桥段写作）
│   ├── engine.py                # 总调度（1101 行）：start_new_book / continue_book / 桥段流式写作 / 审阅门禁 / 承诺台账 / 摘要
│   ├── storyline.py             # BookStoryline/OutlineSlot/PlotSlot + StorylineBuilder + THEME_PLOT_COMPAT（657 行）
│   ├── storyline_writer.py      # ★桥段写作核心 StorylineChapterWriter（541 行）
│   ├── prompt_harness.py        # ★集中式 prompt（846 行）
│   ├── gag_injector.py          # ★笑点探测器环
│   ├── outline_generator.py     # 大纲 6 阶段 LLM 管线（1212 行）
│   ├── outline_agent.py         # 大纲助手（自然语言改故事线）
│   ├── world_builder.py         # 世界观生成器 WorldBuildingGenerator（设定先行）
│   ├── example_lib.py           # 摘录库（写法范本）
│   ├── book_meta.py             # 书名/简介/平台约束纯函数
│   ├── book_manager.py          # 图书 CRUD + 章节摘要读写
│   ├── character_state.py       # 角色状态跟踪
│   ├── reviewer.py              # 规则审查（章节软门禁）
│   ├── de_ai.py                 # 去 AI 味（AI_WORD_MAP 单一词表来源）
│   ├── cost_tracker.py          # 费用追踪 + 预算门控
│   ├── publisher.py             # 上架检查 + 状态机
│   ├── profiles.py              # 笔名风格档案（PRESET_PROFILES：枫落/夜雨/青衫）
│   ├── assembler.py             # 旧书兼容（BookAssemblerPlan/load_plan），不再生成新计划
│   ├── base_library.py          # JsonLibrary 基类（单例 + 读写）
│   ├── plot.py / structure.py / gag.py   # 桥段/大纲/笑点库
│   └── data/                    # plots(47) / structures(11) / gags(24) / themes(20) / excerpts(16) JSON
├── core/                        # 仅 LLM 基础设施（5 文件 + embeds）
│   ├── llm_client.py            # LLMClient（同步/流式）+ extract_json
│   ├── models.py                # APIConfig（context_budget_tokens 字段仅存配置，无计算逻辑）
│   ├── text_utils.py            # count_prose_units（中文字数唯一来源）
│   ├── json_store.py            # read_json / write_json_atomic
│   ├── safe_paths.py            # ensure_child_path / is_safe_book_id / parse_int
│   └── embeds/                  # 空目录残留（旧 core/embeds 内置技能已随 v2 收敛删除，无技能文件）
├── plugins/                     # 采集与工具
│   ├── fanqie_scout.py          # 番茄侦察兵（搜/下/析/入库）
│   ├── font_decoder.py          # PUA 字体解码（362 映射表）
│   ├── novel_storage.py / style_analyzer.py / task_manager.py
│   └── sites/ social/           # 预留空目录
├── tools/                       # 测试与运维脚本
│   ├── test_full_flow.py        # 真实 LLM E2E（约 30 分钟，自建自删测试书）
│   ├── test_outline_quality.py / test_world_builder.py / simulate_full_flow.py
│   ├── smoke_flow_full.py / smoke_world_card.py / migrate_storyline.py
│   ├── e2e_flow_test.py / shot_ui_pages.py / reset_book_001.py
├── books/                       # 图书数据（gitignore）：book_001 / book_003 …
│   └── {book_id}/ book.json · storyline.json · chapters/ · outline/ · draft_chapter.json · character_states.json · cost.json
├── profiles/                    # 笔名档案 profile_001~005.json（gitignore）
├── storage/                     # 运行时缓存（gitignore）
├── api.json / api.example.json  # API 配置（Key 明文，gitignore）
├── test_all.py (94 项) · test_chapters.py · test_e2e_pages.py · test_reader.py
├── launch.bat / launch.sh
└── docs/                        # 唯一主设计文档 + archive/（全部历史文档归档）
```

### 2.3 架构核心结论

- **单写作核心**：`storyline_writer.py`（桥段驱动），`beat_writer.py`/`new_book.py`/`writing_pipeline.py` 已整体删除。
- **集中式 prompt**：`prompt_harness.py` 是唯一 prompt 出口，全项目不再有散落的 prompt 拼接。
- **笑点涌现**：笑点不再由大纲计划分配，改为写作时探测器环实时检测注入。
- **timeline → storyline 改名**：`timeline_writer.py`→`storyline_writer.py`、`timeline.py`→`storyline.py`、`BookTimeline`→`BookStoryline`、`TimelineBuilder`→`StorylineBuilder`、`TimelineChapterWriter`→`StorylineChapterWriter`、`timeline.json`→`storyline.json`；Web 路由 `/timeline/...` 保留为 302 兼容别名。
- **草稿目录已废弃**：`books/timelines/tl_*` 已删除（`tools/migrate_storyline.py` 迁移完成），规划态故事线直接存书目录内 `books/<id>/storyline.json`。

---

## 三、核心数据模型与资产库

### 3.1 BookStoryline（书级大纲/桥段容器）

定义位置：`libraries/storyline.py`（`dataclasses`）。

| 字段 | 说明 |
|---|---|
| `book_title/genre/sub_genre/words_per_chapter/pen_name/platform` | 书级元信息 |
| `basic_info` | 设定卡：protagonist / world_building / supporting_cast / tone / target_audience / pov / era_language（`rebirth_time`/`style` 不是 basic_info 顶层键，是书级设定卡的派生渲染段） |
| `outlines[]` | 大纲槽位（多条，可重叠/接续/融合） |
| `plots[]` | 桥段槽位（挂在某大纲某阶段下） |
| `threads[]` | 叙事线程（多线程设局/收局） |
| `themes[]` | 全书母题（内含 theme_hints 挂载） |
| `promises[]` | 读者承诺台账 |
| `global_gags[]` | 书级全局笑点线索 |
| `phase` | 生成进度：config → outlines → plots → gags → ready |
| `generated_at/updated_at` | 时间戳 |

### 3.2 OutlineSlot（大纲槽位）

`template_id`（流派模板）、`name`、`start_chapter`/`end_chapter`、`stages[]`（`{name,min_ch,max_ch,events}`）、`transition_type`（sequential/overlap/merge）、`narrative`（chronological/flashback/interleaved）、`overlaps_with`（与哪些大纲重叠 id 列表）、`predecessor`/`successor`（前驱/后继大纲 id）。

### 3.3 PlotSlot（桥段槽位）

`template_id`、`name`、`category`、`outline_id`、`stage_index`、`order`、`cover_beats`、`template_structure`（骨架，如"甩婚书→众人嘲讽→展现实力→反转打脸"）、`slots[]`（变量槽）、`gag_ids`（兼容保留，**不再写入**）、`theme_hints`、`hook_points`（吸睛点）、`thread_id`/`thread_seq`（线程归属）、`resolves_plot_id`/`resolves_name`（收局指向）、`roles[]`（出场人物）、`parent_plot_id`/`children_plot_ids`（嵌套层级）、`written_chapter`（断点续写）。

### 3.4 CharacterState（角色状态）

`gender` / `personality` / `catchphrase` / `brief` + 章节出场标注（`update_from_chapter` 纯规则标记，只存结构化状态，无原文片段）。

### 3.5 BookConfig（书配置）

`title`/`genre`/`sub_genre`/`words_per_chapter`/`pen_name`/`platform`/`status`（planning→writing→finished→published）/`budget`（默认 50 元）/`detector_frequency`（探测器旋钮，默认 1）等。

### 3.6 资产库（四大库 + 摘录库）

统一基类 `JsonLibrary`（`base_library.py`）：进程内单例 + JSON 读写；条目含 `enabled / usage_count / banned_in[book_id] / source / created_at`。**改 `libraries/data/*.json` 必须重启服务才生效。**

| 库 | 模块 | 数据文件 | 数量 | 用途 |
|---|---|---|---|---|
| 桥段库 | `plot.py` | `plots.json` | 47 模板（12 内置 + 采集） | 桥段模板：category / template_structure / slots / fit_contexts；写作时作【桥段骨架】注入 |
| 大纲库 | `structure.py` | `structures.json` | 11 模板（5 内置 + 采集） | 卷→弧→章三级骨架，按流派搜索 |
| 笑点库 | `gag.py` | `gags.json` | 24 模式（10 内置 + 采集） | 探测器候选池（不写进大纲） |
| 摘录库 | `example_lib.py` | `excerpts.json` | 16 条演示（7 类型） | 真实原文范本，写前按 category 预筛 2 条注入【写法范本】 |
| 内涵库 | ~~theme.py~~（已删） | `themes.json` | 20 母题 | 书级母题库 + `THEME_PLOT_COMPAT` 免费规则挂载 |

- **内涵体系演进**：`theme.py` 模块已删除；`themes.json` 仍作为书级母题库。大纲不再做固定笑点分配；母题挂载改为 `THEME_PLOT_COMPAT` 命中才挂，未命中母题仍随书级设定卡注入作为可用线索。
- **笔名档案**（`profiles.py`）：`word_print`（common/avoid/dialogue_tags/action_beats）+ `style_fingerprint`（sentence_length/humor_style/action_style/pov_preference）。预设：**枫落**（都市爽文）、**夜雨**（玄幻正剧）、**青衫**（言情甜文）；`profiles/` 现存 profile_001~005。风格以 bullet 注入书级设定卡；幽默风格喂给探测器。

### 3.7 数据落盘约定

| 数据 | 路径 | 写入时机 |
|---|---|---|
| 书配置 | `books/{id}/book.json` | create/update |
| 大纲/桥段/线程/承诺 | `books/{id}/storyline.json` | 每阶段结束 / 每桥段写完 |
| 结构大纲 | `books/{id}/outline/outline.json`（含 synopsis） | 书名/简介生成后 |
| 章节 | `books/{id}/chapters/{NNNN}.json`（含 `.summary` 语义摘要 + review） | 每章固化 |
| 进行中草稿 | `books/{id}/draft_chapter.json` | 每桥段写完未切章（预算暂停/中断恢复） |
| 角色状态 | `books/{id}/character_states.json` | 每章收尾 |
| 费用 | `books/{id}/cost.json` | 每次调用 |

---

## 四、创作管线

### 4.1 全流程总览

```
设定先行 → 世界观生成 → 大纲生成(6阶段) → 时间线编辑 → 写作台(桥段写作) → 审阅/去AI/摘要 → 发布上架
新书：设定(世界观) → 规划(故事线) → 大纲(6阶段) → 写作(桥段驱动) → 书名/简介
续写：写 → 审(软门禁) → 去AI → 摘要 → 台账 → 继续写
```

所有写作（含新书启动）走**同一条桥段管线**；开场只是它的一种模式（§4.4）。

### 4.2 新书创建（设定先行 + 世界观卡）

1. 用户在启动页输入想法 → **世界观设定卡页**（`world_builder.py` 的 `WorldBuildingGenerator`，独立于大纲引擎）。
   - 三种启动方式：① 一句话自由输入（Call A 流式叙事化短文 temp≈0.8 → Call B 结构化 JSON temp≈0.5、失败重试 ≤3）；② 示例候选点选回填；③ 从已有书借鉴（`extract_seed`）。
   - 落盘语义：`merge_basic_info` 保留用户已填非空字段，末尾打 `_world_generated` 标记。
   - 产出「设定圣经」维度：description / era / power_system / factions / rules（数值语义写死，全书唯一口径）/ geography / culture / history / social_structure / core_conflict / world_summary（`DEFAULT_WORLD_BUILDING`）。
   - 若 `basic_info_world_done` 已充实，`OutlineGenerator` Phase 1 可**跳过 LLM 故事分析**直接复用。
2. 步骤条含世界观步骤；世界卡确认后进入规划态。

### 4.3 大纲生成引擎（`outline_generator.py`）

**6 阶段 LLM 管线**（SSE 流式，每阶段 `yield ("phase"|"progress"|"phase_done"|"warnings", ...)`，`on_save` 每阶段落盘 `storyline.json`；任意阶段失败/无 LLM → 规则兜底）：

| 阶段 | 方法 | LLM | 产出 |
|---|---|---|---|
| Phase 1 故事分析 | `_analyze_story` | temp 0.7, max 2048（非流式） | protagonist/world_building/supporting_cast/tone/target_audience/pov/era_language；失败 → `_default_basic_info`；后接 `_validate_storyline_math` |
| Phase 2 故事线规划 | `_ai_sequence` | 流式 0.7, 8192 | 选 2-max_outlines 个模板排时间线（可 overlap 2-5 章）；兜底 `_rule_sequence` |
| Phase 3 桥段选择 | `_ai_select_plots` | 流式 0.5, 8192 | 每阶段 1-3 个（候选 ≤12）；兜底 `candidates[:2]`；cover_beats=word_range//400，slots 展开，链式嵌套 |
| Phase 4 线程与呼应 | `_plan_threads_and_splits` | 非流式 0.3, **16384**（空返回重试 3） | threads[]/assignments[]/splits[]（设局→收局）；兜底按分类归线 |
| Phase 4.5 内涵复查 | `_review_theme_assignments` | 流式 0.3, 8192 | 复查母题挂载（只接受书级母题库内名字，每桥段 ≤2） |
| Phase 5/6 一致性验证 | `_validate`（规则）+ `_validate_with_llm` | 规则先行 + LLM 抽查 | issues/warnings 事件（⚠️ 无自动重试，见 §13） |

**规则校验项**（`_validate`）：章节连续性、重叠区 ≤5 章、桥段覆盖、内涵覆盖率 ≥30%（建议性）、总章节 10-500。

**兼容路径**：`StorylineBuilder`（storyline.py）的规则/AI 模式仍被 `/extend-outline`、`/generate-outlines`、`/fill-plots`、`/fill-gags` 等编辑端点使用；已与 OutlineGenerator 抽公共函数（`structure_to_stages` / `mount_themes_and_hooks`）消除漂移，全量迁移留作后续评估。

**OutlineAgent**（Prompt L）：解析用户口语指令（intent: modify_plot/add_gag/remove_gag/add_plot/remove_plot/modify_outline/general），temp 0.2, max 2048；解析失败 → general + 帮助文本。

### 4.4 写作台（桥段驱动唯一写作核心）

入口 `POST /api/storyline-engine/{id}/write-bridge`（SSE）→ `engine._write_next_bridge_stream` → `storyline_writer.write_bridge_stepwise` → `prompt_harness.render_bridge_prompt`（Prompt I）+ `gag_injector.detect`（Prompt J）。

**桥段驱动逐短句组**：`StorylineChapterWriter._write_plot_segment_groups` 每个桥段内逐「短句组」调用 LLM（每次 3-5 短句约 150-250 字，temp 0.7，max_tokens 1600），组与组之间空行分隔成独立段落；每次调用携带「本章已写全部前文 + 上一章结尾[-150] + 本桥段已写[-300] + 角色状态」。桥段按叙事线程轮流排列（`_threaded_ordered_plots`：主线加权 2:1，副线/伏笔线各 1；全主线时退化为严格顺序）。写完累计字数满 `words_per_chapter` 切章；每桥段写完立即写 `written_chapter`（断点续写）。

**质量重试通道**（复用 `WRITER_EMPTY_RETRIES=2`）：
- 空响应重试（flash 推理吃光 max_tokens 的静默空返回）
- 连续重复词重试（`has_repeated_token`，笑声/拟声叠词白名单放行）
- 疑似错词重写（`_typo_issue`，`_TYPO_PATTERNS` 现 1 条：「先轻轻」→「先缓一缓」）
- 0 字桥段保护：不置 written_chapter、yield `bridge_skip`、下章重试

**炸裂开场**（`OPENING_MODE_RULES`）：第 1 章前 800 字且前 3 桥段命中 `opening_mode_active` → 注入铁律（冷开场三句入冲突 / 前 200 字钩子 / 前 500 字危机 / 冲突线前置）。

**写作铁律**（system 层 7 条）：3-5 短句/一句一行/严禁词表/预算控制等。

### 4.5 续写循环

```
写桥段 → reviewer 软门禁（评分+AI 痕迹）→ _review_to_hint 注入下一桥段
       → de_ai.process_rule_based（去 AI 味）→ _summarize_chapter（语义摘要）
       → 承诺台账更新 → 下一桥段
```

- **审阅门禁**：`reviewer.py`（ContentReviewer：字数/AI 痕迹/段落节奏/对话比/断章），`engine._review_to_hint`（:453）把 review 转成 hint 注入下一桥段（软门禁不阻断，一次性用完）。
- **语义摘要**：`engine._summarize_chapter`（temp 0.3, max 1024）输入本章尾部 600 字 + 本桥段名 → 80-150 字客观摘要（发生了什么/主角状态/埋的钩子）→ 存 `chapters/{NNNN}.json.summary`；写作前 `load_chapter_summaries` 注入最近 5 章（≤600 字）作为【已完成章节语义摘要】。
- **承诺台账**：`BookStoryline.promises[]`（`{id, setup_plot_id, type, desc, status: pending|advanced|fulfilled, setup_chapter, deadline_chapter, payoff_plot_id, payoff_chapter}`）。免费规则登记：设局桥段写完 → pending；收局桥段写完 → fulfilled；阶段推进 → advanced（`engine._update_promises_ledger` :505）。写作注入【读者承诺台账】块（本桥段要兑现/已逾期/活跃可推进，按章节号对比 deadline）。
- **写前编辑诊断**：`_pre_write_diagnosis`（prompt_harness:548）免费规则现算（读者欲望/最强爽点/敌人损失/追更理由/承接上章钩子）。

### 4.6 笑点涌现探测器（`gag_injector.py`）

```
prescreen_pool(plot, book_id) → harness.prescreen_gag_pool（免费规则 ≤6 条：
  category → fit_scene 关键词 → search 去重 → 过滤 enabled/banned_in → usage_count 升序）
detect(item, recent_text, humor_style, pool) → temp 0.3, max_tokens 400
  → 解析失败/缺字段/越权模式(gag_ids⊄池) 一律未命中（静默，绝不打断写作）
命中 → build_inspiration_hint → 【灵机一动】注入下一组 prompt，用完即清
```

- 集成点：`_write_plot_segment_groups` 每写完一组、预算未用尽（remaining>100）、按 `detector_frequency`（BookConfig 字段，默认 1）时运行；命中时 yield `gag_hit` SSE 事件。
- **设计纪律：宁缺毋滥**——`has_opportunity=false` 不注入任何东西；deploy_hint 空或 >60 字丢弃；不改"一句收进场景、不解释、不标注笑点"的落点纪律。

### 4.7 叙事多线程 + 设局/收局

- `PlotSlot.thread_id/thread_seq`：大纲 Phase 4 由 LLM 规划线程归属（主线/副线/伏笔线，第 1 章附近多线并进）；兜底 `_apply_thread_fallback` 按分类归线。
- 设局→收局拆分：选中适合的桥段（阴谋/悬疑/智斗/成长转折）生成收局槽位（`resolves_plot_id/resolves_name`，payoff_after_stage≥2），纯即时爽点不拆。
- 写作注入：收局桥段带【本桥段收束】解决『X』埋的钩子；设局桥段带【设局桥段】为『Y』埋钩子，结尾留明确未解决悬念。
- 甘特图含 🧵 线程横带；承诺台账 + 设局收局部分替代原伏笔系统（`core/foreshadow.py` 已删）。

### 4.8 叙事纪律（规则层校验）

| 纪律 | 机制 | 位置 |
|---|---|---|
| 视角人称 | `basic_info.pov` 显式字段（Phase 1 生成），bible + 每桥段【视角铁律】 | prompt_harness |
| 角色性别/称呼 | 配角 `gender/title` + 出场人物块【本桥段出场人物】 | prompt_harness `_roles_block` |
| 时代语言 | bible 自动生成"禁止晚于时代新词"（era 年份 ≤2015 兜底） | `_era_language_bullets` |
| 时间线校验 | Phase 1 后 `_validate_storyline_math`（重生死亡年份<故事年份、年龄/年份自洽），独立 `timeline_warnings` | outline_generator |
| 重复词 | `has_repeated_token` 检测，该组仅重试一次 | storyline_writer |
| 疑似错词 | `_typo_issue` 规则检测，命中重写一档 | storyline_writer |

### 4.9 写前注入块（免费规则 + 轻量 LLM）

`render_bridge_prompt` 按需注入：书级设定卡（简）→ 开场铁律 → 全书一致性铁律 → 视角铁律 → 所属大纲/当前阶段/事件 → 【桥段骨架】+ 槽位 → 出场人物 → 母题 → 收局/设局 → 灵机一动 →【本桥段吸睛点】（hook_points）→【写法范本】（example_lib 2 条）→【写前编辑诊断】→【读者承诺台账】→ 已完成章节摘要 → 前文上下文 → 平台约束 → 上章审查提示 → 写作要求 7 条。

### 4.10 发布上架（`publisher.py`）

- 5 项 **error 级**检查（缺一不可）：`_check_title`/`_check_synopsis`/`_check_words`/`_check_review`/`_check_finished`。
- 状态机：`writing → finished → published`（`mark_finished` 标完本 + `finished_at` + 重算 total_words）。
- 平台导出：番茄 / 起点格式手动导出。

### 4.11 成本与预算

`cost_tracker.py`：成本统计 + 预算门控（BookConfig.budget 默认 50 元）。桥段端点预算耗尽 → `bridge_skip` + `budget_paused` 事件，**保留草稿不固化**；整章端点同样存草稿不固化。前端 `timeline_write_flow.html` 有 `budget_paused`（warn）与 `bridge_skip`（info）分支。

---

## 五、LLM 提示词架构

### 5.1 统一 LLM 客户端

`core/llm_client.py`（`LLMClient`）：同步/流式 + `extract_json`；单例 `ctx.get_llm()`（从 api.json 读 APIConfig，懒加载）。全项目 LLM 调用统一走它。

### 5.2 PromptHarness（`prompt_harness.py`）

**渲染出口（6 核心 + 3 世界观 + 2 预筛）**：

| 方法 | 用途 |
|---|---|
| `build_book_bible(max_chars=1200)` | 书级设定卡全量（大纲 Phase 2/5 用） |
| `build_book_bible_condensed(max_chars=600)` | 设定卡精简版（写作/探测器/Phase 3 用，目标 ~440 字） |
| `render_bridge_prompt(...)` | 桥段写作（Prompt I） |
| `render_detector_prompt(...)` | 笑点探测器（Prompt J） |
| `render_summary_prompt(...)` | 语义摘要（Prompt K） |
| `render_outline_context(phase_kind)` | 大纲各阶段上下文（sequence/select_plots/validate/theme_review/thread_split） |
| `prescreen_gag_pool(plot, book_id)` | 笑点预筛（≤6） |
| `prescreen_excerpts(plot, book_id, limit=2)` | 写法范本预筛 |
| `render_world_build_draft_prompt` | 世界观草稿 |
| `render_world_build_struct_prompt` | 世界观结构化 |
| `render_world_candidates_prompt` | 候选世界观/书名 |

**设定卡 9 分段**：`_protagonist_bullets` / `_world_bullets` / `_tone_bullets` / `_theme_bullets` / `_supporting_cast_bullets` / `_pov_bullets` / `_era_language_bullets` / `_style_bullets`（支持 PenNameProfile 对象与 dict 两种形态）/ `_rebirth_time_bullets`。

**模块级常量**：
- `OPENING_MODE_RULES`（炸裂开场：冷开场三句入冲突/前 200 字钩子/前 500 字危机/冲突线前置）
- `CONSISTENCY_RULES`（全书一致性：系统绑定全书只一次/数值必须闭环/对话时间线不穿帮/跨天要有时间过渡）
- `WORLD_BUILD_SYSTEM` / `WORLD_BUILD_STRUCT_SYSTEM` / `WORLD_CANDIDATES_SYSTEM` / `WORLD_BUILDING_SCHEMA_HINT`（世界观生成 system）

### 5.3 Prompt A~L 参数表

> 调用点/温度/输出上限/输入/频率为当前代码与实测校准值。

| 编号 | 用途 | 调用点 | 流式 | temp | max_tokens | 关键输入 | 频率 |
|---|---|---|---|---|---|---|---|
| A | 故事分析 | `_analyze_story` | 非流式 | 0.7 | 2048 | 流派/子流派+笔名风格+用户想法 | 每新书 1 次 |
| B | 故事线规划 | `_ai_sequence` | 流式 | 0.7 | 8192 | 设定卡≤900 + 候选模板≤10 | 每新书 1 次 |
| C | 桥段选择 | `_ai_select_plots` | 流式 | 0.5 | 8192 | 设定卡精简 + 候选≤12 | 每阶段 1 次 |
| D | 线程与呼应 | `_plan_threads_and_splits` | 非流式 | 0.3 | **16384** | 设定卡+大纲+桥段≤60 | 每新书 1 次 |
| E | 内涵复查(4.5) | `_review_theme_assignments` | 流式 | 0.3 | 8192 | 桥段快照≤20 | 每新书 1 次 |
| F | 一致性验证 | `_validate_with_llm` | 流式 | 0.3 | 8192 | 大纲视图≤10 | 每新书 1 次 |
| G | 书名生成 | `_generate_book_meta` | 非流式 | 0.8 | 1024 | 流派/平台+第1章前1000字 | 详情页手动触发 |
| H | 简介生成 | `_generate_book_meta` | 非流式 | 0.8 | 1024 | 同 G | 同上 |
| I | 桥段写作 | `StorylineChapterWriter._group_prompt` | 非流式 | 0.7 | **1600** | 设定卡精简+计划+摘要+前文窗口+命中提示 | 每短句组 |
| J | 笑点探测器 | `GagInjector.detect` | 非流式 | 0.3 | 400 | ≤500 token（recent 450 字 + 池 4×60） | 每短句组（按频率） |
| K | 语义摘要 | `engine._summarize_chapter` | 非流式 | 0.3 | 1024 | ≤500 token（content 尾 600 字） | 每章 1 次 |
| L | 大纲助手 | `OutlineAgent._parse` | 非流式 | 0.2 | 2048 | 故事线上下文 + 用户指令 | 用户触发 |

### 5.4 flash 坑（关键经验）

> ⚠️ **核心坑**：`deepseek-v4-flash` 先输出 `reasoning_content` 再输出 `content`，两者**共享 max_tokens**。max_tokens 过小（如 700）推理吃光预算 → content 为空（静默返回 ""）。实测同 prompt `700→0 字`、`1600→76 字`。因此所有 max_tokens 都留足推理余量（推理型调用如线程规划 D 取 **16384**，8192 会被推理吃满）；写作路径对空响应重试 2 次（`WRITER_EMPTY_RETRIES`）。

---

## 六、工程实践现状

### 6.1 已完成：优化方案 P0-P3 全部落地（2026-08-04 批次）

- **死代码清理**：旧状态机死函数（step/run/run_full_cycle + 4 个 `_exec_*`）、未用导入、`_api_tasks.json`、scout_debug 调试残留、空壳插件全部删除。
- **保留** `route()`/`execute()`/`Op`/`Phase`（`test_all.py` Phase 9 依赖其返回值，5 次调用）。
- **blueprint 拆分**：`web_ui.py` 约 2000 行 → 35 行壳 + 10 蓝图 / 74 路由。
- **banned_in 修复**：跨书去重加 `book_id` 锚定。
- **路径锚定**：engine 不再依赖 CWD（`safe_paths.py`）。
- **成本补输入**：成本统计不再传空串。
- **预算门控**：`budget_paused` 草稿保留。
- **单一来源收敛**：AI 词表 `de_ai.AI_WORD_MAP`、中文字数 `core/text_utils.count_prose_units`、reviewer 展示文案引用词表单源。
- **日志统一**、`_group_prompt` 双份渲染收敛、详情页草稿渲染、函数默认 `typo_rate=0`。

### 6.2 未移植：show-me-the-story 工程实践（P0-2 ~ P0-7）

> 对应 `docs/archive/项目规划.md` §十 清单，自 08-01 审查起维持"未做"，`core/` 对应模块（inject/writing/prompts/foreshadow/arcs/reconcile/skills）已删除。

| 项 | 设计 | 现状 |
|---|---|---|
| P0-2 Volume 压缩 | 已总结卷压缩为一行摘要 | ❌ 仅 L1 单章语义摘要（最近 5 章注入），无 arc/volume 层 |
| P0-3 大纲长度验证+重试 | 覆盖不足自动重试 | ⚠️ 半做：`_validate`/`_validate_with_llm` 都有，但 issue 只 emit warnings，**无自动重试回路** |
| P0-4 段落级原文引用 | 记忆带原文 snippet | ❌ 只存结构化状态，无原文片段 |
| P0-5 自适应 context budget | fixed+content < usable*0.65 | ❌ `context_budget_tokens` 仅存配置（models.py），**全项目无计算逻辑** |
| P0-6 段落边界尾提取 | 按 `\n\n` 取完整段落 | ❌ `prompt_harness.py:447` 仍 `prev_ending[-150:]` 字符硬截断 |
| P0-7 Config Snapshot | 大纲生成时存 config_snapshot.json | ❌ 全项目零代码 |

---

## 七、质量体系与测试

### 7.1 规则审查（reviewer.py，软门禁）

检查项：字数（≥2000 下限）、AI 词（`AI_WORD_MAP` 单一来源，与 de_ai 共用词表）、段落节奏（每段 ≤200 字）、对话比例（≥15%）、章末钩子。fail 不阻断，修复提示注入下一桥段【上章审查提示】（§4.5）。

### 7.2 去 AI 味（de_ai.py）

`process_rule_based`：AI 高频词随机替换 + 段落节奏；`add_human_imperfections` **默认 typo_rate=0**（去性别替换，防"他→她"错乱）。

### 7.3 免费规则改进：已做 vs 未做（08-05 两份报告）

**已落地（接线类 + 规则类）**：读者承诺台账、写前编辑诊断、hook_points 吸睛点接线、reviewer 软门禁、平台约束接线、摘录库范本、叙事纪律（§4.5-4.9）。

**未落地**：

| 项 | 级别 | 现状 |
|---|---|---|
| 叙事技法映射（情绪→节奏提示） | Tier 1 | ❌ 无【本桥段节奏提示】类注入 |
| 正文禁区扫描（meta-leak：上一章/本章/伏笔/作者安排） | Tier 1 | ❌ 无创作流程词扫描 |
| 场景类型/节奏监测（连续同类型/收局密度） | Tier 1 | ❌ 无 |
| 前十章留存计划 retention_plan | Tier 2 | ❌ 无 |
| 前三章门禁 first3 gate | Tier 2 | ❌ 无 |
| Beat 竞争（关键桥段多角度候选） | Tier 2 | ❌ 无（默认关闭） |
| 桥段结构硬约束 | 免费规则 | ❌ 仍"参考"级，漂移问题依旧 |
| 硬禁句式表 STYLE_BAN_LIST | 免费规则 | ❌ 无（"不是A而是B/破折号/值得一提"） |
| 审阅软门禁→分级 | 免费规则 | ⚠️ 半做：reviewer 已接门禁，但无"重灾区升级禁用表" |
| 对话引号孤行 bug | 工程 | ❌ 实测仍复现 |

### 7.4 有意不做（硬约束内）

整章重写 / 多智能体交叉评审 / 向量库 RAG / 拆书仿写 / loom 式写作指纹 / LLM 编辑润色 / 整章修订——均违反"保持 flash、只动 prompt/免费规则"红线。

### 7.5 测试体系

| 测试 | 内容 | 基线 |
|---|---|---|
| `test_all.py` | 11 个 Phase 无 LLM 健全性（四大库/档案/写作核心统一/线程拆分/叙事纪律/成本/去AI/角色/审查/引擎路由/持久化/规划态） | **94/94** |
| `test_e2e_pages.py` | 端到端页面回归（自动起 58080 服务，`--no-start` 可复用） | ALL CHECKS PASSED |
| `test_chapters.py` / `test_reader.py` | 章节生成 / 番茄解析 | — |
| `tools/test_full_flow.py` | 真实 LLM E2E（约 30 分钟，自建自删测试书） | 手动 |
| `tools/test_outline_quality.py` / `test_world_builder.py` | 大纲质量 / 世界观质量 | 手动 |

---

## 八、UI/UX

### 8.1 页面与蓝图（10 蓝图 / 74 路由 / 24 模板）

| 蓝图 | 主要页面 | 路由数 |
|---|---|---|
| `dashboard.py` | `/` 仪表盘、`/books/start` 新书启动 | 3 |
| `books.py` | `/books` 书库、`/books/<id>` 书详情（含草稿渲染/书名简介按钮）、generate-meta、delete | 4 |
| `storyline.py` | `/storyline/<id>/edit|detail` 故事线编辑器/详情 + 编辑 API（含 `/timeline/<id>` 302 兼容别名） | 19 |
| `desk.py` | `/books/<id>/continue` 写作台（三栏）、SSE 写作端点（write-bridge/write-chapter/step） | 9 |
| `libraries.py` | `/plots` `/structures` `/gags` `/profiles` `/profiles/new` + 启禁删除 API | 12 |
| `publish.py` | `/publish` 发布索引、`/books/<id>/publish` 上架页 + check/mark-finished/export API | 7 |
| `settings.py` | `/settings`（API Key/模型/预算/context_budget 配置 + 测试连接）、任务状态 API | 6 |
| `tools.py` | `/scout` 番茄侦察兵、`/extract` 提取、`/review-test`、`/deai`、`/write` 兼容跳转 | 9 |
| `world_builder.py` | `/books/<id>/world` 世界观设定卡 + generate/candidates/borrow-preview/confirm | 5 |
| `ctx.py` | 共享：全局服务、get_llm、引擎缓存、故事线统一存取、`sse_stream_response` | — |

### 8.2 写作台（三栏布局）

```
┌─────────────┬──────────────────────────┬──────────────┐
│  左：故事线    │        中：连续正文         │  右：生成/规划  │
│  （垂直甘特图） │  （桥段逐组流式写入，滚动）   │  （写作助手）   │
└─────────────┴──────────────────────────┴──────────────┘
```

- 左侧 `story_line.js` 垂直甘特图（数据驱动）：大纲/桥段/笑点·内涵 + 🧵 线程横带 + 逐桥段高亮。
- 中间只放正文（2026-08 布局重构后）；右侧承载生成/规划面板，可折叠。

### 8.3 UX 8 方案（2026-08-05 全部落地）

1. 右栏可折叠 + 空态紧凑
2. 全局 toast（`flashToast` 跨 reload）+ 统一反馈
3. 设计令牌收敛（base.css `:root` CSS 变量 + workspace.css）
4. 单一渲染器（Jinja/JS 桥段卡对齐 + e2e 断言）
5. 信息架构合并（书详情 + 时间线详情，草稿场景保留独立视图）
6. 可读性（字号下限 11px、对比度 `--fg-dim #6e7681`）
7. 核心工作台重排（基础设定折叠面板下移、流程条压缩、高亮呼吸动画）
8. 抓取页精简（置灰 tab）

### 8.4 任务系统（`plugins/task_manager.py`）

- **状态机**：`start → running → cancel → cancelled → done/failed`。
- **核心 API**：`start(task_id,name,title,total)` / `ensure_single(name)`（同工具互斥，新任务替代旧任务）/ `register_cancel` / `cancel` / `is_cancelled` / `progress` / `log` / `done` / `fail` / `get_tasks` / `clear_old(keep_seconds=60)` / `remove`。任务卡片与日志各自独立更新；每个任务最多保留 100 条日志。
- **端点**（settings 蓝图）：`GET /api/status/tasks`（前端 pollStatus 每 2s 轮询）、`POST /api/status/tasks/close`（关闭卡片，仅 UI 不杀进程）。
- **前端规范**：卡片（卡头+进度条仅 running+卡底阶段/时间，按开始时间新→旧）；日志（增量追加、颜色区分工具、刷新后丢失）；关闭=取消（运行中 kill 线程，worker 在检查点 `is_cancelled()` 优雅停止）。
- **状态栏规范**（ui-notes 合并）：每工具单任务互斥；日志统一进右侧状态栏（showAlert/showToast 双通道已移除）；同工具替代时旧日志自动清除（data-task-id 标记）；body 固定 `height:100vh`，main/aside 内部滚动。

---

## 九、技术决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.10+ | 熟练、生态成熟、LLM 集成方便 |
| LLM | DeepSeek API（`deepseek-v4-flash`） | 低成本 + 中文能力强 + 推理型 |
| Web 框架 | 纯 Flask 3 + Jinja2（无 FastAPI） | 收敛后无需异步 API；蓝图拆分后 web_ui.py 仅 35 行壳 |
| 蓝图 | 10 个 `ui/web_blueprints/*.py` | 按域拆分 |
| LLM 客户端 | 单例 `ctx.get_llm()` | 从 api.json 读 APIConfig，懒加载 |
| 存储 | JSON 文件系统（每书一目录） | 初期无需数据库，后期可迁移 |
| 规划态书 | 即正式书目录 | 草稿目录 tl_* 已废弃，规划中=书目录内 phase 未 ready |
| 外部采集 | SSR + PUA 解码 | 番茄搜索 API 失效，走前端渲染 |
| 运行环境 | Windows | 番茄爬虫依赖 Windows（WSL2 无法连） |
| 任务管理 | threading + Event + 内存任务表 | 轻量协作式取消，前端 2s 轮询 |
| 匹配策略 | 免费规则（候选池预筛 / 兼容映射） | 无额外 LLM 调用，低延迟 |

---

## 十、运行与测试 · 工程约定

### 10.1 启动/重启

```bash
python ui/web_ui.py      # 127.0.0.1:58080；NOVEL_DEBUG=1 开启 debug
```

**重启必须杀净 58080 LISTENING 全部 PID**：`netstat -ano | findstr :58080`，否则旧进程"改动不生效"假象。

### 10.2 测试

```bash
python test_all.py       # 94/94 通过（无 LLM 健全性，含 Phase 3/3.5/3.6/9/11）
python test_e2e_pages.py # 端到端页面回归
python test_chapters.py / test_reader.py
```

### 10.3 工程约定与坑

- **Git**：工作分支 `dev`，`master` 为历史；远程 origin → `github.com/xsneser/fiction-factory.git`（URL 内嵌 ghp_ token **会过期**）；仓库级代理 `http://127.0.0.1:7897`；提交用中文 conventional 风格（feat/fix/refactor + 简述），末尾 `Co-Authored-By: Claude`；`books/`、`profiles/`、`api.json`、`storage/` 不入库。
- **四大库单例**：改 `libraries/data/*.json` 必须重启服务才生效。
- **Windows 控制台 GBK**：脚本打印中文加 `sys.stdout.reconfigure(encoding="utf-8")`。
- **flash max_tokens 余量**：任何新 LLM 调用，max_tokens 必须大于"纯正文+推理"之和（写作 1600 / 摘要·书名 1024 / 探测器 400）。
- **测试会动数据**：`test_full_flow.py` 自建自删测试书；`books/`、`profiles/` 是真实数据，别删。
- **删除类操作无回收站**：delete_book / storyline_delete 前先确认。
- **合规**：番茄侦察兵仅限个人学习研究（SSR 采集 + PUA 解码），禁商业用途/大量下载传播正文；README 有完整声明。

---

## 十一、数据现状（2026-08-17）

- `books/book_001`：都市/枫落，规划完成（storyline phase=ready，2 大纲 / 29 桥段 / 2 母题），0 章已写。
- `books/book_003`：都市/枫落，phase=config（仅建书，未生成故事线）。
- `profiles/`：profile_001~005（预设 枫落/夜雨/青衫 + 后续新增）。
- 无运行中的 58080 服务。

---

## 十二、开放决策与余量

1. **旧状态机 route()/execute()/Op/Phase 是否整体下线**：仍开放。`test_all.py` Phase 9 依赖 route() 返回值，删则需同步改写测试；推荐保留。
2. **StorylineBuilder 全量迁移到 OutlineGenerator**：只做了抽公共函数止血（structure_to_stages/mount_themes_and_hooks），全量迁移留待评估。
3. **错别字检测仅 1 条规则**（`_TYPO_PATTERNS` 只有「先轻轻」）：框架可扩展，待实测积累。
4. **跨书污染**：已审计无泄漏路径（无模块级可变共享），判为模型幻觉，保持观察。
5. **语义摘要只有 L1**：无 L2/L3 chunk/arc 聚合，卷级后记忆断层风险。
6. **context_budget_tokens 无计算逻辑**：仅配置 + 设置页可编辑，超长书 context overflow 风险。

---

## 十三、路线图（未完成项与开放决策）

> 全部来自既有文档的待办/建议；按性价比排序。

### 13.1 首选待办（改动小、收益直接）

1. **P0-6 段落边界尾提取 + P0-3 自动重试**：`prev_ending[-150:]` 改按 `\n\n` 取完整段落；`_validate` 命中"桥段覆盖不足"时对 Phase 3 追加一次重试。
2. **修复引号孤行 bug**（实测复现，10 行内）：`_split_sentences` 的 lookbehind 增加 `"」’』` 收尾。
3. **补硬禁句式表 STYLE_BAN_LIST**（"不是A而是B/破折号/值得一提"等，免费规则）。
4. **桥段结构硬约束**（治实测结构漂移）：【桥段骨架】从"参考"升级为"本桥段唯一事件顺序"，短句组完成后对照步进，漂移注入重写提示。
5. **可选**：正文禁区扫描（复用 WRITER 重试通道，纯规则）；first3 门禁（轻量调用 1 次）。

### 13.2 远期（项目规划 Phase 4/5）

- **Phase 4 质量体系**：全书优化诊断管线（reconcile.py 已删，需重建或放弃）、段落级修订 + diff 追踪、设定协调（改设定后自动调和章节）、审查规则库扩充（当前 reviewer 5 项）。
- **Phase 5 批量生产**：队列式章节自动生产（多书并发定时）、AI 助理 Agent（tool-calling loop，现有 OutlineAgent 是雏形）、多平台发布适配器（publisher.py 已做上架检查 + 手动导出，自动发布未做）、发布统计面板（publish 页面已有基础）、PyInstaller 单文件打包。

### 13.3 文档回写清单

- `docs/archive/项目规划.md` 仍描述 v0.5 架构，作为历史归档保留；本文档为唯一技术权威。
- 改代码必须同步本文档。

---

## 十四、文档索引与参考项目

### 14.1 当前文档（docs/ 顶层）

docs/ 顶层仅保留本文档（唯一主设计文档）与 `archive/`（全部历史文档归档）：

| 文档 | 定位 |
|---|---|
| `设计文档-总览-claude.md` | **唯一主设计文档**（本文档） |
| `archive/` | 全部已合并/历史文档（设计稿、交接、优化、UX、任务系统、调研、审查报告等 19 份） |

### 14.2 归档文档（docs/archive/）

| 文档 | 原定位 | 关键内容去向 |
|---|---|---|
| `项目规划.md` (v0.6) | 主设计稿 | 定位/路线图 → §一/§十三；show-me-the-story 清单 → §六 |
| `交接文档.md` | 通用交接 | 运行/坑/待办 → §十/§十二 |
| `harness重构交接文档.md` | harness 设计史 | 设计动机（笑点涌现/集中式 prompt）→ §一/§四 |
| `新书创建-Harness架构与LLM提示词.md` | 当前代码事实 | Prompt A~L → §五；数据模型 → §三 |
| `优化方案-2026-08-04.md` | 优化母方案 | P0-P3 落地结果 → §六 |
| `优化方案核对-2026-08-04.md` | 方案核对 | 结论 → §六 |
| `待codex处理-2026-08-04.md` | codex 待办 | 全部落地 → §六 |
| `UX报告-2026-08-05.md` | UX 方案 | 8 方案 → §八 |
| `task-system-spec.md` | 任务系统规范 | 状态机 → §八.4 |
| `ui-notes.md` | UI 改进记录 | 已全部完成 → §八.4 |
| `novel-factory-timeline.html` | 时间线可视化设计稿 | 已实现为 story_line.js → §八.2 |
| `设计文档.md` | 另一会话合并版 | v1.1 已并入并退役 |
| `开源调研.md` | 早期开源项目架构调研（07-28） | 竞品全景 → §14.3 |
| `审查报告-设计与实现差异.md` | 08-01 设计-实现差异审查 | 已被 08-05 核对报告取代 |
| `人类创作思考路线-开源调研与方案.md` | 创作思考路线 + Tier 落地方案（08-05） | 免费规则改进待办 → §7.3/§13 |
| `写作质量对比-开源项目-2026-08-05.md` | 质量对比 + §六 改进清单（08-05） | 改进清单 → §7.3/§13 |
| `网文开头处理-开源调研.md` | 网文开头处理调研（08-05） | 炸裂开场 → §4.4 |
| `审查报告-完成度核对-2026-08-05.md` | 完成度核对审查 | 本文档的**事实基线** |
| `AGENTS_LEGACY.md` | 参考项目 show-me-the-story 的 Go AGENTS.md | 与 NovelEngine 无关，仅存档 |

### 14.3 参考项目

- **[Nigh/show-me-the-story](https://github.com/Nigh/show-me-the-story)** — 核心架构参考（早期 core/ 为 Go→Python 移植）；工程实践清单见 §6.2。
- **[qiuxinyuan321/novel-writer-master](https://github.com/qiuxinyuan321/novel-writer-master)** — 流式 UI + AI 降重灵感。
- 写作质量对比（9 家竞品）：`docs/archive/写作质量对比-开源项目-2026-08-05.md`。
- 人类创作思考路线（11 步认知模型 + 分级落地方案）：`docs/archive/人类创作思考路线-开源调研与方案.md`。
- 架构向调研：`docs/archive/开源调研.md`；开头向调研：`docs/archive/网文开头处理-开源调研.md`。

> *"从 show-me-the-story 的架构思想出发，走向真正的网文工业量产。"*
