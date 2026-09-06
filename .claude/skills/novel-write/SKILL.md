---
name: novel-write
description: >-
  写作阶段。Use when the user wants to 写正文/写下一章/继续写/写桥段/把第 N 章写出来/写开头几章/
  一键写完整章/写完整章/一键完整章节/完整章节构建
  (write the next bridge or chapter, build a complete chapter in one shot)。
  流程：渐进式披露组装上下文（书详情→故事线→当前桥段出场角色→最近已写）→ **agent 自主生成桥段
  正文**（保持完整上下文连续）→ save_bridge_draft 落盘 → 章满 save_chapter_text → 规则质检。前置：phase=ready。
---
# 写作阶段（novel-write）— agent 自主生成

> **核心原则**：正文由你（agent）**自主生成**——你带着完整对话上下文（书设定/故事线/刚写的前一段/
> 风格要求）逐桥段写，生成后调用**薄工具** `save_bridge_draft` / `save_chapter_text` 落盘。
> **不要**调用内部跑 LLM 的旧工具（`write_next_bridge` / `write_chapter`，已废弃留档）。

## 前置检查（必做）
1. `mcp__novel-engine__get_book_state(book_id)`：
   - `phase=config`（缺弧）→ 提示先经建书向导/dsh 落弧（本 skill 不排弧）。
   - `phase=plots`（config 补弧后/遗留恢复；正常新书提交即 ready，不经此）→ 提示用户在书详情页「✅ 确认弧+桥段」进 ready 再写（agent 无翻 ready 工具）。
   - `phase=ready` → 继续；看 `current_chapter` 与进行中草稿（draft）定位续写点。
2. `mcp__novel-engine__get_storyline(book_id)` → 当前弧、下一个待写桥段（plot_id/名称/写在哪章）。

## 上下文组装（渐进式披露 — **单次读取**，不要把整本书灌进上下文）
1. **一次** `mcp__novel-engine__get_writing_context(book_id)` → 返回 `{book(含 tags), storyline 全量, outline, chapters(最近摘要), draft, synopsis, protagonist, next_bridge, style_card}`——含角色/世界观/基调/pov/下一个待写桥段/精简风格卡，一次拿全。
2. 整理成「写哪个桥段（next_bridge）+ 出场角色 + 风格要求 + 前文语气」，然后**自己生成正文**。
3. **逐桥段循环里每轮只重取一次 `get_writing_context`**（draft/written_chapter 会变）；**不要**再单独调 `get_book_detail` / `get_storyline`（它们是 get_writing_context 的子集/重叠）。

## 写作规则（生成时内嵌到你的思考）
- **笔名风格强约束（必读必遵）**：动笔前先 `mcp__novel-engine__get_pen_style(book_id)` 拿该笔名**全量风格**（句式风格 + 禁止内容 + 语言习惯 + 通用纪律），逐条遵守；每轮 `get_writing_context` 返回的 `style_card` 是**精简风格提醒（必读，防风格漂移）**。**未拿到风格不得写正文**；被裁剪/信息不足时用 `get_pen_style` 重读（独立薄工具，不纠缠全量上下文）。若 `get_pen_style` 的 `style_rules` 内含 `STYLE REFERENCE` 人工样本段，它是**最高风格来源**：直接参考其语言惯性/叙述距离/信息组织/对白衔接继续创作，**不总结、不抽公式、不套模板**；md 原则与规则只作负约束。样文 = 全局样文库（10 类场景×多条）。**动笔前按当前桥段性质把场景标签传给 `get_pen_style(book_id, scene_tags=[…])`**（新场景/开场→开场、多人聚→群像/多人对白、破解→推理、对峙/危险→冲突·威胁、死伤→死亡·惊悚、规则说明→规则·设定、独自心绪→独处·心理、平和日常→过渡·日常、反转揭底→高潮·转折；一个桥段可多传）。命中只注入该类词条（每条前标 `# 场景:…`），不传=多样封顶；`samples` 给 id/title/scene_tags（无正文）便于核对，想细看用 `get_style_sample` 拉全文。
- **一致性铁律**：人名/系统绑定/数值/设定不得与已写冲突；前后呼应伏笔。
- **视角铁律**：全书统一（默认第三人称），不漂移。
- **语言纪律**：禁 AI 味句式（仿佛/似乎/不禁/只见 堆叠），少用破折号，对话占比自然——以 `get_pen_style` 拿到的笔名规则（句式风格/禁止内容）为准。
- **前三章开篇钩子**（第 1-3 章）：首句强钩子、三章内出第一个爽点、章末留钩。
- **每章字数**：约 `words_per_chapter`（get_book_state 看）；一桥段 800-2500 字。
- **章末钩子**：每章最后一句留悬念/反转/爽点，保追读。

## 生成 → 落盘（逐桥段）
1. 用 `get_writing_context` 的 `next_bridge`（第一个未写桥段）→ **你自主生成该桥段正文**
   （你的 LLM 直接产出，上下文连续）。
2. 生成完调用 `mcp__novel-engine__save_bridge_draft(book_id, chapter_num=N, plot_id=..., plot_name=..., text=...)`
   落盘到进行中草稿（断点续写保底）。
3. 继续下一桥段；每写完一桥段按需自我核查（语气连贯/伏笔/错词），有问题就地重写再落盘。

## 章满收尾 → save_chapter_text
- 累计字数 ≥ `words_per_chapter`（或故事线该章桥段写完）→ **你生成章节语义摘要**（80-150 字，
  供长程记忆），把本章全部桥段拼成完整正文，调用：
  `mcp__novel-engine__save_chapter_text(book_id, chapter_num=N, text=完整正文, title=「第N章」,
   summary=你生成的摘要, bridge_segments=[{plot_id, plot_name, text}, ...])`
  ——它负责规则去AI味/审查/角色状态/承诺台账/书进度并落盘（内部不调 LLM）。
- `save_chapter_text` 返回 `word_count`/`review`；有问题可用 `chapter_quality_gate` 复核（只报告不修复）。

## 完整章节构建流水线（一键写完整章 / 完整章节构建）
1. **写**：逐桥段「你生成正文 → save_bridge_draft」直到本满章 → save_chapter_text。
2. **门禁**：`mcp__novel-engine__chapter_quality_gate(book_id, chapter_num=0)` 跑审查/连续性/追读/伏笔/爽点。
3. **汇报（只报告不修复）**：读 `summary` + `checks.*.passed` + `decision_points`；
   `passed=false` → 逐条列给用户定夺。

## 退出状态
- 桥段写完：`save_bridge_draft` 落盘草稿；整章写完：`save_chapter_text` 返回 `chapter`/`word_count`/`review`。
- 全书写完 → 引导 `novel-publish`（save_book_meta → publish_check → publish_book）。

## 失败处置
- phase 不过（config 缺弧 / plots 待确认）→ 按前置检查处理：config 引导落弧，plots 引导书详情确认进 ready，勿硬写。
- `BookBusyError` → 稍后重试。`budget_paused` → 停，问用户。
- 生成内容为空/报错 → 检查 `api.json` 模型配置与 max_tokens 余量（推理型模型需留足）。
- `save_chapter_text` 保存失败 → 桥段已用 `save_bridge_draft` 落盘（草稿还在，可续），检查后重试。
