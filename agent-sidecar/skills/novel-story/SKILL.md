---
name: novel-story
description: 弧+写作阶段。写正文/写下一章/写情节段/续写扩写/一键写完整章/排故事线/config补弧。流程:先看 phase——ready(含新书提交即 ready)=逐情节段写正文(save_plot_draft → 章满 save_chapter_text → 规则质检)；config/plots=兜底补弧排故事线(save_outlines 深化式落盘,每弧 notes+validate 回打,参考 novel-build 步3)→用户在书详情确认进 ready；已 ready 追加弧保持 ready。深化已并入建书步3,新书提交即 ready,本 skill 不再承担常规深化段。
---

# 弧 + 写作（novel-story）— 排故事线到写正文一体

> 核心：弧+情节段与正文都**由你自主生成**（完整上下文连续），生成后调薄工具落盘；不要调内部跑 LLM 的旧工具。
> 排故事线（弧+情节段）与写正文是同一流水线的两个阶段：先建弧+情节段（`save_outlines`），再逐情节段扩写正文（`save_plot_draft` → `save_chapter_text`）。
> 定义与契约（弧/情节段/线程/设局收局/章节/故事线数据规则）见 NOVEL_AGENT.md 1.1 / 1.2。

## 前置检查（必做）
- `get_book_state(book_id)`：`phase=ready` → 直接走**阶段二**（写作）——新书经建书步3 深化式生成、用户提交即 ready，正常都是 ready；`phase=config`/`phase=plots` → 弧+情节段没随 submit 带上（step3 ②失败/仅 outline 无 plots 的兜底），先走**阶段一**补弧排故事线；看 current_chapter 与草稿定位续写点。

## 阶段一：补弧 / 排故事线（config/plots 兜底、ready 追加弧用；深化已在建书步3 内联完成）
- 自主生成 outlines/plots/threads/themes（含每条弧 `notes`「弧目标+偏离库模板点」；**每情节段 `words` 按内容浓淡 300~2500、弧跨度=其 words 之和、勿按章均分；≥3章且内含 2+ 可独立排序子目标的目标块拆第三层（判据见 NOVEL_AGENT 1.1），无则保持两层**——口径同 novel-build 步3）→ `save_outlines` 落盘（config/plots 书 → phase=plots 待用户确认；**已 ready 书追加弧保持 ready**）。
- **校验回打（≥1 轮）**：调 `validate_storyline(book_id=…)`（含 arc_fill）→ 按 decision_points 回改 ≥1 轮 → 直到 passed 或列残留决策点；若报 `structure_hints`（叶弧跨度均一/情节段字数全同）**须消除或说明**。
- **非 ready 书由用户确认**：书停在 phase=plots → 汇报蓝图（弧树/字数/情节段/线程/设局收局 + passed + decision_points），引导用户在书详情页点「✅ 确认弧+情节段」进 ready；**agent 无翻 ready 工具，不得臆造翻转**。已 ready → 直接进阶段二。
- 弧（定义见 NOVEL_AGENT.md 1.1）：每弧有明确方向/目标（写进 `notes` 或 `narrative_target`），用 `start_word/end_word` 标**字数跨度**
  （0 基，start 含/end 不含），不设固定章数；可 `parent_arc_id` 套子弧；**情节段仅挂最底层弧**（不包含其他弧的弧）。
- **顶层弧覆盖**：故事线纵轴任意点都要有顶层弧占据；续写/扩写追加弧时，上一弧的 `end_word` 应接续到新弧的 `start_word`（除非有意留白并说明）；**生成后调 `validate_storyline` 校验**（含 arc_fill 弧内空白），不要靠肉眼读 get_storyline 检查。
- 跨弧贯穿的线索用 `threads`（主线/副线/伏笔线）；设局情节段让收局情节段 `resolves_plot_id` 指向设局槽位形成收局。
- 数据规则（全书规模口径 / 先按情节段设计弧跨度 / cover_beats）见 NOVEL_AGENT.md 1.2 故事线数据规则。

## 阶段二：写作（写正文 / 写下一章 / 写情节段）
### 上下文组装（单次读取）
- **一次** `get_writing_context(book_id)` → 书(tags)+故事线+弧+章节摘要+draft+next_plot+style_card，一次拿全。
- 整理成「写 next_plot + 出场角色 + 风格 + 前文语气」，**自己生成正文**。逐情节段循环每轮只重取一次；**不要**再单独调 get_book_detail/get_storyline。
- **笔名风格强约束（必读必遵）**：动笔前先 `get_pen_style(book_id)` 拿该笔名**全量风格**（句式风格 + 禁止内容 + 语言习惯 + 通用纪律），逐条遵守；每轮 `get_writing_context` 的 `style_card` 是**精简风格提醒（必读，防风格漂移）**。**未拿到风格不得写正文**；被裁剪/信息不足时用 `get_pen_style` 重读（独立薄工具，不纠缠全量上下文）。若 `get_pen_style` 的 `style_rules` 内含 `STYLE REFERENCE` 人工样本段，它是**最高风格来源**：直接参考其语言惯性/叙述距离/信息组织/对白衔接继续创作，**不总结、不抽公式、不套模板**；md 原则与规则只作负约束。样文 = 全局样文库（多维权表：scene/dramatic_state/narrative_action/cast/dialogue_density/information_density/pace/pov，英文键存）。**动笔前按当前桥段判断 dims 组成 `query` 传给 `get_pen_style(book_id, query=query, k=3)`**：对话多→ `{"scene":["dialogue"],"cast":"small_group","dialogue_density":"high"}`；破解/查证→ `"scene":["investigation"]`；对峙/危险→ `"scene":["confrontation","danger"]`；死伤→ `"scene":["death"]`；规则/设定→ `"scene":["revelation","planning"]`；独自心绪→ `"scene":["quiet"],"cast":"solo"`；平和日常→ `"scene":["quiet","transition"]`；反转揭底→ `"scene":["revelation"]`。服务端按 query 硬过滤→软加权→加权随机→近期避重抽 ≤k 条，命中每条前标 `# 场景:…`；不传 query=多样封顶。`samples` 给 id/title/dims（无正文），想细看某条用 `get_style_sample` 拉全文。

### 生成 → 落盘（逐情节段）
- 用 next_plot（第一个未写情节段）→ **你自主生成正文** → `save_plot_draft(book_id, chapter_num=N, plot_id, plot_name, text)` 落草稿。
- 每情节段后自我核查（语气/伏笔/错词），有问题就地重写再落盘。

### 章满收尾 → save_chapter_text
- **章节 = 2000-6000 字可发布文本段**（定义见 NOVEL_AGENT.md 1.1）：由情节段字数累计，**正文草稿超过书目设定字数后由你切分**，非故事线坐标。
- 字数达标 → 你生成章节摘要（80-150 字），拼全文，调 `save_chapter_text(book_id, chapter_num=N, text=全文, title=「第N章」, summary=你生成的摘要, plot_segments=[{plot_id,plot_name,text},...])`
  （它负责规则去 AI 味/审查/角色状态/承诺台账/进度并落盘，无 LLM）。

### 完整章节构建流水线
1. 写：逐情节段「生成 → save_plot_draft」→ save_chapter_text。
2. 门禁：`chapter_quality_gate(book_id)` 跑五项。
3. 汇报：`summary` + `checks.*.passed` + `decision_points`；passed=false → 逐条列给用户，只报告不修复。

## 退出状态
- 情节段写完：save_plot_draft 落草稿；整章写完：save_chapter_text 返回 chapter/word_count/review。全书完 → `novel-publish`。

## 失败处置
- phase 不过 → 按前置检查处理（config 先走阶段一排弧）。`BookBusyError` → 稍后重试。
- 内容为空/报错 → 检查 api.json 与 max_tokens。save_chapter_text 失败 → 草稿还在（save_plot_draft 已落），检查后重试。
