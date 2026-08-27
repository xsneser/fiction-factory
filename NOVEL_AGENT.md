# NovelEngine 小说创作 Agent 指令（NOVEL_AGENT.md）

你是 NovelEngine 平台的外部驱动 agent。
按本指南 + MCP 工具（`mcp__novelengine__*`）直接驱动。

---

# 第一部分：定义与契约（先读，全书唯一来源）

## 1.1 概念定义（术语）

### 弧（情节弧）
- **弧 = 5-15 章、有方向/目标的情节单元，不是卷**（卷是输出分组，见下）。
- 弧是**树状目标节点**：一个弧承载一个方向/目标，内部可独立成段的子目标/阶段可再拆成子弧；
  **子弧内仍可再拆（多层嵌套），数量与层数都由剧情结构决定，不设固定值**——不要为凑数拆、
  也不要机械地每个弧都拆；目标单一的一个弧保持单层即可。
- **拆弧思考**：对每个弧先想清它的方向/目标，再判断内部有没有可独立成段的子目标/阶段
  （起承转合/多场战役/递进目标）：有就拆成若干子弧（**几个由内容定**），没有就不拆。
- 例（思考过程，非固定配置）：一个 12 章「囤物资」弧——若含「变卖资产→采购→加固」三个递进目标
  可拆 3 个子弧；若只是一次目标单一的试炼就不拆；一个大战役还可弧→战役子弧→更小阶段多层嵌套。
- 每条弧 `start_chapter/end_chapter` 写真实跨度（5-15 章，顺序接续或明确重叠），`notes` 写方向/目标。
- 选模板时其 `stages`（阶段名）是**子弧/节拍材料**——可提升为子弧（`parent_arc_id`），
  不要只在顶层弧 `stages` 字段抄名字却不落子弧。

### 桥段
- **桥段 = 弧内可执行剧情片段/事件（约 0.3-2 章）**，完成弧的某个子目标；有场景、人物行动、冲突，
  可直接扩写正文。
- 每个桥段属于一个弧（`outline_id`）和一条线程（`thread_id`）。

### 线程 / 故事线
- **线程 = 横向贯穿全书的叙事线索**（主线/副线/伏笔线），由散布在不同弧里的桥段组成；
  线程决定阅读的穿插顺序，不改变弧/桥段本身的因果。

### 设局 / 收局
- **设局桥段** = 埋钩子（`resolves_plot_id` 为空）；**收局桥段** = `resolves_plot_id` 指向设局桥段 `id`。
- 跨弧贯穿的伏笔以此设局→收局，形成读者承诺（promises 台账自动登记）。

### 卷
- **卷 = 输出分组（后处理概念），不是素材层**；绝不允许把弧直接当成卷。弧是 5-15 章，卷是多个弧的组合。

## 1.2 工具参数契约（驱动时严格遵守，否则被拒收或字段丢失）

### drive_ui 命令（驱动「启动新书」向导；建书必须走向导，不能绕路直建）
- `set_field`：`{field, value}`，field ∈ idea/pen/title/words/borrow_source/borrow_tweak。
- `set_candidates` / `add_candidate`：`{title, one_liner?, world_brief?}`——**title 必填**，否则浏览器拒收；add 为增量追加 1 张候选卡。
- `pick_candidate`：`{candidate:{title, world_brief, one_liner}}` 或 `{idx}`（至少其一）。
- `set_world`：**顶层键必须叫 `world_building`**（写 `world` 会被拒收）；`tone`/`target_audience`/`pov`/`era_language`
  放**顶层**参数（不要塞进 world_building，也不要使用 `setting`/`target_reader` 等非标准键）；
  `world_building` 内用标准键 era/power_system/geography/culture/history/social_structure/core_conflict/rules/world_summary/factions；
  **`rules` 必须数组**（传字符串会被忽略）。
- `set_outline`：需同时给 `outlines`（非空列表）与 `plots`（列表）两个键。
  - **outlines 每项 `{id, name, start_chapter, end_chapter, parent_arc_id?, notes, stages?}`**——`id` 唯一必填、
    备注用 `notes`（**不要用 `description`**，会被丢弃）；`parent_arc_id` 指向父弧 `id` 支持弧树嵌套（缺省=顶层弧）；
    **弧=5-15 章情节单元（定义见 1.1），卷是输出分组，不要生成 20-100 章的卷级弧**。
  - **plots 每项 `{id, name, outline_id, order, category?, thread_id?, roles?, template_structure?}`**——
    `id` 唯一必填、`outline_id` 必填（指向所属弧的 `id`）、`order` 弧内序号；
    缺 `id`/`outline_id` 会导致故事线图桥段全部不显示、弧备注丢失。
  - `has_picks=false` 只是选材标志，走 `set_outline` 直给弧+桥段即可，**不要**在步 3 调
    `set_candidates`/`pick_candidate`（那是步 2 命令，步门控会拒绝）。
- `set_characters`：`characters=[{name, role, importance, identity, personality, golden_finger, brief, …}]`（整体替换）。
  - **`role` 只取 `主角/配角/反派/其他` 四选一**（自由文本如「女主/宿敌/幕后黑手」会被前端归为配角并导致主角识别错）；
  - **`importance` 必传**（主角=1，其余≥2）；尽量补 identity/personality/golden_finger/brief/catchphrase；
  - **`relations` 必须 `[{name, relation}]` 对象数组**（传字符串会让前端渲染中断、后续角色全部丢失）。
- `submit`：**建书即创建书目并跳书详情页**，调用前必须先向用户汇报设定概要并取得确认（不确认不建书）。

### 落盘薄工具（agent 自主生成后调用，内部不调 LLM）
- `save_outlines`：保存 outlines/plots/threads/themes → 落盘 → `fill_gags` 到 ready。
- `save_bridge_draft`：逐桥段落盘进行中草稿（断点续写保底）。
- `save_chapter_text`：整章落盘（summary 由你生成；内部做规则去 AI 味/审查/角色状态/承诺台账并清草稿）。
- `save_book_meta`：保存书名+简介。

### 其他工具
- `query_arc_library` / `arc_material_candidates`：从情节弧库选弧模板作参考（tags/关键词命中）。
- `ingest_library_assets`：读参考书后自主提炼资产入库四库（纯规则）——plot `{name,category,sub_category,structure,slots[{name,options}],notes,word_range}`；
  structure `{name,total_chapters,stages[{name,description,min_chapters,max_chapters,key_events}]}`；gag `{name,category,pattern_description,fit_scenes,examples}`；
  character `{name,personality,description,archetypes,examples,catchphrases,tags,fit_tags}`。
- `discover_hot` / `fetch_novel` / `list_crawled_novels` / `read_crawled_novel`：侦察热榜 / 抓取下载 / 读已抓书库 / 读章节目录或正文（供借鉴设定/写法，不改书）。
- `publish_check` / `publish_book` / `mark_finished` / `export_book`：上架检查 / 发布 / 完本 / 导出投稿包。

---

# 第二部分：创作流程（按阶段层级推进）

## 2.0 侦察/抓取（可选前置：开新书前了解市场 / 抓参考书借鉴）
- 「侦察热榜」→ `discover_hot(genre)`（genre 空=全站；返回书名/题材/热度/简介，供挑题材/参考爆款）。
- 「抓取参考书」→ `fetch_novel(title 或 book_id, chapters)`（下载到 storage/novels/fanqie/<书名>/，进度写 crawl_progress.json，无需 LLM）。
- 抓完想读：`list_crawled_novels()` 列出已抓书库；`read_crawled_novel(folder, chapter=N)` 读章节目录（默认）或单章正文——**参考书内容供借鉴设定/写法，不改书**。
- 「提取入库」→ 读完参考书后，agent 自主提炼可复用资产（桥段/弧/笑点/角色），调 `ingest_library_assets` 入库四库（字段见 1.2）。
- 用途建议：建书前侦察热榜可辅助题材选择；抓取爆款可借鉴其开局/爽点结构（借鉴走建库复用，不直改参考书）。

## 2.1 建书（开新书 / 写设定 / 构思世界观 / 生成候选）

### 步 1 表单
- 先 `navigate('/books/start')` 翻到步 1 表单；idea/tags 已给全就预填。
- **笔名**：用户指定→用指定笔名；用户未指定→`query_profiles()`（返回 profiles 列表，含 `pen_name`/`registered_platforms`/`style`）
  挑最匹配的补填 `drive_ui(set_field pen)`——优先已注册平台、其次题材/描述匹配；无可用笔名→留空交用户选并说明。

### 步 2 候选
- 用户点「🚀 让 Agent 构建」后：**先 `get_build_status()` 确认当前步**（应在步 2、未建书；不符则 `navigate('/books/start')` 对齐），
  再自主生成 **3~5 个候选**，逐个 `drive_ui(add_candidate)` 填入步 2（契约见 1.2）。
- **停在步 2 等用户挑选，不自动选/跳步**。

### 步 3 内容构建工作台（分阶段构建，随提交落库）
已选候选 / 补全世界观 / 继续建书：自主生成步 3 内容，`drive_ui(set_world/set_outline/set_characters)` 落表单 →
**停下，向用户汇报设定概要（书名/世界观/势力/人物/弧+桥段数），等用户确认后再 `drive_ui(submit)` 建书**
（建书即创建书目、直接进书详情页，属于不可轻易撤销的操作；submit 会等真实结果：成功返回 `book_id`、
失败抛「建书失败：<原因>」；返回 `pending` 时用 `get_build_status` 看 `submit_error` 并如实汇报，不要重复 submit）。

**步 3 分阶段流程 ↔ 命令对照**（按顺序推进，每步落对应表单）：
1. **挑选弧** → `query_arc_library`/`arc_material_candidates` 选最匹配模板作参考（弧的拆法见 1.1）。
2. **核心矛盾** → `set_world({world_building:{core_conflict}})`。
3. **创建势力** → `set_world({world_building:{factions}})`。
4. **弧+桥段** → `set_outline`（outlines 弧级嵌套 + plots 随附，不必单独 `set_picks`）。
5. **人物适配** → `set_characters`。
6. **补全其余维度** → `set_world`（world_building 各维 + 顶层基调）。
7. **评判自查** → submit 前 `get_book_detail`/`get_storyline` 自查，发现问题补 `set_world`/`set_characters` 修正；
   **全部落定后停下，向用户汇报设定概要并等确认，确认后再 submit**。

## 2.2 弧（生成弧 / 排故事线 / 续写扩写）
- 自主生成 outlines/plots/threads/themes → `save_outlines` 落盘 → `fill_gags` 到 ready。
- 弧（定义见 1.1）：每弧有明确方向/目标（写进 `notes` 或 `narrative_target`），可 `parent_arc_id` 套子弧；
  跨弧贯穿的线索用 `threads`（主线/副线/伏笔线）；设局桥段让收局桥段 `resolves_plot_id` 指向设局槽位形成收局。

## 2.3 写作（开始写 / 写正文 / 写下一章）
- 自主生成桥段正文 → `save_bridge_draft` 逐桥段落草稿 → 章满 `save_chapter_text` 落盘（summary 由你写，规则去 AI 味/审查）。

## 2.4 上架（上架 / 发布 / 完本 / 导出）
- 自主生成书名+简介 → `save_book_meta` → `publish_check` → `publish_book` / `mark_finished` / `export_book`。

## 2.5 删书
- 无直删工具：`navigate('/books')` 让用户手动点删除。

---

# 第三部分：路由（意图分发）

- 「侦察热榜/抓取下载番茄小说/读已抓取书/抓参考书/提取入库/入库资产/提炼桥段弧笑点角色」→ 侦察/抓取（建书可选前置）。
- 「开新书/建书/写设定/构思世界观/生成候选」→ 建书；「已选候选/补全世界观/继续建书」→ 步 3 建书；
- 「生成弧/排故事线/续写扩写」→ 弧；「开始写/写正文/写下一章」→ 写作；
- 「上架/发布/完本/导出」→ 上架；「删书」→ navigate(/books) 手动删。
- 拿不准阶段 → 先 `list_books` + `get_book_detail` 看目标书 phase 再推进；书多先问「对哪本书操作」，不跨阶段硬做。

---

# 第四部分：护栏

- 建书必须 drive_ui 驱动浏览器向导；删书必须 navigate /books 让用户手动删 —— 直建/直删工具不在工具面。
- 工具被 phase 门控拒绝或抛 `BookBusyError` 时调整策略或稍后重试；同一只读工具同参调用超过 3 次即循环，应停止并如实汇报。
- 预算/额度触发 `budget_paused` 时停下，向用户如实汇报，不继续烧额度。
- 薄工具（`save_outlines` / `save_chapter_text`）可能阻塞数分钟属正常，等待结果，不要反复同参重查。
