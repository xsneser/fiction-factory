---
name: novel-scout
description: 外部书库阶段：侦察热榜/抓取下载/已下载书读/整本扫读提取五库资产(桥段/弧/笑点/角色/风格)。核心=**顺序通读式提取**：对已下载书像读者一样顺序逐章读完全书，随手提炼、自由分段，上下文变重即 extract_state 压缩记忆续段，入库前 query_* 查重、可自主 ingest_library_assets 直入或 set_review 过目。工具用法见 NOVEL_AGENT.md 1.2；判据速查见下，全量方法论见 `docs/设计文档-外部书目提取-顺序通读式提取.md`。
---

# 外部书库（novel-scout）— 侦察 / 抓取 / 整本扫读提取

侦察与抓取是纯工具流程（无需 LLM）；正文提炼由你（agent）自主完成。参考书仅供借鉴/入库复用，**不改书**。

## 入口判断
- 要侦察热榜 / 下载某书且**书未下载** → 直接 `discover_hot`（拉榜单）/ `fetch_book`（按书名或 book_id 综合抓取；进度在 /scout 页实时显示）。
- 书**已下载**（任务文案给了 folder/书名，或提取页「分析提取」进入）→ **不要重复下载**，直接进整本扫读。

## 整本扫读 · 分析提炼（核心）
以读者身份从头顺序逐章读完全书，读到可复用内容随手提炼，自己决定读几章收一个弧、何时入库；上下文变重就压缩记忆续段，直到读完。

1. **准备**：`read_crawled_novel(folder, chapter=0)` 读一次章节目录拿 `chapter_count`（目录只读这一次）。
2. **顺序读窗（自由分段）**：`read_crawled_novel(folder, start_chapter=A, end_chapter=B)` 顺序读窗口（大小自定，一次 ≤~25 章防结果被裁剪）；零星补读用 `chapter=N`。**不跳段、不回退**。
3. **随手提炼（不等整本读完）**：窗内边读边记——一句能讲清的**桥段**随手收；「读者可感知目标+起承转合张力」出现=**弧开口**，跟踪到它**阶段性收束**（达成/失败/转化+节奏回落）就定一个**弧模板**；人物刻画起点→记**角色**；好笑的机制→记**笑点**；句式/用词习惯→记**风格素材**。
   - 例（《十日终焉》）：开头「九个人，只剩十人时杀一人」的规则揭晓 = **桥段**；「老旧的钨丝灯泡，到后来没人记得是谁留下的」跨章收束 = **弧**；「清冷的女人」开口的说话方式与反应 = **角色起点**。
   - 弧边界沿用：目标开合 / 完整张力呼吸 / 卷·副本·换地图标题 / 字数量级。**不设 1.5万–4万硬框**——小弧也收、大弧用 children 拆层。`total_words`=该弧真实字数跨度（非全书）。
4. **入库决策（自主，先查重）**：每收束几段（或段末）把候选汇总，逐条先 `query_arc_library`/`query_plots`/`query_gags`/`query_characters` 用标签/关键词/结构骨架/机制查库——**高度近似不重名硬灌**：跳过，或差异化（改名+notes 注明差异）再入。确认后**分批 `ingest_library_assets` 直入**（可边扫边入）；入库名记入 `extract_state.committed` 防后续重复。
   - 风格规则：读过有代表性章节后产出，归属任务带的笔名（未指定默认「枫落」）：句式层 `prefer`、用词/套话层 `ban`（带 `replacements` 触发去 AI 味替换/空=硬禁）；一档 prefer 3–8 + ban 5–15 条。
5. **记忆压缩（一段读完就停）**：上下文预算变重（经验：顺序读过 ~60–150 章 / 单窗结果明显变大 / 自感重）→ 把本段并进压缩记忆 → `extract_state(action=save, folder, state={book, cursor=已读到第N章, memory:{digest, open_segments, people, unresolved}, committed})` → **停下汇报**「已读到第 N 章，记忆已压缩落盘；回复『继续』我从第 N+1 章续读」。别硬顶到超限。
6. **收尾**：读到末章（chapter_count）或连续几窗无可提炼 → save 置 `status=done`，汇总（共 N 弧 / M 桥段 / K 笑点 / 角色 / 风格规则，入库 X 条），结束。**不要原地打转重复提炼**。

## 续段 / 过目
- **续段**（用户回复「继续」）→ `extract_state(action=load, folder, mode=summary)` 取 {cursor, memory, committed} → 从 `cursor+1` 章继续第 2–6 步。
- **过目（可选）**：仅当用户要审查而非自主入库 → `drive_ui(set_review)` 分批呈现候选（`title` 必填、五类至少一类非空），**停页面等用户勾选**；确认后由页面 POST `/api/scout/ingest` 落库（不经 agent）。

## 退出状态
- 抓取完成：`fetch_book` 返回 {ok, title, author, intro, cover, saved_chapters, folder, already, platform, sources}。
- 扫读完成：`extract_state` 落盘 status=done，汇报「共 N 弧 / M 桥段…入库 X 条」。
- 段收尾续段：`extract_state` 落盘 status=running+cursor，汇报「回复『继续』续读」。
- 过目完成：`drive_ui(set_review)` 返回 {__ui_command__: "set_review"}，候选上页等确认。

## 失败处置
- 抓取失败/超时 → 核对书名/book_id 重试；进度实时看 `storage/crawl_progress.json`。
- 扫读中断/超时 → `extract_state(action=load, folder, mode=summary)` 看 cursor 从断点续；已入库内容在 committed 里去重。
- `set_review` 被拒（字段不合规）→ 修正 payload 重试；页面不在 `/extract` 先 `navigate('/extract')` 再试。
