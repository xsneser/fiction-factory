---
name: novel-scout
description: 侦察/抓取/提取阶段（建书可选前置）。侦察热榜/抓取下载番茄小说/读已抓取书/借鉴参考书/提取入库五库。流程:discover_hot 侦察 → fetch_novel/fetch_book 抓取下载 → list_crawled_novels/read_crawled_novel 读 → **整本扫读**：以读者身份顺序逐章读全书，读到可复用内容随手提炼五类资产(桥段/弧/笑点/角色/写作风格)，上下文变重即把压缩记忆落盘(extract_state)续段，直到读完；入库前先 query 库比对去重 → 可自主 ingest_library_assets 直入，也可 set_review 呈现等用户确认。给一个小说 id/书名即触发「下载→扫读→提炼→(入库)」。
---

# 侦察 / 提取（novel-scout）— 建书可选前置

> 抓取是纯工具（无需 LLM）；分析提取由你（agent）**以读者身份顺序通读全书**自主提炼。参考书内容仅供借鉴/入库复用，**不改书**。
> 字段契约见 NOVEL_AGENT.md 1.2；判据速查如下，全量方法论见 `docs/设计文档-外部书目提取-顺序通读式提取.md`。

## 流程
1. 「侦察热榜」→ `list_rankings(platform, gender)` 查榜单/分类清单 → `discover_hot(platform, key, count)` 拉榜单（key=榜单分类 id 或题材中文名，空=聚合综合热榜；返回排名/书名/题材/在读量/简介，供挑题材/参考爆款）。
2. 「抓取」→ `fetch_book(title 或 番茄 book_id 或 镜像站 URL, chapters, start_chapter?, end_chapter?)`（**综合抓取**：番茄解析元数据/简介/封面/权威章节目录 + 镜像站全文，合并到统一书库 `storage/novels/<书名>/`，进度写 `crawl_progress.json`，无需 LLM；番茄解析不到回退镜像站元数据）。
   按**真实章号**下载区间：用户给「第 A 章到第 B 章」→ `fetch_book(..., start_chapter=A, end_chapter=B)`；只给 `chapters` 时默认从 `start_chapter`（缺省 1）起 N 章。
   （`fetch_novel`=番茄专用、`fetch_webnovel`=镜像站专用，高级用；默认请用 `fetch_book`。）
3. 「读」→ `list_crawled_novels()` 列已下载书库（统一书库，platform 为 info 字段）。**书已下载时（任务文案给出 folder/书名）跳过抓取**：提取页「分析提取」任务即此场景，直接进第 4 步扫读，**不要再 `fetch_book` 重复下载**。
4. **「整本扫读 · 分析提炼（默认）」** → 像读者一样**从头顺序逐章读完全书**，读到可复用内容**随手提炼**，自己决定读几章收一个弧、何时入库。要点：
   - **顺序读窗（自由分段）**：先 `read_crawled_novel(folder, chapter=0)` 读一次章节目录（获 `chapter_count`）。然后**顺序推进**：一次读若干章用 `read_crawled_novel(folder, start_chapter=A, end_chapter=B)`（窗口大小自己定，1 章到几十章都行；每窗 ≤~25 章防结果被裁剪），零星补读用 `chapter=N` 单章。**不跳段、不回退**（必要时只回看少量）。
   - **随手提炼（读到即提，不等整本读完）**：在窗内识别并记录——① 一句能讲清的**桥段**（单场景事件）随手收；② 出现一个「读者可感知目标 + 起承转合张力」= **弧开口**，跟踪到它**阶段性收束**（达成/失败/转化为新威胁 + 节奏回落）就定成一个**弧模板**；③ 人物刻画起点（如「一个清冷女人开口」）→ 记角色原型；④ 好笑的「机制」→ 笑点。
     - 例（《十日终焉》）：开头「九个人，只剩十人时杀一人」的**游戏规则揭晓 = 一个桥段**；「老旧的钨丝灯泡，到后来没人记得它是谁留下的」这种**跨章的意象收束 = 一个弧的痕迹**；「清冷的女人」一开口的说话方式与反应 = **角色刻画的起点**。
   - **弧边界自由判断**：沿用边界信号（目标开合 / 完整张力呼吸 / 卷·副本·换地图标题 / 字数量级）。**不预设 1.5万–4万字硬框**——几千字的清晰小弧也收、跨大段的弧用 `children` 拆层（深度按真实结构，不要求均匀）。`total_words` = 该弧在书里的**真实字数跨度**，**绝不=全书**；弧模板内只表述字数、不含章数。
   - **入库决策（自主，入库前查重）**：每收束几段（或每段末）把候选汇总，逐条先查库比对——`query_arc_library` / `query_plots` / `query_gags` / `query_characters` 用候选的**标签/关键词/结构骨架/机制**去查已有模板，结构或机制**高度近似的不重名硬灌**：要么跳过，要么差异化（改名 + notes 写明与既有模板的差异点）再入。确认后**分批 `ingest_library_assets` 直接入库**（可边扫边入，也可攒一批再入）。入库即把名称记进 extract_state 的 `committed`，避免后续重复。
   - **写作风格**：扫读全程留意句式/用词习惯；风格规则在读过有代表性章节后产出，归属任务里带的笔名（`profile_id`，未指定默认「枫落」）——句式层转 `prefer`（如「句长偏短」），用词/套话层转 `ban`（带 `replacements` 触发去 AI 味替换 / 空=硬禁句式检测）；一档 3–8 条 prefer + 5–15 条 ban。
   - **记忆压缩（一段读完的关键动作）**：上下文预算变重时**停一轮**——把本段新增内容并进压缩记忆 → `extract_state(action=save, folder, state={book, cursor: 已读到第N章, memory:{digest, open_segments(未收束弧/悬念), people, unresolved}, committed, ...})` 落盘 → **停下汇报**「已读到第 N 章，记忆已压缩落盘；回复『继续』我从第 N+1 章续读」。经验阈值：已顺序读过 **~60–150 章** / 单次读窗工具结果明显变大 / 或自感上下文重，就应压缩续段，别硬顶到超限。下一段开头 `extract_state(action=load, folder, mode=summary)` 续读（只带 digest）。
   - **收尾**：已读到末章（`chapter_count`）或连续多个窗口无可提炼 → `extract_state(action=save, ...)` 置 `status=done`，汇总汇报（共提炼 N 弧 / M 桥段 / K 笑点 / 角色 / 风格规则，已入库 X 条），结束。**不要在同一窗口原地打转重复提炼**。
5. **「续段（用户回复『继续』）」** → `extract_state(action=load, folder, mode=summary)` 拿 {cursor, memory, committed} → 从 `cursor+1` 章继续第 4 步扫读循环，直到读完。
6. **「给用户过目（可选）」** → 仅当**用户明确要审查**而非自主入库时：把候选经 `drive_ui(set_review)` 呈现成可勾选审查卡（payload `{title, platform?, folder?, downloaded_chapters?, profile_id?, plots?, structures?, gags?, characters?, style_rules?}`——title 必填、五类至少一类非空），**停页面等用户勾选确认**；用户确认后由**页面** POST `/api/scout/ingest` 落库（不经 agent）。候选量大时分批呈现，别一次塞几百条。
7. 「直接入库（兜底）」 → 仅当用户明确要求直接入库且不需自主决策时，调 `ingest_library_assets` 写四库（不含风格；风格另用 `add_style_rule`）。

## 用途建议
- 建书前侦察热榜可辅助题材选择；抓取爆款可借鉴其开局/爽点结构（借鉴走建库复用，不直改参考书）。
- 提取入库完成后 → 进入建书流程（`novel-build-candidates`）。

## 退出状态
- 抓取完成：`fetch_book` 返回 {ok, title, author, intro, cover, saved_chapters, folder, already, platform, sources}。
- 整本扫读完成：`extract_state` 落盘 status=done，汇报「共提炼 N 弧 / M 桥段…已入库 X 条」。
- 段收尾续段：`extract_state` 落盘 status=running + cursor，汇报「已读到第 N 章，回复『继续』续读」。
- 呈现完成：`drive_ui(set_review)` 返回 {__ui_command__: "set_review"}，候选已上页等用户确认。
- 兜底入库：`ingest_library_assets` 返回 {ok, source, plots, structures, gags, characters}。

## 失败处置
- 抓取失败/超时 → 检查书名或 book_id 是否正确，稍后重试；进度实时看 `storage/crawl_progress.json`。
- `set_review` 被拒（字段不合规）→ 按报错修正 payload 重试；若页面不在 `/extract` 会提示「命令接收器未就绪」，先 `navigate('/extract')` 再重试。
- 扫读中途断/超时 → `extract_state(action=load, folder, mode=summary)` 看 cursor，从断点续段即可，已入库内容在 committed 里去重。
