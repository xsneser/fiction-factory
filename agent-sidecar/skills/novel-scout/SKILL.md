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
4. **「分析并呈现（默认）」** → 读完参考书后，**代表性单弧采样**：只挑题材典型、可在别书复用的弧拆成**单弧模板**（≈1.5万–4万字），**不还原全书**、不做「顶层弧覆盖全纵轴」（那是平台书级规则）。提炼前先 `query_arc_library`/`query_plots`/`query_gags` 查库内去重。提炼**五类资产**（字段契约见 NOVEL_AGENT.md 1.2；决策判据速查如下，全量方法论见 `docs/设计文档-外部书目提取-代表性单弧采样.md`）：
   - **桥段(plot)**：叶弧内切**单场景事件**（0.3–2章/800–2500字）；`structure` 写箭头流程骨架 `[羞辱]→[隐忍]→[亮实力]→[震惊]→[后悔]`；剥掉主角名/金手指/数值、提成 `slots[{name,options}]` 变量槽——**只收结构清晰、可迁移、库里同类少的**。
   - **情节弧(structure)**：`read_crawled_novel(chapter=0)` 看章节目录，按边界信号（目标开合 / 完整张力呼吸 / 卷·副本·换地图标题 / 1.5万–4万字量级）在抽样正文里圈候选弧。**`total_words`=采样弧自身字数跨度，绝不=全书**。层级判断：目标能拆出 ≥2 个「递进、各自独立开合一次小张力、占 ≥~5千字连续篇幅」的子目标 → `children` 递归（深度/分支按真实结构、**不要求均匀**）；并列小额操作 → 收作本节点 `key_events`；一个都不够格 → 叶 stage。
   - **笑点(gag)**：抓「为什么好笑」的**机制**→ `pattern_description`（含结构句式）+`fit_scenes`+`examples`；绑定具体角色的口头禅笑点 → 归角色 `catchphrases`，不进 gag。
   - **内涵/母题(theme)**：弧模板整弧与每 stage 写 `themes:[{name, position(开头/中段/结尾), how(靠哪类事件让读者尝到)}]`——母题≠笑点；参考词汇 公平/成长的代价/身份与伪装/牺牲/归属感/传承与突破（可扩）。此为 scout 桥段母题传导的主要载体（THEME_PLOT_COMPAT 只覆盖内置 plot_dating）。
   - **角色(character)**：人设类型+口头禅+适配题材，剥与书名绑定的专属剧情；`examples` 注明出处角色。
   - **写作风格(style_rules)**：抽读 5–15 章 → 句式层转 `prefer` 正向指令（如「句长偏短」）、用词/套话层转 `ban`（`replacements` 有值=AI 高频词自动去 AI 味替换、空=硬禁句式检测）；**规则站在去 AI 味视角、不克隆作者**（平台不做拆书仿写/指纹）；一档 3–8 条 prefer + 5–15 条 ban；
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
