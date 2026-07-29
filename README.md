# NovelEngine — AI 小说工厂

> **全自动网文量产系统**：多号矩阵 × 模板驱动仿写 × AI 拟人写作

---

## 核心理念

每个笔名 = 一个独立的 AI 作家，有自己的记忆、风格、桥段库和创作习惯。

不是"一个生成器生成多本书"，而是"一群 AI 作家同时开工"。

---

## 四大核心库

| 库 | 说明 | 当前规模 |
|---|---|---|
| **桥段库** (`libraries/plot.py`) | 网文经典桥段的结构化模板（12个内置） | 12 模板 × 7 分类 |
| **大纲库** (`libraries/structure.py`) | 各流派的卷/弧/章骨架（5个内置） | 5 模板覆盖玄幻/都市/悬疑/言情/穿越 |
| **笑点库** (`libraries/gag.py`) | 搞笑模式模板 + 例句（10个内置） | 10 模式 × 8 分类 |
| **内涵库** (`libraries/theme.py`) | 故事背后的母题与表达手法（6个内置） | 6 母题 |

### 桥段库分类

- 爽文：退婚打脸、拍卖会捡漏、装逼打脸连环套、扮猪吃虎日常
- 开篇：穿越/重生开局、获得金手指/系统激活
- 战斗：擂台/比武大会、闯关/秘境探险
- 成长：拜师/拜入门派
- 冲突：宗门/家族危机
- 情感：英雄救美、修罗场/情感博弈

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

## 仿写流程

```
选大纲模板 → 确定章节结构
选桥段模板 → 填入变量 → LLM填充段落
注入风格约束（笔名档案）
注入笑点（按场景匹配）
注入母题（按桥段匹配）
→ LLM 生成正文
→ 人工确认 → 保存章节
```

## 目录结构

```
D:\NovelEngine/
├── libraries/           # 四大核心库 + 档案 + 图书管理
│   ├── plot.py          # 桥段库（12模板）
│   ├── structure.py     # 大纲库（5模板）
│   ├── gag.py           # 笑点库（10模式）
│   ├── theme.py         # 内涵库（6母题）
│   ├── profiles.py      # 笔名风格档案
│   └── book_manager.py  # 图书管理器
├── plugins/             # 外部采集插件
│   └── __init__.py      # 番茄/起点/微博/B站插件骨架
├── core/                # LLM 引擎（show-me-the-story 移植）
│   ├── llm_client.py    # API 调用封装
│   ├── writing.py       # 写作引擎
│   ├── prompts.py       # 提示词模板
│   └── ...              # arcs, inject, foreshadow, reconcile
├── profiles/            # 笔名档案 JSON 文件
├── books/               # 图书数据
│   └── {book_id}/
│       ├── book.json    # 图书配置
│       ├── chapters/    # 章节文件
│       └── outline/     # 大纲文件
├── exports/             # 导出 Markdown
├── api.json             # API 配置
└── 项目规划.md          # 原始设计文档
```

## 快速开始

### 方式一：一键启动（推荐）

**Windows**：双击 `launch.bat`  
**Linux/WSL2**：`bash launch.sh`

脚本自动完成：Python 检测 → api.json 检查 → 依赖安装 → 启动 Web 面板

浏览器自动打开 `http://localhost:58080`

### 方式二：手动启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API
cp api.example.json api.json
# 编辑 api.json 填入你的 DeepSeek API Key

# 3. 启动
python run.py server    # FastAPI 后端 (58080)
python ui/web_ui.py     # Flask 管理面板 (58080)
python run.py ui        # Streamlit 实验台 (8501)
```

### 1. 配置 API

编辑 `api.json`：
```json
{
  "api_key": "sk-your-key",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "http_timeout_seconds": 300
}
```

### 2. 创建笔名

```python
from libraries.profiles import ProfileManager
pm = ProfileManager("profiles")
pm.create("我的笔名", "这是我的写作风格描述",
          style_fingerprint={"humor_style": "吐槽型"},
          word_print={"common_words": ["卧槽"]})
```

### 3. 创建新书

```python
from libraries.book_manager import BookManager
bm = BookManager("books")
book = bm.create("我的小说", "我的笔名", genre="玄幻",
                 structure_template_id="struct_xuanhuan_01")
```

### 4. 匹配桥段

```python
from libraries.plot import PlotLibrary
plot = PlotLibrary()
templates = plot.match_for_chapter("主角需要第一次展现实力", genre="玄幻")
for t in templates:
    print(f"{t.name}: {t.template_structure}")
```

### 5. 注入风格约束

```python
from libraries.profiles import ProfileManager
pm = ProfileManager("profiles")
profile = pm.get("profile_001")
style_prompt = profile.build_style_prompt()
# 把 style_prompt 注入到 LLM 写作 prompt 中
```

## 开发状态

| 模块 | 状态 |
|---|---|
| 桥段库 | ✅ 12 模板，支持搜索/匹配 |
| 大纲库 | ✅ 5 流派模板，卷/弧/章骨架 |
| 笑点库 | ✅ 10 模式，场景匹配 |
| 内涵库 | ✅ 6 母题，表达手法 |
| 笔名档案 | ✅ 风格指纹 + prompt 生成 |
| 图书管理 | ✅ 创建/章节/大纲/导出 |
| 采集插件 | 🏗️ 骨架就绪，待实现具体爬虫 |
| 新书流程 | 🏗️ 前三章 + 命名（进行中） |
| 去 AI 味 | 📋 规划中 |
| 矩阵发布 | 📋 规划中 |

## 技术栈

- **语言**: Python 3.10+
- **LLM**: DeepSeek API（兼容 OpenAI 格式）
- **存储**: JSON 文件系统
- **UI**: Streamlit（已接入）

---

*从 show-me-the-story 的架构思想出发，走向真正的网文工业量产。*
