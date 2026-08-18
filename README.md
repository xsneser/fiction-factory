# NovelEngine — AI 小说工厂

> **全自动网文量产系统**：AI 拟人写作 × 桥段驱动生成 × 番茄侦察兵

---

## 核心理念

每个笔名 = 一个独立的 AI 作家，有自己的记忆、风格、桥段库和创作习惯。

不是"一个生成器生成多本书"，而是"一群 AI 作家同时开工"。

---

## 功能概览

| 模块 | 说明 | 状态 |
|------|------|------|
| **引擎** (`libraries/engine.py`) | 新书启动 → 规划 → 逐章续写，全自动闭环 | ✅ v0.5 |
| **桥段写作** (`libraries/storyline_writer.py`) | 唯一写作核心：桥段驱动逐短句组增量生成 + 炸裂开场 | ✅ 新 |
| **桥段库** (`libraries/plot.py`) | 网文经典桥段的结构化模板（12个内置） | ✅ |
| **大纲库** (`libraries/structure.py`) | 各流派的卷/弧/章骨架（5个内置） | ✅ |
| **笑点库** (`libraries/gag.py`) | 搞笑模式模板 + 例句（10个内置） | ✅ |
| **摘录库** (`libraries/example_lib.py`) | 真实网文原文摘录范本（写作时注入【写法范本】块借鉴语感） | ✅ 新 |
| **笔名档案** (`libraries/profiles.py`) | 风格指纹 + prompt 注入 | ✅ |
| **番茄侦察兵** (`plugins/fanqie_scout.py`) | 番茄小说搜索/下载/分析/PUA解码 | ✅ 新 |
| **AI 降重** (`libraries/de_ai.py`) | 续写流程中的 AI 痕迹消除 | ✅ |
| **审阅** (`libraries/reviewer.py`) | 自动审阅质量打分 | ✅ |
| **Flask Web UI** | 新书流程 + 续写面板 + 侦察兵 + 仪表盘 | ✅ v0.4 |
| **矩阵发布** | 多平台多账号批量分发 | 📋 规划中 |

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

**MCP 接口**：同一套 29 个工具也暴露为 MCP，供 Claude Code 等外部 Agent 驱动：

```bash
claude mcp add --scope project novel-engine -- python mcp_server.py
claude mcp call novel-engine get_book_state book_id=book_001   # 只读试调用
```

> MCP 为独立 stdio 进程，与 Web 服务并存（共享 `books/` 文件，勿同时操作同一本书）。详见主设计文档 §8.5。

---

## 引擎核心流程

引擎支持双模式，由 `libraries/engine.py` 驱动：

### 新书启动

```
规划 → CH1（钩子 + 金手指）→ CH2（世界观展开）
→ CH3（冲突引入）→ 生成书名 → 自动转入续写
```

### 续写循环

```
写 → 审 → 去AI → 修正 → 继续写
```

每个章节由**桥段写作**（`storyline_writer.py`，唯一写作核心）逐桥段、逐短句组增量生成：
- 桥段是生成单元：每个桥段按短句组流式续写，累计满 `words_per_chapter` 自动切章。
- 第 1 章前 800 字 / 前 3 桥段强制"炸裂开场"（番茄式冷开场：前三句不铺垫、前 200 字钩子）。
- 写完后：灵机一动探测器注入笑点 → 章节语义摘要 → 第 1 章写完自动生成书名/简介。

---

## 番茄侦察兵

`plugins/fanqie_scout.py` 提供番茄小说数据采集三件套：

| 功能 | 方式 | 说明 |
|------|------|------|
| **搜索** | Bing 搜索 → SSR 页面解析 | 番茄搜索 API 已全部挂掉，走搜索引擎 |
| **下载** | Reader 页面 SSR → PUA 字体解码 | 使用 `font_decoder.py` + fonttools |
| **分析** | 4 次 LLM 调用 → 入库各库 | 桥段/大纲/笑点/摘录库自动填充 |

PUA 字体解码器 `plugins/font_decoder.py` 内置 362 条映射表，支持逐本小说字体动态解码。

---

## 四大核心库

### 桥段库 —— `libraries/plot.py`

12 模板 × 7 分类：

- 爽文：退婚打脸、拍卖会捡漏、装逼打脸连环套、扮猪吃虎日常
- 开篇：穿越/重生开局、获得金手指/系统激活
- 战斗：擂台/比武大会、闯关/秘境探险
- 成长：拜师/拜入门派
- 冲突：宗门/家族危机
- 情感：英雄救美、修罗场/情感博弈

### 大纲库 —— `libraries/structure.py`

5 套流派模板，覆盖卷/弧/章三级骨架：玄幻、都市、悬疑、言情、穿越。

### 笑点库 —— `libraries/gag.py`

10 模式 × 8 分类：吐槽、误会、打脸、反差、卖萌、装逼、自黑、神逻辑。

### 摘录库 —— `libraries/example_lib.py`

真实网文原文摘录范本，7 类型（开头钩子/主角亮相/高张力对白/打脸爽点/章末钩子/结尾余韵/日常对话）。桥段开写前按分类预筛 2 条注入【写法范本】块，让模型借鉴真实句子的节奏/画面/语气（治「没味儿」）；内容由番茄侦察兵从下载书切取，内置 16 条演示范本。

---

## 笔名风格档案

每个笔名拥有独立的风格指纹：

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

预设笔名：**枫落**（都市爽文）、**夜雨**（玄幻正剧）、**青衫**（言情甜文）

---

## 目录结构

```
D:\NovelEngine/
├── launch.bat / launch.sh  # 一键启动脚本
│
├── libraries/              # 核心业务逻辑
│   ├── engine.py           # 引擎（新书/续写双模式）
│   ├── book_meta.py        # 书名/简介生成（纯函数）
│   ├── outline_generator.py# 故事线大纲生成（5阶段管线）
│   ├── storyline.py        # 故事线数据模型 + StorylineBuilder
│   ├── storyline_writer.py # 桥段驱动的逐章增量写作
│   ├── assembler.py        # 书籍组装器（库材料→写作计划）
│   ├── outline_agent.py    # 大纲助手（自然语言调整故事线）
│   ├── base_library.py     # 四大库共用基类（单例+读写）
│   ├── prompt_harness.py   # 集中式 prompt（书级设定卡+渲染器）
│   ├── book_manager.py     # 图书管理器
│   ├── profiles.py         # 笔名风格档案
│   ├── de_ai.py            # AI 降重
│   ├── reviewer.py         # 审阅模块
│   ├── cost_tracker.py     # API 费用追踪
│   ├── character_state.py  # 角色状态跟踪
│   ├── reset_data.py       # 一键重置四大库
│   ├── plot.py             # 桥段库（12模板）
│   ├── structure.py        # 大纲库（5模板）
│   ├── gag.py              # 笑点库（10模式）
│   └── example_lib.py      # 摘录库（真实范本，7类型）
│
├── core/                   # LLM 基础设施（v2 引擎共用）
│   ├── llm_client.py       # API 调用封装
│   ├── models.py           # APIConfig 数据模型
│   └── text_utils.py       # 文本工具（字数统计）
│
├── plugins/                # 外部采集插件
│   ├── fanqie_scout.py     # 番茄侦察兵（搜/下/析）
│   ├── font_decoder.py     # PUA 字体解码器
│   ├── novel_storage.py    # 已下载小说管理
│   ├── style_analyzer.py   # 写作风格分析
│   ├── agent_loop.py       # Agent 工具调用循环（function calling）
│   └── task_manager.py     # 全局任务管理器
│
├── ui/                     # Web 用户界面
│   ├── web_ui.py           # Flask 管理面板
│   ├── templates/          # Jinja2 模板（22个）
│       ├── base.html       # 布局骨架（导航/面包屑/右栏 Agent 聊天面板）
│       ├── dashboard.html  # 仪表盘（统计+快捷入口+各书下一步）
│       ├── books.html      # 图书列表
│       ├── book_detail.html# 单书详情
│       ├── start_book.html # 新书启动 ① 一句话设定
│       ├── world_card.html # 世界观设定卡（设定唯一编辑面）
│       ├── storyline_write_flow.html # 写作台（两栏：故事线+正文/规划）
│       ├── storyline_outline_card.html # 大纲卡片组件
│       ├── publish.html    # 单书上架/导出
│       ├── publish_index.html # 上架管理中心
│       ├── extract.html    # 内容提取
│       ├── settings.html   # 设置
│       ├── review_test.html# 审查测试（调试工具组）
│       ├── deai.html       # 去AI测试（调试工具组）
│       ├── plots.html      # 桥段库
│       ├── structures.html # 大纲库
│       ├── gags.html       # 笑点库
│       ├── profiles.html   # 笔名管理
│       ├── new_profile.html# 新建笔名
│       ├── scout.html      # 番茄侦察兵
│       ├── _flow_stepper.html # 流程状态条 + 状态徽标 partial
│       └── _info_bar.html  # 书信息条 partial
│   └── static/             # CSS / JS
│       ├── css/            # base.css / story_line.css
│       └── js/             # base.js / story_line.js / agent_panel.js / library_review.js
│
├── books/                  # 图书数据（.gitignore）
│   └── {book_id}/
│       ├── book.json       # 图书配置
│       ├── chapters/       # 章节 JSON
│       ├── outline/        # 大纲
│       ├── storyline.json  # 故事线配置
│       ├── assembler_plan.json # 写作计划（桥段/笑点注入）
│       ├── draft_chapter.json  # 进行中章节草稿
│       ├── character_states.json
│       └── cost.json
│
├── profiles/               # 笔名档案 JSON（.gitignore）
├── storage/                # 运行时缓存（.gitignore）
├── api.json                # API 配置（.gitignore）
├── api.example.json        # 配置模板
│
├── test_all.py             # 全模块集成测试
├── test_chapters.py        # 章节生成测试
├── test_e2e_pages.py       # 端到端页面测试（15页+13侧栏+3书续写）
├── test_reader.py          # 番茄阅读解析测试
│
├── docs/                   # 文档
│   ├── 设计文档-总览-claude.md  # 唯一主设计文档
│   └── archive/            # 全部历史文档归档（设计稿/交接/优化/调研/审查报告等 19 份）
│
├── requirements.txt
├── agent_tools.py            # 共享 Agent 工具注册表（29 工具，全链路）
├── mcp_server.py             # MCP 适配层（从 agent_tools 注册，claude mcp add 接入）
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
- **存储**: JSON 文件系统
- **Web 面板**: Flask + Jinja2 (58080)

---

## ⚠️ 合规声明（番茄侦察兵）

`plugins/fanqie_scout.py` 及配套的 `font_decoder.py` 仅供**个人学习、研究网文结构技巧**使用。使用前请注意：

- 番茄小说等内容平台的服务协议普遍禁止自动化数据采集，请勿用于商业用途或大规模抓取
- 请勿大量下载并二次传播受著作权保护的正文内容；分析应以「模式/结构/桥段」等抽象技巧为主，避免全文存储与转载
- PUA 字体解码属于对技术保护措施的绕过，请仅用于个人学习研究
- 爬虫模块默认开启 TLS 证书校验（`verify=True`）；如遇旧证书环境可显式传 `verify=False`
- 使用本模块产生的任何法律风险由使用者自行承担

## 许可证

MIT
