---
name: novel-scout
description: 外部书库：侦察、抓取和顺序扫读提取桥段/弧/笑点/角色/风格。暂存区收集 agent 的全部可复用候选，候选可持续修改；用户确认后才能入库。工具契约见 NOVEL_AGENT.md 1.2。
---

# 外部书库（novel-scout）

侦察/抓取直接调用工具；顺序阅读和提炼由 agent 编排。`/extract` 暂存区是 agent 的完整候选工作区：候选先放入、可随时补充/修改/替换，用户确认后才由页面入库。参考书只读，不修改原书。

## 入口判断
- 未下载：按需调用 `discover_hot` 或 `fetch_book`。
- 已下载（已有 folder 或从「分析提取」进入）：跳过抓取，直接扫读。

## 顺序扫读与提炼

这是一个自适应循环，不是固定的“读完一批才提取”任务。读到有价值的内容即可形成候选；候选先进入 `/extract` 暂存区，由 agent 持续维护完整的当前候选集。**任何候选未经用户确认不得入库**。

1. 首次调用 `read_crawled_novel(folder, chapter=0)` 获取 `chapter_count`；不要反复读目录。
2. 从头顺序读窗口 `start_chapter=A, end_chapter=B`（单窗约 25 章以内），不得跳读或回退；必要时用 `chapter=N` 补读。
3. 边读边提炼：桥段可以在单章出现时记录；弧先记录为 `open`，目标阶段性收束后再定稿；人物先积累证据，至少有 2–3 处可复用特征再形成角色候选；喜剧机制和代表性句式/用词随时记录。弧按真实边界拆分，不按固定字数硬切。
4. agent 自主决定节奏：可以读完一章立即处理，也可以连续读数章后统一提炼；候选成熟或后续发现新证据时，都可用 `drive_ui(set_review)` 提交**当前完整候选集**，从而新增、修改或替换暂存条目。纯过渡、纯换皮或仅绑定本书的内容记入 digest，不强行送审。
5. 候选进入暂存区前，先用对应 `query_*` 查重。按标签、关键词、结构骨架和 gag 机制比较；高度近似则跳过或差异化。用 `_mechanism_key` 做机制级去重。查重结果和候选修改由 agent 自主维护，不因暂存而写入 `committed`。
6. **强制审查入库**：所有候选必须经 `drive_ui(set_review)` 进入 `/extract` 暂存区；页面复用确认入库代码，用户可随时勾选部分或全部候选。只有用户确认的条目才由页面调用 `/api/scout/ingest`；未勾选条目继续保留，agent 可再次提交更新后的完整候选集。agent 不得直接调用 `ingest_library_assets` 或绕过审查。闸门返回的 `incomplete`、`duplicate`、`book_archive` 按 `judge.reasons` 修正。
7. 风格归属任务笔名，读到代表性章节后直接生成/修改 `prefer` 与 `ban`；不必等全书读完。
8. 上下文变重时，在当前已读游标处把任意前缀（例如前 50 章）合并成摘要，保存
   `extract_state(action=save, state={book, cursor, memory, committed, segments_log})`；阈值不是固定章数。下一段
   `load(summary)` 后从 `cursor+1` 继续，直到 `chapter_count`。

## 退出状态
- 抓取完成：汇报 `fetch_book` 的 `folder`、`already` 和保存结果。
- 扫读完成：`extract_state` 保存 `status=done`，汇报各类提取及入库数量。
- 上下文过重：先处理当前已成熟候选，把已读内容压缩进 `memory`，保存 `status=running` 和 `cursor`，然后在同一任务中继续下一窗口。
- 暂存/过目：未确认候选保持在暂存区；用户部分确认后只移除已确认条目，其余候选继续等待或由 agent 修改。

## 失败处置
- 抓取失败/超时：核对书名或 `book_id` 后重试，并查看 `storage/crawl_progress.json`。
- 扫读中断：加载 `extract_state` 从 `cursor` 续读，依靠 `committed` 去重。
- `set_review` 拒绝：修正 payload；不在 `/extract` 时先导航到该页。
