---
name: novel-scout
description: 侦察/抓取阶段（建书可选前置）。侦察热榜/抓取下载番茄小说/读已抓取书/借鉴参考书/提取入库。流程:discover_hot 侦察 → fetch_novel 抓取下载 → list_crawled_novels/read_crawled_novel 读 → ingest_library_assets 提取入库四库。
---

# 侦察 / 抓取（novel-scout）— 建书可选前置

> 纯抓取与入库，无需 LLM 生成；参考书内容仅供借鉴设定/写法，**不改书**。
> 字段契约见 NOVEL_AGENT.md 1.2。

## 流程
1. 「侦察热榜」→ `discover_hot(genre)`（genre 空=全站；返回书名/题材/热度/简介，供挑题材/参考爆款）。
2. 「抓取参考书」→ `fetch_novel(title 或 book_id, chapters)`（下载到 `storage/novels/fanqie/<书名>/`，进度写 `crawl_progress.json`，无需 LLM）。
3. 抓完想读：`list_crawled_novels()` 列出已抓书库；`read_crawled_novel(folder, chapter=N)` 读章节目录（默认）或单章正文——**参考书内容供借鉴设定/写法，不改书**。
4. 「提取入库」→ 读完参考书后，自主提炼可复用资产（桥段/弧/笑点/角色），调 `ingest_library_assets` 入库四库（plot/structure/gag/character 字段契约见 NOVEL_AGENT.md 1.2）。
   **弧模板抽多层树**：`read_crawled_novel(chapter=0)` 看章节目录/字数定位各叙事弧的章节边界，再抽样读正文，把每个典型弧拆成**多层弧树**（大弧→子弧→阶段）——深度/分支按书里真实结构定，**不要求均匀**（有的弧一层、有的两层、有的子弧再拆到三层）；`structure.stages` 即子弧，子弧用 `children` 继续嵌套。

## 用途建议
- 建书前侦察热榜可辅助题材选择；抓取爆款可借鉴其开局/爽点结构（借鉴走建库复用，不直改参考书）。
- 抓取/入库完成后 → 进入建书流程（`novel-build-candidates`）。

## 退出状态
- 抓取完成：`fetch_novel` 返回 {ok, title, author, saved_chapters, folder, platform}；入库完成：`ingest_library_assets` 返回 {ok, source, plots, structures, gags, characters}。

## 失败处置
- 抓取失败/超时 → 检查书名或 book_id 是否正确，稍后重试；进度实时看 `storage/crawl_progress.json`。
