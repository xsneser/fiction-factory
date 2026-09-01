---
name: novel-scout
description: 侦察/抓取/提取阶段（建书可选前置）。侦察热榜/抓取下载番茄小说/读已抓取书/借鉴参考书/分析提取入库五库。流程:discover_hot 侦察 → fetch_novel 抓取下载 → list_crawled_novels/read_crawled_novel 读 → 自主提炼五类资产(桥段/弧/笑点/角色/写作风格) → drive_ui(set_review) 呈现候选到提取页 → 停页面等用户确认后入库。给 agent 一个小说 id/书名即触发「下载→分析→呈现」。
---

# 侦察 / 提取（novel-scout）— 建书可选前置

> 抓取是纯工具（无需 LLM）；分析提取由你（agent）读正文自主提炼。参考书内容仅供借鉴/入库复用，**不改书**。
> 字段契约见 NOVEL_AGENT.md 1.2。

## 流程
1. 「侦察热榜」→ `list_rankings(platform, gender)` 查榜单/分类清单 → `discover_hot(platform, key, count)` 拉榜单（key=榜单分类 id 或题材中文名，空=聚合综合热榜；返回排名/书名/题材/在读量/简介，供挑题材/参考爆款）。
2. 「抓取」→ `fetch_book(title 或 番茄 book_id 或 镜像站 URL, chapters, start_chapter?, end_chapter?)`（**综合抓取**：番茄解析元数据/简介/封面/权威章节目录 + 镜像站全文，合并到统一书库 `storage/novels/<书名>/`，进度写 `crawl_progress.json`，无需 LLM；番茄解析不到回退镜像站元数据）。
   按**真实章号**下载区间：用户给「第 A 章到第 B 章」→ `fetch_book(..., start_chapter=A, end_chapter=B)`；只给 `chapters` 时默认从 `start_chapter`（缺省 1）起 N 章。
   （`fetch_novel`=番茄专用、`fetch_webnovel`=镜像站专用，高级用；默认请用 `fetch_book`。）
3. 「读」→ `list_crawled_novels()` 列已下载书库（统一书库，platform 为 info 字段）；`read_crawled_novel(folder, chapter=N)` 读章节目录（默认）或单章正文（folder 为唯一路径 key）。
   > **书已下载时（任务文案给出 folder/书名）跳过抓取**：提取页「分析提取」任务即此场景——直接 `read_crawled_novel(folder=...)` 读正文 → 第 4 步分析呈现，**不要再 `fetch_book` 重复下载**。
4. **「分析并呈现（默认）」** → 读完参考书后，自主提炼**五类资产**：
   - **桥段(plot) / 情节弧(structure) / 笑点(gag) / 角色(character)**：字段契约见 NOVEL_AGENT.md 1.2；
     **弧模板抽多层树**：`read_crawled_novel(chapter=0)` 看章节目录/字数定位各叙事弧的章节边界，再抽样读正文，
     把每个典型弧拆成**多层弧树**（大弧→子弧→阶段；`structure.stages` 即子弧，子弧用 `children` 继续嵌套，
     深度/分支按书里真实结构定、**不要求均匀**）。
   - **写作风格(style_rules)**：读若干章正文后提炼该书的句式风格/用词特点，转成规则列表
     `[{kind, pattern, desc?, severity?, replacements?}]`——`kind=prefer` 句式风格正向指令（如「句长偏短」）|
     `ban` 禁止内容（`replacements` 有值=AI 高频词自动去 AI 味替换、空=硬禁句式检测）；
     **归属用户所选笔名**（任务文案里带的笔名，落 `profile_id`；未指定时归默认笔名「枫落」）。
   - 调 `drive_ui(set_review)` 把五类候选呈现到提取页审查区：
     payload `{title, platform?, folder?, downloaded_chapters?, profile_id?, profile_name?,
     plots?, structures?, gags?, characters?, style_rules?}`（title 必填、五类至少一类非空）。
   - **然后停下，等用户确认**：不直接 ingest。汇报「已呈现，请在页面勾选确认入库」。
     用户确认后由**页面** POST `/api/scout/ingest` 落库（不经 agent）。
5. 「直接入库（兜底）」 → 仅当用户明确要求直接入库时，才调 `ingest_library_assets` 写四库（不含风格，风格另用 `add_style_rule`）。

## 用途建议
- 建书前侦察热榜可辅助题材选择；抓取爆款可借鉴其开局/爽点结构（借鉴走建库复用，不直改参考书）。
- 提取入库完成后 → 进入建书流程（`novel-build-candidates`）。

## 退出状态
- 抓取完成：`fetch_book` 返回 {ok, title, author, intro, cover, saved_chapters, folder, already, platform, sources}。
- 呈现完成：`drive_ui(set_review)` 返回 {__ui_command__: "set_review"}，候选已上页，等用户确认。
- 兜底入库：`ingest_library_assets` 返回 {ok, source, plots, structures, gags, characters}。

## 失败处置
- 抓取失败/超时 → 检查书名或 book_id 是否正确，稍后重试；进度实时看 `storage/crawl_progress.json`。
- `set_review` 被拒（字段不合规）→ 按报错修正 payload 重试；若页面不在 `/extract` 会提示「命令接收器未就绪」，先 `navigate('/extract')` 再重试。
