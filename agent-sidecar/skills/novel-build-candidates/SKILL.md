---
name: novel-build-candidates
description: 建书 步1-2。开新书/建书/写设定/构思世界观/生成候选。流程:navigate /books/start 步1 表单(预填 idea/tags,笔名策略 query_profiles) → 步2 自主生成 3~5 候选 add_candidate 逐张呈现 → 停在步2 等用户挑选,不自动选/跳步。
---

# 建书 步1-2：表单 + 候选呈现（novel-build-candidates）

> 护栏：建书只能 `drive_ui` 驱动浏览器向导（`navigate('/books/start')`），直建工具不在工具面。
> 本 skill 只到步 2：生成候选并**呈现**到步 2，**停在交互点等用户挑选，不自动选/跳步**；用户选完点「已挑选完毕」后，页面自动触发 `novel-build`（步 3 建书）。

## 步 1 表单
- 先 `navigate('/books/start')` 翻到步 1 表单；idea/tags 已给全就预填。
- **idea / 题材标签从哪来**：向导交接任务里会带上（`一句话设定「…」题材标签「…」`）；
  **文本没带全时用 `get_build_status()` 读 `idea` / `tags`**——提交前服务端只有这一份表单快照，
  是「用户到底选了什么」的权威。**绝不**在不知道题材标签的情况下凭空生成候选。
- **笔名**：用户指定→用指定笔名；用户未指定→`query_profiles()`（返回 profiles 列表，含 `pen_name`/`registered_platforms`/`style`）
  挑最匹配的补填 `drive_ui(set_field pen)`——优先已注册平台、其次题材/描述匹配；无可用笔名→留空交用户选并说明。

## 步 2 候选
- 用户点「🚀 让 Agent 构建」后：**先 `get_build_status()` 确认当前步**（应在步 2、未建书；不符则 `navigate('/books/start')` 对齐），
  再自主生成 **5~7 个候选**，逐个 `drive_ui(add_candidate)` 填入步 2（契约见 NOVEL_AGENT.md 1.2）。
- **候选必须贴着用户的 idea 与题材标签**：每张卡都要能对上所选标签；不要给与标签无关的方向
  （做错了用户得整批重来）。
- **候选详情要充实**：每张卡 `world_brief` 写 **150~250 字详细世界观设定**（覆盖 ①时代/世界背景 ②主角身份与处境 ③金手指/核心矛盾 ④题材卖点与开篇钩子），让用户只看卡片就能判断方向；`one_liner` 保持一句话核心设定（不写长）。
- **停在步 2 等用户挑选，不自动选/跳步**。

## 退出状态
- 候选已呈现，停在步 2。用户挑选完毕 → 页面触发 `novel-build`（步 3）。

## 失败处置
- `drive_ui` 后浏览器没反应 → `navigate('/books/start')` 再试。
- 候选卡 title 缺失被浏览器拒收 → 保证 `{title, one_liner?, world_brief?}`，**title 必填**。
