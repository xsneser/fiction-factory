---
name: novel-story
description: 弧+写作阶段。生成弧/排故事线/选桥段/续写扩写/写正文/写下一章/写桥段/一键写完整章。流程:阶段一 弧+桥段自主生成 save_outlines→fill_gags 到 ready → 阶段二 逐桥段写正文 save_bridge_draft → 章满 save_chapter_text → 规则质检。前置:phase=config(先排弧)或 ready(写作)。
---

# 弧 + 写作（novel-story）— 排故事线到写正文一体

> 核心：弧+桥段与正文都**由你自主生成**（完整上下文连续），生成后调薄工具落盘；不要调内部跑 LLM 的旧工具。
> 排故事线（弧+桥段）与写正文是同一流水线的两个阶段：先建弧+桥段（`save_outlines`），再逐桥段扩写正文（`save_bridge_draft` → `save_chapter_text`）。
> 定义与契约（弧/桥段/线程/设局收局/章节/故事线数据规则）见 NOVEL_AGENT.md 1.1 / 1.2。

## 前置检查（必做）
- `get_book_state(book_id)`：`phase=config` → 先走**阶段一**（弧+桥段）；`phase=ready` → 直接走**阶段二**（写作）；看 current_chapter 与草稿定位续写点。

## 阶段一：弧 + 桥段（生成弧 / 排故事线 / 选桥段）
- 自主生成 outlines/plots/threads/themes → `save_outlines` 落盘 → `fill_gags` 到 ready。
- 弧（定义见 NOVEL_AGENT.md 1.1）：每弧有明确方向/目标（写进 `notes` 或 `narrative_target`），用 `start_word/end_word` 标**字数跨度**
  （0 基，start 含/end 不含），不设固定章数；可 `parent_arc_id` 套子弧；**桥段仅挂最底层弧**（不包含其他弧的弧）。
- **顶层弧覆盖**：故事线纵轴任意点都要有顶层弧占据；续写/扩写追加弧时，上一弧的 `end_word` 应接续到新弧的 `start_word`（除非有意留白并说明）；**生成后调 `validate_storyline` 校验**（含 arc_fill 弧内空白），不要靠肉眼读 get_storyline 检查。
- 跨弧贯穿的线索用 `threads`（主线/副线/伏笔线）；设局桥段让收局桥段 `resolves_plot_id` 指向设局槽位形成收局。
- 数据规则（全书规模口径 / 先按桥段设计弧跨度 / cover_beats）见 NOVEL_AGENT.md 1.2 故事线数据规则。

## 阶段二：写作（写正文 / 写下一章 / 写桥段）
### 上下文组装（单次读取）
- **一次** `get_writing_context(book_id)` → 书(tags)+故事线+弧+章节摘要+draft+next_bridge+style_card，一次拿全。
- 整理成「写 next_bridge + 出场角色 + 风格 + 前文语气」，**自己生成正文**。逐桥段循环每轮只重取一次；**不要**再单独调 get_book_detail/get_storyline。
- **笔名风格强约束（必读必遵）**：动笔前先 `get_pen_style(book_id)` 拿该笔名**全量风格**（句式风格 + 禁止内容 + 语言习惯 + 通用纪律），逐条遵守；每轮 `get_writing_context` 的 `style_card` 是**精简风格提醒（必读，防风格漂移）**。**未拿到风格不得写正文**；被裁剪/信息不足时用 `get_pen_style` 重读（独立薄工具，不纠缠全量上下文）。

### 生成 → 落盘（逐桥段）
- 用 next_bridge（第一个未写桥段）→ **你自主生成正文** → `save_bridge_draft(book_id, chapter_num=N, plot_id, plot_name, text)` 落草稿。
- 每桥段后自我核查（语气/伏笔/错词），有问题就地重写再落盘。

### 章满收尾 → save_chapter_text
- **章节 = 2000-6000 字可发布文本段**（定义见 NOVEL_AGENT.md 1.1）：由桥段字数累计，**正文草稿超过书目设定字数后由你切分**，非故事线坐标。
- 字数达标 → 你生成章节摘要（80-150 字），拼全文，调 `save_chapter_text(book_id, chapter_num=N, text=全文, title=「第N章」, summary=你生成的摘要, bridge_segments=[{plot_id,plot_name,text},...])`
  （它负责规则去 AI 味/审查/角色状态/承诺台账/进度并落盘，无 LLM）。

### 完整章节构建流水线
1. 写：逐桥段「生成 → save_bridge_draft」→ save_chapter_text。
2. 门禁：`chapter_quality_gate(book_id)` 跑五项。
3. 汇报：`summary` + `checks.*.passed` + `decision_points`；passed=false → 逐条列给用户，只报告不修复。

## 退出状态
- 桥段写完：save_bridge_draft 落草稿；整章写完：save_chapter_text 返回 chapter/word_count/review。全书完 → `novel-publish`。

## 失败处置
- phase 不过 → 按前置检查处理（config 先走阶段一排弧）。`BookBusyError` → 稍后重试。
- 内容为空/报错 → 检查 api.json 与 max_tokens。save_chapter_text 失败 → 草稿还在（save_bridge_draft 已落），检查后重试。
