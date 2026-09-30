---
name: novel-style-match
description: >-
  对照一本参考书，把一个笔名（如 枫落）的写作风格库重写成它的风格。
  触发词：对照/匹配/仿写某本书的风格、给某笔名重写/重建写作风格库、让笔名学某本书的笔风、
  风格对齐到《XXX》、参考《XXX》改笔名风格。流程=量化参考书→决策禁词/身份→分轮重建规则→
  dsh 写样段→逐项对比→按差异迭代（人以读定稿，数字只定位）。
  前置：参考书已在书库（list_crawled_novels / fetch_book）；目标笔名存在或新建；58080 在跑。
  护栏：改动共享笔名/整库清空重建须先 AskUser 显式点头；不直改 style_rules.jsonl（不热载）；
  不跑 test_all（会非幂等重写该文件）。
---

# 笔名风格匹配（novel-style-match）

把「仿写某参考书 → 重建某笔名风格库」做成可复现闭环，靠**中间产物可量化/可被生成验证**逼出深度，
而不是一句「多想想风格」——这正是与一次性 prompt 的区别。

> 首次实例见 `examples/十日终焉→枫落.md`（量化表 / 7 轮规则演进 / 最终 35 条清单 / 全部坑与解法），
> 照抄其套路最快。

## 工具与脚本（相对本 skill 目录）

- 量化参考书：`python scripts/style_scan.py <folder>`
  - 读 `storage/novels/<folder>/chapters/*.json`；默认取样 前 20 章 + 第 300/800 起各 20 章。
  - 输出：引号/括号体系（“/「/（）、句末标点（。！？…）、句长(均值/中位/p90/≤6占比)、
    单句段占比、对话占比、语词密度（缓缓/仿佛/似乎/忽然/居然/顿时/只见/然而/所以/说道/问道/难道…，>0 才列）。
- 样段 vs 参考对比：`python scripts/style_cmp.py <sample.txt> <folder> [ch=1]`
  - 同维度双列 + ✓ 对齐标记；默认比参考第 1 章。
- 规则一键写回（防并行还原/防转写漂移）：`python scripts/build_rules_replay.py <spec.json>`
  - spec 形如 `[{"kind":"prefer","profile_id":"profile_001","pattern":"…","desc":"…","severity":"warning","replacements":[],"enabled":true}, …]`
  - 保留他 profile 原行，重建指定 profile 后写 `libraries/data/style_rules.jsonl`，打印 kind 计数。

## 流程 steps

0. **前置与护栏（先做）**
   - `query_profiles` / `get_pen_style` 确认目标笔名现状；确认参考书已抓取。
   - **改动共享笔名 / 整库清空重建 / 换人格 = 会被权限分类器盯**（2026-09-05 实测：选项标签「是」不够，
     规模操作被拦）。先用 AskUserQuestion 给出明确选项（改造现有笔名 / 新建专用笔名 / 仅加分轮微调），
     用户点名后才动。回滚备份：`cp libraries/data/style_rules.jsonl libraries/data/style_rules.jsonl.bak-<ts>`。
1. **量化参考书**：`style_scan.py <folder>`，读表。
2. **决策禁词与身份**
   - 禁词：**源文高频自然用的词放开**（如 缓缓/仿佛/似乎/忽然/居然/竟然——不是 AI 味，是本书节奏）；
     **源文避用 + AI 俗词才禁**（然而/因此/与此同时/顿时/只见/不禁/微微一笑/眼中闪过一丝…）；
     空替换=硬禁检测别滥（多了 save 审查会误伤拒稿）。带替换=自动去AI味。
   - 身份：`profiles/<id>.json` 的 `style_fingerprint.humor_style/action_style`、
     `tropes.chapter_hook_style/scene_pacing`、`description` 随风格一起改（无 MCP 工具→直改文件，gitignore 区，
     新进程 dsh 会读到）。`libraries/profiles.py` 里 zh 通用语言习惯/纪律硬编码若与新风格相冲，也一并调和（会提交）。
3. **重建规则（分轮小步，别一次全建）**
   - 先正向基调 + 标点/引号体系 → 再逐轮按对比差异微调；每轮 1–5 条小步优于一次灌 20 条。
   - 走 MCP `add_style_rule`/`delete_style_rule`（**别直改 style_rules.jsonl——运行进程不热载**；
     真要整体重建用 `build_rules_replay.py` 一次性写）。
   - **每轮把最终规则规格导出为 JSON**（给 build_rules_replay 用）→ 并行窗口还原 / 会话翻页都不丢。
3b. **提炼 STYLE REFERENCE 样文库入库（样本驱动加分项）**
   - 规则只能兜底，真正的语言惯性靠**人工样文**（STYLE REFERENCE）。重建规则后，从参考书里按
     不同场景类型截「**完整连续场景**」成条入库：`add_style_sample(profile_id, text, title, scene_tags, source, note)`。
   - `scene_tags` 填**场景类型**（场景开场/推理观察/多人对白/冲突·威胁/规则·死亡/过渡·日常/独处·心理…，
     自由扩展），别填角色名；`source` 记书与起止。
   - 每条 1500-3000 字为宜；**原文照搬不润色**——重复/普通解释段/笨拙处是作者语言惯性，别「优化」掉；
     别只截金句/漂亮比喻/纯高潮。
   - 凑 3-5 条覆盖不同场景，让模型看到「同一作者在不同情况下的写法」。写完 `list_style_samples` 复查；
     注入全量由系统负责（`get_pen_style` 的 `ref_summary` 可见 N/N）；想聚焦某场景再 `get_style_sample` 拉。
     样文库是**全局词条库**(不分笔名),`add_style_sample` 写库即被各笔名 `get_pen_style` 注入;
     人最终在 `/samples` 样文库页定稿;**已有词条别整批覆盖**,逐条 add/编辑或去重在页面保存。
4. **dsh 写样段（重置法，默认）**
   - 取现成 phase=ready 样书（`get_book_detail` 确认 ready+outlines/plots 非空），**重置到第 1 章前**：
     删 `books/<id>/chapters/*.json` 与 `character_states.json`；`book.json` → `current_chapter=0 / total_words=0 /
     status=planning`；`storyline.json` 全 plot `written_chapter=0`（phase 保持 ready）。
   - 触发 headless dsh：`curl -sN -X POST http://127.0.0.1:58080/api/agent/chat -H 'Content-Type: application/json'
     -d '{"messages":[{"role":"user","content":"…按笔名最新风格整章重写第1章(首个情节段)，约3000字 save_chapter_text 落盘，不要中途停下问…"}],"debug":false}'`
     （后台跑 + 轮询 `book.json` 的 `current_chapter≥1 && total_words>0` 取正文）。
   - 书库无 ready 样书才新建（走建书向导，护栏同 novel-build）。
   - 每次只重置+重写**同一章**做 A/B，别一本写到底。
5. **对比**：`style_cmp.py <sample> <folder>` 读差异表。
6. **迭代**：差异 → 微调规则 → 重置 → 重写 → 复比，**直到人读认可**。数字只定位差异，不用它判「好」。
   - 改完规则由**下一次 dsh 新任务**生效（每次 headless 新进程读 get_pen_style），当前在途任务不吃新规则。

## 用户点名必查维度（首次实测反复踩，逐个核）

1. 对话用全角双引号 “”；强调/概念/数字/拟声用直角引号 「」；正文**不用圆括号（）括注**、不用单引’。
2. 句号放在「」**外侧**（「十二」。），不要把整句含句号塞进「」。
3. 日期/年份/编号整段括（「2050年3月14日」），别拆成「2050」年「3」月「14」日。
4. 叹号密度低；设问/自问与省略号…… 密度按参考书；感叹 ≈ 0。
5. 对话占比、说道/问道/开口 前引标签按参考书。
6. 禁正文破折号 `——`（参考书正文几乎不用，仅个别拟声延长）。
7. **句式错落、反公式**：禁固定字数规则（会把每句擀成同形）。要的是 5–12 字无逗号短句、
   两句一逗中句、带 3–5 逗号块的长链句（可达 35–55 字）交替，句号随语义落。
8. 参考书风格结论以**人读原文段落**为准，别只信统计。

## 坑 / 教训（全部实测）

- **直改 style_rules.jsonl 不热载**：运行进程(58080/长驻 MCP)缓存旧库；改后用 add/delete 或 replay 脚本，
  并让 dsh 走新任务（新进程）读。
- **test_all.py 非幂等重写 style_rules.jsonl**：风格测试段每次全量重写该文件 → 别为验证跑 test_all；
  跑了会把你刚重建的规则冲掉（2026-09-05 实例即被并行 test_all 还原到基线）。校验用 py_compile / 干跑脚本。
- **并行窗口会还原文件**：另一窗口可能用你的 `.bak` cp 还原 → 随时 `build_rules_replay.py` 一键重放；先确认冲突。
- **共享笔名被权限分类器拦**：换人格/整库清空 = Modify Shared Resources，先 AskUser 点名；杀卡死 dsh(node) 同理，
  只能杀**你自己触发**的那条且 PID 经用户确认。
- **硬禁(空替换)别滥**：save 审查检测到会要求改 → 用多会频繁拒稿；能自动替换的尽量给替换候选。
- **固定字数句长规则 = 公式化**：先「短句为骨」→碎；再「串 20-35 字」→ 每句同形两段。直接给**错落混排**语义，
  别给死区间。
- **断句/节奏问题靠人读，不靠统计判好坏**：指标能定位「句/千字过高」「无长链」，但「像不像十日」要人读定夺。
- **禁词放开依据源文词频**：只放开源文自然用的；源文避用的仍禁，避免模型照 AI 俗癖跑。

## 退出状态
- 一轮：量化表 + 重建后 `get_pen_style` 全量风格文本（身份+prefer+ban）自洽无自相矛盾。
- 收敛：人读样段认可；关键维度清单（见上）逐项核过。
- 建议把最终规则规格留一份 JSON 在仓库外/local，防还原；若要固化为长期参考，写 examples/。
