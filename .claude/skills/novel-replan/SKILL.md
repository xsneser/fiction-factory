---
name: novel-replan
description: 临近已承诺故事边界或既有预测被事实推翻时，依据最新故事状态延伸下一小段正式故事线。
---

# 增量续规划

先读取 `get_story_state(book_id)`。仅当 `boundary.needs_replan=true` 时规划；已写事实不可改，forecast 可废弃。

生成 2–3 个明显不同的方向，比较人物选择、代价、线程推进、承诺兑现和读者拉力；将选中方向压成 3–8 个目标—阻力—选择—代价—结果完整的 plot。新增 committed 从当前边界连续追加，远期方向只进入 `future_intents`。

不要直接调用 `save_outlines`。调用 `drive_ui(cmd="set_replan_preview", args=...)` 暂存结构化预览，包含书、revision、诊断、2–3 个方向、选中方向、3–8 个 plots、可选 outlines 与 planning patch；只有用户在抽屉确认后才原子提交。用户改选方向时重拟该方向，状态变化时重新读取，不能强行覆盖。只保存结构化决策卡，不保存思维链。
