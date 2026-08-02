# NovelEngine — AI 小说工厂

> **全自动网文量产系统**：AI 拟人写作 × 节拍级生成 × 番茄侦察兵

---

## 核心理念

每个笔名 = 一个独立的 AI 作家，有自己的记忆、风格、桥段库和创作习惯。

不是"一个生成器生成多本书"，而是"一群 AI 作家同时开工"。

---

## 功能概览

| 模块 | 说明 | 状态 |
|------|------|------|
| **引擎** (`libraries/engine.py`) | 新书启动 → 规划 → 逐章续写，全自动闭环 | ✅ v0.5 |
| **节拍写作** (`libraries/beat_writer.py`) | 14 种微模板，每章拆 6–8 节拍逐节拍生成 | ✅ 新 |
| **桥段库** (`libraries/plot.py`) | 网文经典桥段的结构化模板（12个内置） | ✅ |
| **大纲库** (`libraries/structure.py`) | 各流派的卷/弧/章骨架（5个内置） | ✅ |
| **笑点库** (`libraries/gag.py`) | 搞笑模式模板 + 例句（10个内置） | ✅ |
| **内涵库** (`libraries/theme.py`) | 故事背后的母题与表达手法（6个内置） | ✅ |
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
# 旧版 FastAPI 兼容后端：python run.py server（与主面板同端口，二选一启动）
```

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

每个章节拆分为 6–8 个**节拍**（`beat_writer.py`），使用 14 种微模板逐节拍生成：
- 开篇、对话、动作、内心独白、环境描写、战斗、冲突、反转、情感爆发、
  日常过渡、信息揭露、悬念收尾、高潮、余韵

---

## 番茄侦察兵

`plugins/fanqie_scout.py` 提供番茄小说数据采集三件套：

| 功能 | 方式 | 说明 |
|------|------|------|
| **搜索** | Bing 搜索 → SSR 页面解析 | 番茄搜索 API 已全部挂掉，走搜索引擎 |
| **下载** | Reader 页面 SSR → PUA 字体解码 | 使用 `font_decoder.py` + fonttools |
| **分析** | 4 次 LLM 调用 → 入库四大库 | 桥段/大纲/笑点/内涵库自动填充 |

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

### 内涵库 —— `libraries/theme.py`

6 母题：正义必胜、自由意志、爱与牺牲、成长蜕变、命运抗争、因果轮回。

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
├── main.py                 # FastAPI 后端入口 (58080)
├── run.py                  # 命令行入口（server / ui / dev）
├── launch.bat / launch.sh  # 一键启动脚本
│
├── libraries/              # 核心业务逻辑
│   ├── engine.py           # 引擎（新书/续写双模式）
│   ├── beat_writer.py      # 节拍级写作（14微模板）
│   ├── outline_generator.py# 故事线大纲生成（5阶段管线）
│   ├── timeline.py         # 时间线数据模型 + TimelineBuilder
│   ├── timeline_writer.py  # 桥段驱动的逐章增量写作
│   ├── assembler.py        # 书籍组装器（库材料→写作计划）
│   ├── outline_agent.py    # 大纲助手（自然语言调整故事线）
│   ├── base_library.py     # 四大库共用基类（单例+读写）
│   ├── new_book.py         # 新书模块（前三章生成）
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
│   └── theme.py            # 内涵库（6母题）
│
├── core/                   # LLM 引擎（show-me-the-story Python 移植）
│   ├── llm_client.py       # API 调用封装
│   ├── writing.py          # 写作引擎
│   ├── prompts.py          # 提示词模板
│   ├── models.py           # 数据模型
│   ├── storage.py          # 存储层
│   ├── inject.py           # 上下文注入
│   ├── foreshadow.py       # 伏笔系统
│   ├── arcs.py             # 卷/Arc 系统
│   ├── reconcile.py        # 结果修正与后处理
│   ├── skills.py           # 技能嵌入系统
│   └── embeds/skills/      # 内置技能（写作技巧/人设/剧情发展）
│
├── plugins/                # 外部采集插件
│   ├── fanqie_scout.py     # 番茄侦察兵（搜/下/析）
│   ├── font_decoder.py     # PUA 字体解码器
│   ├── novel_storage.py    # 已下载小说管理
│   ├── style_analyzer.py   # 写作风格分析
│   └── task_manager.py     # 全局任务管理器
│
├── ui/                     # Web 用户界面
│   ├── web_ui.py           # Flask 管理面板
│   ├── templates/          # Jinja2 模板（20个）
│       ├── base.html       # 布局骨架
│       ├── dashboard.html  # 仪表盘
│       ├── books.html      # 图书列表
│       ├── book_detail.html# 单书详情
│       ├── start_book.html # 新书启动 ① 选择笔名/流派
│       ├── timeline_editor.html # 故事线编辑器（时间线）
│       ├── timeline_detail.html # 故事线草稿详情
│       ├── timeline_outline_card.html # 大纲卡片组件
│       ├── timeline_write_flow.html # 蓝图式写作台
│       ├── desk_list.html  # 写作台列表
│       ├── extract.html    # 内容提取
│       ├── settings.html   # 设置
│       ├── review_test.html# 审阅测试
│       ├── deai.html       # 降重测试
│       ├── plots.html      # 桥段库
│       ├── structures.html # 大纲库
│       ├── themes.html     # 内涵库
│       ├── gags.html       # 笑点库
│       ├── profiles.html   # 笔名管理
│       ├── new_profile.html# 新建笔名
│       └── scout.html      # 番茄侦察兵
│   └── static/             # CSS / JS
│       ├── css/            # base.css / story_line.css
│       └── js/             # base.js / story_line.js / library_review.js
│
├── books/                  # 图书数据（.gitignore）
│   └── {book_id}/
│       ├── book.json       # 图书配置
│       ├── chapters/       # 章节 JSON
│       ├── outline/        # 大纲
│       ├── timeline.json   # 故事线配置
│       ├── assembler_plan.json # 写作计划（桥段/笑点注入）
│       ├── draft_chapter.json  # 进行中章节草稿
│       ├── character_states.json
│       └── cost.json
│
├── profiles/               # 笔名档案 JSON（.gitignore）
├── storys/                 # 旧引擎（FastAPI 兼容）项目数据（.gitignore）
├── storage/                # 运行时缓存（.gitignore）
├── api.json                # API 配置（.gitignore）
├── api.example.json        # 配置模板
│
├── test_all.py             # 全模块集成测试
├── test_chapters.py        # 章节生成测试
├── test_e2e_pages.py       # 端到端页面测试（12页+11侧栏+3书续写）
├── test_reader.py          # 番茄阅读解析测试
│
├── docs/                   # 设计文档
│   ├── 项目规划.md
│   └── 开源调研.md
│
├── requirements.txt
└── LICENSE
```

---

## 参考项目

NovelEngine 的开发参考了以下开源项目：

### [Nigh/show-me-the-story](https://github.com/Nigh/show-me-the-story) ⭐ 398+

**核心架构参考。** 整个 `core/` 模块（LLM 客户端、写作引擎、提示词系统、伏笔系统、卷弧管理、上下文注入、结果修正等）是对 show-me-the-story Go 架构的完整 Python 移植。其大纲→逐章→伏笔→打磨的流程设计是 NovelEngine 的架构基石。

### [qiuxinyuan321/novel-writer-master](https://github.com/qiuxinyuan321/novel-writer-master)

**流式 UI + AI 降重参考。** Streamlit 实验台的设计灵感来源于此项目，AI 降重模块 `de_ai.py` 的思路也受其启发。

> *"从 show-me-the-story 的架构思想出发，走向真正的网文工业量产。"*

---

## 技术栈

- **语言**: Python 3.10+
- **LLM**: DeepSeek API（兼容 OpenAI 格式）
- **存储**: JSON 文件系统
- **后台**: FastAPI (58080)
- **管理面板**: Flask + Jinja2 (58080)
- **实验台**: Streamlit (8501)

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
