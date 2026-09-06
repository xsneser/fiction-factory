# NovelEngine — AI 小说工厂

> **全自动网文量产系统**：AI 拟人写作 × 情节段驱动生成 × 侦察兵采集 × 人工审查入库

---

## 核心理念

每个笔名 = 一个独立的 AI 作家，有自己的记忆、风格、情节段库和创作习惯。

不是"一个生成器生成多本书"，而是"一群 AI 作家同时开工"。

---

## 功能概览

| 模块 | 说明 | 状态 |
|------|------|------|
| **引擎** (`libraries/engine.py`) | 新书启动 → 规划 → 逐章续写，全自动闭环（双写通道：情节段驱动 / 通用） | ✅ |
| **情节段写作** (`libraries/storyline_writer.py`) | 唯一写作核心：情节段驱动逐短句组增量生成 + 炸裂开场 | ✅ |
| **情节段库** (`libraries/plot.py` + `data/plots.jsonl`) | 网文经典情节段结构化模板，写作时按场景匹配注入 | ✅ |
| **情节弧库** (`libraries/structure.py` + `data/structures.jsonl`) | 各题材方向弧模板；**平级独立弧**（每行一弧，无父子层级，自带 tags/描述） | ✅ |
| **笑点库** (`libraries/gag.py` + `gag_injector.py`) | 搞笑模式模板 + 探测器实时涌现注入 | ✅ |
| **角色库** (`libraries/character.py` + `data/characters.jsonl`) | 性格原型 + 代表人物，设定表单「从原型库选」一键填充 | ✅ |
| **风格规则库** (`libraries/style_rules.py` + `data/style_rules.jsonl`) | `prefer`（句式偏好）/ `ban`（禁用+`replacements` 去 AI 味替换），按笔名归属 | ✅ 新 |
| **内涵系统** | 母题跟随大纲阶段，阶段级 `themes` 带插入位置，写作 prompt 注入 | ✅ |
| **笔名档案** (`libraries/profiles.py`) | 风格指纹 + prompt 注入；风格规则在笔名页维护 | ✅ |
| **Agent 助手** (`libraries/dsh_bridge.py` + `vendor/dsh-ne/`) | 侧栏对话（dsh-ne headless 驱动），MCP 工具全链路操作 + 工具调用日志页签 | ✅ |
| **Agent 侧车技能** (`agent-sidecar/skills/`) | novel-build / novel-scout / novel-publish / novel-story 等 5 技能，供外部 agent 编排 | ✅ 新 |
| **番茄侦察兵** (`plugins/fanqie_scout.py` 等) | 热榜侦察 / 搜索 / 下载 / 顺序通读式提取五类资产 | ✅ |
| **提取审查流** (`/extract` + `extract_judge.py`) | 候选先进暂存区，**用户勾选确认后才入库**；闸门判定结构/重复/书级专用 | ✅ 新 |
| **AI 降重** (`libraries/de_ai.py`) | 续写流程中的 AI 痕迹消除（联动风格规则 `replacements`） | ✅ |
| **审阅** (`libraries/reviewer.py`) | 自动审阅质量打分 | ✅ |
| **Flask Web UI** | 蓝图化：仪表盘 / 新书向导 / 写作台 / 五大库页 / 侦察 / 提取审查 | ✅ |
| **矩阵发布** | 多平台多账号批量分发 | 📋 规划中 |

> **五大资产库统一 JSONL 存储**（`libraries/data/*.jsonl`，一行一条）：`plots` / `structures` / `gags` / `characters` / `style_rules`。旧单 JSON 首次加载自动迁移。

---

## 快速开始

### 方式一：一键启动（推荐）

**Windows**：双击 `launch.bat`
**Linux/WSL2**：`bash launch.sh`

脚本自动完成：Python 检测 → API 配置检查 → 依赖安装 → 启动 Web 面板

浏览器自动打开 `http://localhost:58080`

### 方式二：手动启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API
cp api.example.json api.json
# 编辑 api.json 填入你的 DeepSeek API Key

# 3. 启动
python ui/web_ui.py     # Web 管理面板（主界面，端口 58080）
```

### Agent 助手（内置 + MCP）

**右侧栏内置 Agent 聊天助手**（OpenClaw 式）：启动 Web 面板后在右侧栏直接对话，内置 Agent 通过原生 function calling 操作整个创作引擎——建书、世界观、大纲、写作、审查去 AI、上架导出，还能切页导航、控制写作台故事线画布。试试：「创建一本都市爽文 by 枫落」「给 book_001 生成完整大纲」「打开书库页面」。

面板顶部有 **「💬 对话 / 🔧 工具日志」两个页签**：切到工具日志可实时看到 Agent 调用了哪些工具（工具名/时间/成败/耗时/参数/结果摘要，3 秒自动刷新），一目了然每个步骤在干什么。

**MCP 接口**：全部注册工具暴露为 MCP（**39 个**，数量以 `tools/mcp_smoke.py` `EXPECT_MCP_TOOLS` 为准；护栏：直建/直删工具不存在，建书走系统向导、删书走书库页手动），供 Claude Code 等外部 Agent 驱动：

```bash
claude mcp add --scope project novel-engine -- python mcp_server.py
claude mcp call novel-engine get_book_state book_id=book_001   # 只读试调用
```

> MCP 为独立 stdio 进程，与 Web 服务并存（共享 `books/` 文件，勿同时操作同一本书）。详见主设计文档。

---

## 引擎核心流程

引擎支持双模式，由 `libraries/engine.py` 驱动：

### 新书启动（单页 3 步向导）

`/books/start` 为横条步骤条向导：**一句话设定（含题材标签）→ 挑选世界观（受题材标签约束，可借鉴）→ 世界观补全（自动 AI 补全 12 维 + 基调，可编辑；提交即入库跳书详情）**。

```
一句话设定（含题材标签）→ 挑选世界观 → 世界观补全（12维+基调）→ 角色 → 建书（入库）→ 大纲 → 写作台
```

- 题材标签在步 1 选择（预置 **50 标签 5 组** `libraries/world_tags.py`），作为世界观/大纲/写作 prompt 的硬约束。
- 步 3 提供**分阶段内容构建**（顶部状态区 5 徽标实时显示 ✅/未填）：`generate_core_conflict`（核心矛盾）→ `query_arc_library`/`query_plots` + `set_picks`（开篇大纲+情节段）→ `generate_factions`（势力）→ `generate_characters`（主要人物，从角色库/原型生成）→ `generate_rest_world`（其余维度）。
- 角色经 `drive_ui(set_characters)` 填入步 3，可手动编辑；书名可改。

### 续写循环

```
写 → 审 → 去AI → 修正 → 继续写
```

每个章节由**情节段写作**（`storyline_writer.py`）逐情节段、逐短句组增量生成：
- 情节段是生成单元：每个情节段按短句组流式续写，累计满 `words_per_chapter` 自动切章。
- 第 1 章前 800 字 / 前 3 情节段强制"炸裂开场"（番茄式冷开场：前三句不铺垫、前 200 字钩子）。
- 写完后：灵机一动探测器注入笑点 → 章节语义摘要 → 第 1 章写完自动生成书名/简介。
- 角色状态 / 伏笔（`promise_ledger`）/ 连续性（`continuity`）跨章跟踪，写入上下文。

---

## 侦察兵与提取（外部书库 → 五库）

### 采集（`plugins/`）

| 功能 | 方式 | 说明 |
|------|------|------|
| **热榜** | `hot_ranks.py` | 多平台榜单聚合（番茄在线；起点/晋江 UI 已预留） |
| **搜索** | Bing 搜索 → SSR 页面解析 | 番茄搜索 API 已挂，走搜索引擎转跳 |
| **下载** | Reader 页面 SSR → PUA 字体解码 | `font_decoder.py` + fonttools（内置映射表，支持逐本动态解码） |
| **抓取** | `webnovel_scraper.py` / `book_fetch.py` | 章节分卷抓取，进度实时（`crawl_progress.py`） |

### 提取入库（`/extract` 审查流，2026-09 重构）

**原则：任何候选未经用户确认不得入库。**

```
下载参考书 → agent 顺序通读（分窗读，五类随手提炼）
  → 候选进 /extract 暂存区（drive_ui(set_review)，可持续补充/修改）
  → 用户勾选 → 页面 POST /api/scout/ingest 落库（agent 无权直调 ingest）
```

- **五类资产**：情节段 / 弧 / 笑点 / 角色 + 写作风格规则（风格归属笔名，`prefer` 3–8 条 + `ban` 5–15 条）。
- **断点续读**：上下文变重时 `extract_state` 把已读前缀压缩进记忆落盘（`storage/extract_work/`），同一任务继续读下一窗，长书不中断。
- **入库闸门** `extract_judge.py`：结构完整（情节段需 `structure`+`slots`、弧需可复用 `description`…）、库内近似查重（bigram + `_mechanism_key` 骨架键）、书级专用候选记 `book_archive` 不进库。
- **进度实时**：`extract_progress.py` 与 dsh 桥事件联动，进度卡实时显示当前读到哪。

---

## 五大资产库

### 情节段库 —— `libraries/plot.py`（当前 64 条）

按场景分类（爽文/开篇/战斗/成长/冲突/情感…），写作时由 `assembler` 按活跃情节段匹配注入，支持嵌套。

### 情节弧库 —— `libraries/structure.py`（当前 9 棵根弧，扁平节点行）

题材方向弧骨架。**平级独立弧存储**：每个弧（整段壳大弧与几章的小弧都可收）统一字段（`id/name/description/min_words/max_words/key_events/foreshadow_opportunities/tags/…`，**不再含 themes 内涵字段**），**无父子层级**、每弧自带 tags/描述。旧分层（原树各层节点）迁移时各自成为独立弧，tags 平铺到每弧。

### 笑点库 —— `libraries/gag.py`（当前 34 条）

分类：吐槽、误会、打脸、反差、卖萌、装逼、自黑、神逻辑。笑点完全涌现——不写进大纲，写作时由探测器实时判断注入。

### 角色库 —— `libraries/character.py`（当前 22 条）

性格原型 + 代表人物（高冷毒舌/沙雕谐星/温柔治愈/热血莽夫/腹黑军师/傲娇大小姐/忠犬伙伴/阴险反派…），带口癖示例与适配标签。设定表单里每张人物卡可「🎭 从原型库选」一键填充性格/口癖/简介。

### 风格规则库 —— `style_rules.jsonl`（当前 42 条：ban 32 + prefer 10）

按笔名归属：`prefer` 句式偏好注入写作 prompt；`ban` 禁用词/套话，带 `replacements` 时触发去 AI 味替换（空=硬禁）。在笔名档案页（`profiles.html`）维护。

> 五库数据存 `libraries/data/{plots,structures,gags,characters,style_rules}.jsonl`（JSONL 一行一条；旧单 JSON 首次加载自动迁移）。数量为运行时数据，随侦察/提取持续增长。

---

## 笔名风格档案

每个笔名拥有独立的风格指纹（`libraries/profiles.py`）：

```python
{
    "pen_name": "枫落",
    "word_print": {
        "common_words": ["卧槽", "淦", "牛逼"],   # 常用词
        "avoid_words": ["仿佛", "似乎", "不禁"],  # 禁用词
        "dialogue_tags": ["说", "道", "笑了"],    # 对话标签偏好
        "action_beats": ["眯眼", "挑眉", "咂嘴"],  # 动作节拍
    },
    "style_fingerprint": {
        "sentence_length": "short",          # 短句为主
        "humor_style": "吐槽型",
        "action_style": "简洁利落",
    },
}
```

预设笔名：**枫落**（都市爽文）、**夜雨**（玄幻正剧）、**青衫**（言情甜文）。每个笔名还挂有自己的风格规则库（见上，从提取的参考书归纳）。

---

## 目录结构

```
D:\NovelEngine\
├── launch.bat / launch.sh   # 一键启动脚本
│
├── libraries/               # 核心业务逻辑
│   ├── engine.py            # 引擎（新书/续写双模式总调度，Op 指令分发）
│   ├── storyline.py         # 故事线数据模型（角色统一 characters）+ StorylineBuilder
│   ├── storyline_writer.py  # 情节段驱动逐章增量写作（唯一写作核心）
│   ├── outline_generator.py # 大纲 LLM 管线
│   ├── outline_agent.py     # 大纲助手（自然语言调整故事线）
│   ├── prompt_harness.py    # 集中式 prompt（书级设定卡 + 场景渲染器）
│   ├── world_builder.py     # 世界观/设定生成器
│   ├── book_manager.py / book_meta.py / book_lock.py / book_snapshot.py
│   ├── profiles.py          # 笔名风格档案
│   ├── de_ai.py             # AI 降重（联动风格规则 replacements）
│   ├── reviewer.py          # 审阅模块
│   ├── publisher.py         # 上架检查/状态机
│   ├── assembler.py         # 情节段装配（活跃情节段匹配/嵌套 → 注入写作）
│   ├── gag_injector.py      # 笑点探测器/注入器
│   ├── promise_ledger.py    # 伏笔账本（跨章跟踪兑现）
│   ├── continuity.py        # 连续性检查（前后文一致）
│   ├── character_state.py   # 角色状态跟踪
│   ├── retention.py / loop_guard.py / build_status.py / tag_generator.py
│   ├── world_tags.py        # 预置题材标签库（50 标签 + 题材方向推导）
│   ├── base_library.py      # 资产库基类（JSONL 单例 + 读写）
│   ├── plot.py / structure.py / gag.py / character.py  # 四类资产库
│   ├── style_rules.py / style_assets.py / style_ban.py # 风格规则体系
│   ├── extract_state.py     # 整本扫读断点记忆（storage/extract_work/）
│   ├── extract_judge.py     # 入库判断闸门（结构/重复/书级专用）
│   ├── extract_progress.py  # 提取进度实时共享（storage/extract_progress.json）
│   ├── crawl_progress.py    # 抓取进度实时共享
│   ├── dsh_bridge.py        # Agent 侧车桥（dsh 工具事件 → 进度卡/日志）
│   ├── tool_log.py / tool_policy.py / nav_intent.py  # Agent 工具治理与导航
│   ├── token_proxy.py / cost_tracker.py  # API 代理与费用追踪
│   └── data/                # 五库 JSONL（plots/structures/gags/characters/style_rules）
│
├── core/                    # LLM 基础设施
│   ├── llm_client.py        # API 调用封装
│   ├── models.py            # APIConfig 数据模型
│   ├── json_store.py        # JSON/JSONL 原子读写
│   ├── safe_paths.py        # 路径安全（防越界）
│   └── text_utils.py        # 文本工具（字数统计）
│
├── plugins/                 # 外部采集插件
│   ├── fanqie_scout.py      # 番茄侦察（搜索/下载编排）
│   ├── hot_ranks.py         # 多平台热榜聚合
│   ├── webnovel_scraper.py  # SSR 抓取器
│   ├── book_fetch.py        # 按书名/book_id 综合抓取
│   ├── browser_render.py    # 无头浏览器兜底渲染
│   ├── site_clean_rules.py  # 站点反爬规则
│   ├── font_decoder.py      # PUA 字体解码（font_charset.json 映射表）
│   ├── novel_storage.py     # 已下载小说管理
│   ├── style_analyzer.py    # 写作风格分析
│   └── task_manager.py      # 全局任务管理器
│
├── ui/                      # Web 用户界面
│   ├── web_ui.py            # Flask 入口（58080）
│   ├── web_blueprints/      # 按域拆分的蓝图（10 个）
│   │   ├── dashboard.py     # 仪表盘
│   │   ├── books.py         # 图书列表/详情
│   │   ├── storyline.py     # 写作台 / 故事线画布
│   │   ├── desk.py          # 写作/续写面板
│   │   ├── libraries.py     # 五大库页
│   │   ├── world_builder.py # 世界观补全 API
│   │   ├── tools.py         # 侦察/提取/工具 API
│   │   ├── agent.py         # Agent 对话 + 工具日志
│   │   ├── settings.py / publish.py
│   │   └── ctx.py           # 上下文辅助
│   ├── templates/           # Jinja2（26 个）
│   │   ├── base.html / dashboard.html / books.html / book_detail.html
│   │   ├── start_book.html  # 新书向导（3 步）
│   │   ├── storyline_write_flow.html  # 写作台
│   │   ├── scout.html       # 侦察·外部书库（热榜/下载/阅读入口）
│   │   ├── extract.html     # 提取审查（五库候选暂存区 + 勾选入库）
│   │   ├── novels.html / novel_reader.html
│   │   ├── plots.html / structures.html / gags.html / characters.html
│   │   ├── profiles.html / settings.html / publish.html / publish_index.html
│   │   ├── review_test.html / deai.html
│   │   └── _*.html          # partials（_flow_stepper/_info_bar/_world_edit_form/
│   │                         #   _profile_form/_style_rules_cards/_book_runtime_panels）
│   └── static/              # css（base/story_line）+ js（base/story_line/agent_panel/library_review）
│
├── agent_tools.py           # 共享 Agent 工具注册表（39 工具暴露 MCP，全链路）
├── mcp_server.py            # MCP 适配层（stdio，claude mcp add 接入）
├── agent-sidecar/           # 侧车技能包（供外部 agent 编排）
│   └── skills/              # novel-build / novel-build-candidates / novel-scout /
│                            #   novel-publish / novel-story（SKILL.md）
├── vendor/dsh-ne/           # vendored dsh 精简核心（node_modules 不入库，npm install 重建）
│
├── books/                   # 图书数据（.gitignore）
│   └── {book_id}/
│       ├── book.json        # 图书配置
│       ├── chapters/        # 章节 JSON
│       ├── outline/         # 大纲
│       ├── storyline.json   # 故事线配置
│       ├── character_states.json
│       └── export/          # 导出投稿包
│
├── profiles/                # 笔名档案 JSON（.gitignore）
├── storage/                 # 运行时缓存/断点（.gitignore）
├── api.json                 # API 配置（.gitignore）
├── api.example.json         # 配置模板
│
├── test_all.py              # 全模块集成测试（123 断言，无 LLM）
├── test_chapters.py / test_e2e_pages.py / test_reader.py
├── tools/                   # 运维/专项脚本（mcp_smoke / simulate_full_flow /
│                            #   verify_storyline_upgrade / test_picks_contract…）
├── docs/                    # 9 篇设计/研究文档
│   ├── 设计文档-总览-claude.md            # 唯一主设计文档
│   ├── 设计文档-外部书目提取-顺序通读式提取.md
│   ├── 设计文档-多平台热榜接口.md
│   ├── 架构文档-内置agent-dsh.md / 架构总览.md
│   └── 研究文档-*.md
├── requirements.txt
└── LICENSE
```

---

## 参考项目

NovelEngine 的开发参考了以下开源项目：

### [Nigh/show-me-the-story](https://github.com/Nigh/show-me-the-story) ⭐ 398+

**核心架构参考。** 早期的 `core/` 模块（LLM 客户端、写作引擎、伏笔系统、卷弧管理等）曾是对 show-me-the-story Go 架构的 Python 移植，其流程设计是 NovelEngine 的架构基石。现已收敛为纯 LLM 基础设施层（客户端/配置/文本工具），业务全部由 `libraries/` 的 v2 引擎承载。

### [qiuxinyuan321/novel-writer-master](https://github.com/qiuxinyuan321/novel-writer-master)

**流式 UI + AI 降重参考。** AI 降重模块 `de_ai.py` 的思路受其启发。

> *"从 show-me-the-story 的架构思想出发，走向真正的网文工业量产。"*

---

## 技术栈

- **语言**: Python 3.10+
- **LLM**: DeepSeek API（兼容 OpenAI 格式）
- **存储**: JSON 文件系统 + JSONL 资产库
- **Web 面板**: Flask + Jinja2 + 蓝图（58080）

---

## ⚠️ 合规声明（番茄侦察兵）

`plugins/fanqie_scout.py` 及配套的 `font_decoder.py` 仅供**个人学习、研究网文结构技巧**使用。使用前请注意：

- 番茄小说等内容平台的服务协议普遍禁止自动化数据采集，请勿用于商业用途或大规模抓取
- 请勿大量下载并二次传播受著作权保护的正文内容；分析应以「模式/结构/情节段」等抽象技巧为主，避免全文存储与转载
- PUA 字体解码属于对技术保护措施的绕过，请仅用于个人学习研究
- 爬虫模块默认开启 TLS 证书校验（`verify=True`）；如遇旧证书环境可显式传 `verify=False`
- 使用本模块产生的任何法律风险由使用者自行承担

## 许可证

MIT
