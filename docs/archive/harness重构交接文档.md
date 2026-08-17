# NovelEngine Harness 重构交接文档

> 写给明天的你。日期：2026-08-03。本次会话完成了 harness（提示词系统）重构，并清空了旧书数据、运行真实 LLM 写作测试。通用项目交接见《交接文档.md》。

---

## 0. 一句话现状

**笑点不再由大纲"计划"，而是写作时"涌现"。** 大纲只规划内涵（跟随桥段），笑点交给每写完一组短句后的"灵机一动"探测器。集中式 `prompt_harness.py` 统一产出书级设定卡与三类 prompt，`gag_injector.py` 实现探测环。跨章长程记忆改为 LLM 语义摘要。

---

## 1. 为什么重构（背景）

旧实现最大的问题：**笑点被当作"计划产物"而非"创作产物"**。

- 大纲 Phase 4 给每个桥段**固定分配** 1-2 个笑点（`plot.gag_ids`），写作时命令模型"在这桥段塞这个笑点"→ 桥段实际写出来的场景与笑点不匹配 → 生硬、别扭。
- 写完还有 `engine._inject_gags_themes_pass` 把**整章全文交给 LLM 重写一遍**去塞笑点 → 正文漂移。
- 内涵被粗暴处理：`plot.theme_hints = tl.themes[:2]` 对所有桥段取前 2 个，不检查桥段能否承载。
- 跨章无长程记忆：`summary` 字段存在但从未填充，写作只靠最近 200-3600 字原文窗口。
- prompt 分散在各模块内联构建，风格/世界观注入各写各的，无统一出口。

用户明确的设计洞察：*人类写作时"灵机一动"才产生笑点，笑点库提供"形式"（怎么逗），场景提供"内容"（什么好笑）；内涵则需要一个能承载它的桥段才能自然写出来。*

---

## 2. 设计原则（三个核心决策）

| 项 | 决策 | 落地 |
|---|---|---|
| **笑点** | 完全涌现，大纲不参与 | 删除 Phase 4 固定 gag 分配；写作时探测器命中才注入 |
| **内涵** | 跟随桥段 | Phase 4 用 `ThemeEntry.compatible_plots` 把母题挂到能承载的桥段 |
| **长程记忆** | LLM 语义摘要 | 每章写完生成 80-150 字摘要存 `summary` 字段，后续注入最近 5 章 |
| **架构** | 集中式 harness | 新 `prompt_harness.py` + `gag_injector.py`，现有模块改为调用 |
| **约束** | flash 模型、不加整章重写后处理 | 探测/摘要调用小而轻（输入 ≤500 token、输出 ≤256） |

---

## 3. 架构与数据流

```
大纲生成（5阶段）                写作（逐短句组）
  Phase1 故事分析                  每桥段写前：
  Phase2 故事线 ──bible──┐        · 候选笑点池 = prescreen_gag_pool(桥段)（免费规则）
  Phase3 桥段编排        │        · 书级设定卡精简版 + 语义摘要 + 角色状态 + 母题
  Phase4 内涵挂载 ───────┼────→   每写完一组：
  Phase5 一致性验证      │          · GagInjector.detect() → 命中则【灵机一动】注入下一组
  （不写 gag_ids）       │          · 未命中 → 静默续写
  tl.themes=库内母题     │
                         │
  prompt_harness.py（书级设定卡渲染器）
```

---

## 4. 新模块详解

### 4.1 `libraries/prompt_harness.py`（集中式出口）

- **书级设定卡（Book Bible）**：把 `timeline.basic_info`（主角/世界观/配角/基调）+ `tl.themes` + 笔名风格压缩成紧凑 bullet。
  - `build_book_bible(max_chars=1200)` 全量（主角→世界观→风格→配角→母题→基调按优先级截断）
  - `build_book_bible_condensed(max_chars=600)` 精简版（主角+世界观+风格+母题，目标 ~440 字，写作/探测器用）
  - 笔名风格同时支持 `PenNameProfile` 对象和 dict 两种形态（`_profile_style_text`）
- **渲染器**（返回 user prompt 字符串 或 `{"system","user"}`）：
  - `render_bridge_prompt(...)` → 桥段写作 user prompt（**取代** timeline_writer 内联拼装）。**修复了原 `_group_prompt` 的 bug：`character_states` 形参从未渲染，现在补上。**
  - `render_detector_prompt(recent_text, humor_style, pool)` → 探测器 prompt（system+user）
  - `render_summary_prompt(content_tail, bridge_line)` → 语义摘要 prompt（system+user）
  - `render_outline_context(phase_kind, timeline)` → 大纲各 phase 前置设定卡（sequence/select_plots/validate/theme_review）
- `prescreen_gag_pool(plot, book_id)` → 候选笑点池免费规则预筛（≤6 个）

### 4.2 `libraries/gag_injector.py`（灵机一动探测环）

```python
class GagInjector:
    def prescreen_pool(plot, book_id="") -> list   # 委托 harness
    def detect(item, recent_text, humor_style, pool) -> dict
        # {"has_opportunity", "gag_ids[]", "reason", "deploy_hint"}
    def build_inspiration_hint(hit, pool) -> str    # 【灵机一动】段
```

**探测器调用**：`temperature=0.3, max_tokens=150`。任何解析失败/缺字段/越权模式（`gag_ids ⊆ 候选池`）一律视为**未命中（静默）**，绝不打断写作。`deploy_hint` 为空或 >60 字则丢弃。命中 → 把 hint 注入**下一组**写作 prompt，用完即清。

**候选池预筛规则**（`CATEGORY_GAG_SCENES`，免费）：桥段 `category` → fit_scene 关键词（爽文→打脸后/身份揭示/多人场景；战斗→战斗后放松/战斗间隙；情感→日常互动/身份揭露；…）→ `gag_lib.search(scene=kw)` 去重 → 过滤 `enabled==False` / `book_id in banned_in` → 按 `usage_count` 升序 → 截断 ≤6 条。

**集成点**：`timeline_writer._write_plot_segment_groups` 循环内，维护近 3 组正文 `last_groups`，每写完一组、预算未用尽（`remaining-words > 100`）、有池时跑 detect。

---

## 5. 关键 Prompt 模板

### 5.1 桥段写作（render_bridge_prompt，system 沿用 timeline_writer 铁律逐字未动）

```
【书级设定（简）】{bible_condensed}
【所属大纲】{name}（第{start}-{end}章）
【当前阶段】{stage_name}
【本桥段要推动的事件】{events[:4]}
【桥段骨架】{structure}
【变量槽位】{slots}
【本桥段要自然体现的母题】{theme_hints}（从情节自然流露、不直白点题）   ← 有才出现
【灵机一动】顺势落地 {deploy_hint}                                  ← 探测器命中才出现
【已完成章节语义摘要】- 第N章：...                                  ← 有摘要才出现
【前文上下文】
【上一章结尾】prev[-150:] / 【本章已写正文】buffer[-900:] / 【本桥段已写】bridge[-300:] / 【角色当前状态】char_states
【写作要求】3-5 短句 / 一句一行 / 严禁词表 / 预算控制（7 条铁律逐字保留）
```

### 5.2 探测器（render_detector_prompt）

```
system: 你是中文网文主角的「喜剧嗅觉探测器」。只返回 JSON。
user: 判断刚写好的正文里，下一句顺势落一个笑点是否「天然、不硬凑」。
【主角喜剧声线】{humor_style}
【候选笑点模式池】≤4 条：[id] name：pattern_description[:60]
【刚写好的正文（本组+前1-2组）】{recent_text[-450:]}
【判断铁律】1. 只在对话刚结束/动作刚发生/情绪高点/天然落差处认为有戏；
2. 没有天然缝隙绝对不要硬造，宁可错过不可硬塞（最高优先级）；
3. gag_ids 最多 1 个且必须选自候选池。
返回 JSON：{"has_opportunity": bool, "gag_ids": [], "reason": "...", "deploy_hint": "..."}
```

### 5.3 语义摘要（render_summary_prompt，每章写完一次）

```
输入：content_tail[-600:] + 本桥段名 → 输出 80-150 字客观摘要
temperature=0.3, max_tokens=256 → 存入 chapters/{NNNN}.json.summary
```

---

## 6. 文件级改动清单

| 文件 | 改动 |
|---|---|
| `libraries/prompt_harness.py` | **新建**：Book Bible + 4 渲染器 + 候选池预筛 |
| `libraries/gag_injector.py` | **新建**：探测器环 |
| `libraries/timeline_writer.py` | `__init__` 加 `harness/gag_injector`；`_group_prompt` 改薄包装（有 harness 走渲染器，无则回退内联）；`_write_plot_segment_groups` 集成探测环 + **空响应重试**（`WRITER_EMPTY_RETRIES=2`，max_tokens 700→**1600**）；`write_bridge_stepwise` 加 **0 字桥段保护**（不置 `written_chapter`、yield `bridge_skip`、下章重试，防止空响应把桥段全吞掉）；三个写入口透传 `summaries_context`。**保留未提交基线**：3-5 短句铁律 / temp 0.7 |
| `libraries/outline_generator.py` | Phase 4 `_inject_gags_and_themes`→`_inject_themes_and_hooks`（删 gag 分配，compatible_plots 匹配母题）；`_review_injections`→`_review_theme_assignments`；事件 `gag_injected`→`theme_injected`、kind `gag_review`→`theme_review`；`_select_book_themes` 改为优先按本书桥段命中 compatible_plots；删"笑点密度"校验、加"内涵覆盖率"；`__init__` 加 `harness`，Phase 2/3/5 prompt 前置设定卡。**顺手修 bug：补 `Optional` 导入** |
| `libraries/engine.py` | 删 `_inject_gags_themes_pass` 与其调用；`start_new_book_timeline`/`continue_book` 构建 harness + GagInjector 传入 writer；新增 `_summarize_chapter`；`_finalize_written_chapter` 落盘时写入摘要；`_prepare_chapter_context` 返回 5 元组（+summaries）；三个写桥段路径透传摘要；节拍路径把摘要拼 `previous_summary`。**顺手修 bug：de-AI 拿 `.processed`（原代码把 DeAIResult 对象当文本）** |
| `libraries/book_manager.py` | 新增 `update_chapter_summary` / `load_chapter_summaries` |
| `libraries/timeline.py` | `fill_gags_and_hooks`→`fill_themes_and_hooks`（删 gag_ids 分配） |
| `ui/web_ui.py` | generate-full 传 harness；事件白名单 `gag_injected`→`theme_injected`；`_decision_log_message` 的 `gag_review`→`theme_review`；fill 端点改调新方法 |
| `ui/templates/timeline_editor.html` | 事件/文案联动；`p.gag_ids` 渲染改为"笑点: 写作时灵机一动（不预设）" |
| `tools/test_outline_quality.py` | `gag_review`→`theme_review` |

**字段兼容**：`PlotSlot.gag_ids` 及其 to_dict/from_dict **保留不动**（兼容旧数据），只是不再写入。

---

## 7. token 预算与参数约定

| 调用 | 输入 | 输出 | temp | 频率 |
|---|---|---|---|---|
| 桥段写作 | 设定卡精简+计划+摘要+原文窗口+命中提示 | **1600** | 0.7 | 每短句组 |
| 探测器 | ≤500 token（recent 450 字 + 池 4×60） | **400** | 0.3 | 每短句组 |
| 语义摘要 | ≤500 token（content 尾 600 字） | **1024** | 0.3 | 每章 1 次 |
| 书名/简介（book_meta） | ≤1000 字第1章 + prompt | **1024** | 0.8 | 第 1 章写完一次 |
| 大纲各 phase | 设定卡全量 ≤1200 + phase 自身 | 沿用 | 沿用 | phase 级 |

> ⚠️ **max_tokens 必须留推理余量**：`deepseek-v4-flash` 先输出 `reasoning_content` 再输出 `content`，两者共享 `max_tokens`。若 max_tokens 过小（如 700），推理吃光预算 → content 为空（静默返回 ""）。实测同 prompt 700→0字、1600→76字。因此写作/探测器/摘要的 max_tokens 都需明显大于"纯正文 + 推理"之和，且写作路径对空响应做了 2 次重试（`WRITER_EMPTY_RETRIES`）。

---

## 8. 验证方式

### 无 LLM 健全性（已跑通，全绿）
- Book Bible 渲染（386/343 字、含主角/风格/母题）
- 桥段 prompt 结构（含摘要/角色状态/母题/灵机一动）
- 探测器四路径：池内命中 / 未命中 / 异常 JSON / 越权模式 → 全部符合预期
- 候选池预筛 ≤6、全 enabled、升序
- 母题挂载：`plot_dating_001` 命中"公平"，无兼容桥段留空，`gag_ids` 不被触碰
- 摘要存储：降序、非空过滤、补写正常
- 规则模式完整 `generate()`：27 桥段、0 gag_ids、5 桥段挂内涵、`theme_injected` 事件正常

### 真实 LLM E2E（本次运行中）
`PYTHONIOENCODING=utf-8 python tools/test_full_flow.py --genre 都市 --sub 系统流`
① 5 阶段大纲 → ② 桥段写作 3 章（触发探测器+摘要）→ ③ 桥段续写第 4 章（`_exec_write_timeline_chapter`，唯一核心）→ ④ 落盘校验 → 自清理测试书。**注意：脚本 stdout 重定向到文件是块缓冲，进度要等缓冲区满或结束才可见，用 books/{book_id}/chapters 轮询更靠谱。**

---

## 9. 已知取舍 / 待办（按优先级）

1. **旧书已有 `gag_ids` 的桥段，写作时被新逻辑忽略**（不再强制塞旧笑点）——符合设计，但行为变了，若需保留旧笑点要另行处理。
2. **探测器每短句组一次调用**会新增 latency（约 0.3-1s/次），flash 便宜。可加频率旋钮（每1组/每2组/每桥段）。
3. **`banned_in` 未生效**：`prescreen_pool` 调用时 `book_id=""`，跨书避免重复笑点模式的功能暂失效（笑点涌现后此限制不再关键）。
4. **`gag_hit` SSE 事件未做**：前端写作台看不到探测器命中过程（`timeline_write_flow.html` 对未知事件会忽略，安全）。要做需在 `_write_plot_segment_groups` yield 事件。
5. **`test_full_flow.py` 的 OutlineGenerator 未传 harness**（profile 也 None），大纲前置设定卡分支未被 E2E 覆盖——行为正常，只是覆盖不全。
6. **`timeline_write_flow.html`** 未加探测器命中的视觉反馈（可选）。
7. `_storyline_chapter_context` 仍收集 `gags`（现恒空），`_render_chapter_outline` 的【可注入笑点】段自动不渲染——无害，可顺手清理。

---

## 10. 坑与注意事项

- **书级设定卡是"可用线索"，不是硬约束**：母题/风格注入措辞用"要自然体现/应该"，避免模型机械套用。
- **探测器必须"宁缺毋滥"**：`has_opportunity=false` 时不注入任何东西；`build_inspiration_hint` 自带"一句收进场景、不解释、不标注'笑点'"纪律。改 prompt 时别削弱这条。
- **删了 `_inject_gags_themes_pass`**：全仓已无残留引用（已 grep 确认）。不要再加"整章重写塞笑点"的后处理。
- **flash 模型 / 不加 LLM 质量重写**是用户反复强调的硬约束，探测器/摘要算生成过程的一部分，不算后处理。
- 测试会动数据：`test_full_flow.py` 自建自删测试书；`books/`、`profiles/` 是用户真实数据。本次已按用户要求**删除旧书 book_001**。
- Windows 控制台 GBK：新脚本打印中文要加 `sys.stdout.reconfigure(encoding="utf-8")`。

---

## 11. 本次会话状态（实时）

- 旧书 `books/book_001` **已删除**（用户要求"旧书全部删除"）。
- 真实 LLM 写作测试 `test_full_flow.py` **运行中**（PID 23280）。已确认：大纲完成 27 桥段、0 `gag_ids`、5 桥段挂内涵、母题=库内「公平/成长的代价」。
- 交接时若测试已完成，看输出文件确认"✅ 完整流程通过"；若仍在跑，等它自然结束（它会自清理测试书）。
- 工作区改动：8 个文件修改 + 2 个新文件（prompt_harness.py / gag_injector.py），**未提交**。测试通过后可 `git add` + 中文 conventional 提交（建议 `feat(harness): 笑点涌现+内涵跟随桥段+语义摘要+集中式prompt`）。

---

# Part 2：写作核心统一（2026-08-03 同日追加）

## 12. 统一到唯一写作核心 = 桥段写作

与 harness 互补的下半部分：删除节拍写作与老新书流程，**所有写作（含新书启动）走同一条桥段管线**。开场只是它的一种模式。

**已删除**：
- `libraries/beat_writer.py`、`libraries/new_book.py`（整体）
- `engine.py` 老新书死流程：`_route_new_book`/`_exec_plan_book`/`_exec_write_ch1-3`/`_exec_write_opening_chapter`/`_exec_generate_title`/`finalize_new_book`/`run_new_book_full`/`NewBookState`/`BookMode.NEW`/`Phase.NEW_*`/`Op.PLAN_BOOK·WRITE_CH*·GENERATE_TITLE`
- `assembler.py` 的 `PlanInjector`（仅节拍路径用）

**新增/改造**：
- `libraries/book_meta.py`：书名/简介/平台约束纯函数（从 new_book.py 抢救）
- `prompt_harness.py`：`OPENING_MODE_RULES` 炸裂开场铁律 + `render_bridge_prompt(is_opening=)`
- `timeline_writer.py`：`opening_mode_active(chapter_num, chapter_words, written_count)`（第1章前800字/前3桥段）+ `is_opening` 传递链
- `engine.py`：`_generate_book_meta`（第1章写完自动生成书名/简介 → book.json/outline.json/timeline.json）；`_exec_write_chapter` 改为纯时间线委托；`continue_book` 无 timeline 报错
- `test_all.py` Phase 3'：写作核心统一健全性（无 LLM）

**行为变化**：
- 无 timeline 的书无法写作（报错提示先生成故事线）
- 角色/伏笔 AI 规划（`_generate_initial_characters`/`_generate_foreshadows`）随老流程删除；时间线路径角色来自 outline Phase 1 `basic_info`，伏笔靠 `theme_hints`/`hook_points`
- 第 1 章开头 prompt 含炸裂开场铁律（前三句冷开场）

**验证**：`python test_all.py`（Phase 3' 覆盖开场/书名简介/平台约束）；`python tools/test_full_flow.py`（改写后第 4 章走桥段续写）。
