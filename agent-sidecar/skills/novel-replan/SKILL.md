---
name: novel-replan
description: 临近已承诺故事边界或既有预测被事实推翻时，依据最新故事状态延伸下一小段正式故事线。
---

# 增量续规划

1. 调 `get_story_state(book_id)`；若 `boundary.needs_replan=false`，停止，不重复规划。
2. 只把 `facts` 当作不可改事实；`forecast` 可修改或废弃，禁止回写已完成 plot 和章节。
3. 用短决策卡完成：当前问题、人物压力、读者问题、2–3 个不同方向、各自代价与人物影响、选中方向及原因。
4. 把选中方向压成下一批可执行 plot，**直到累计承诺字数达到约 9000**（`get_story_state` 的
   `boundary.replan_target_words`，≈3 章；段数上限 20 只是安全网）。按新粒度，**一个 plot =
   一个主要戏剧变化**：`primary_turn` 必填、`words` 过渡 250~450 / 推进 450~700 / 冲突 600~850 /
   关键 800~1050 / 高潮 850~1100、**硬上限 1200**，一章通常 4~6 段；出现时间跳跃 / 地点转换 /
   冲突对象变化 / 新独立问题 / 双高潮就拆。每个 plot 明确目标、阻力、选择、代价、结果，并至少
   造成一种不可逆变化；顺手标 `chapter_break_after`（写完这段适不适合断章）。
5. 新弧从现有 `committed.until_word` 连续追加；情节段只挂叶弧。远期内容只写入 `future_intents`，不得伪装成正式 plot。
6. 不直接调用 `save_outlines`。调用 `drive_ui(cmd="set_replan_preview", args=...)` 暂存预览；参数包含书、revision（必须等于当前故事线版本，服务端会校验）、诊断、2–3 个方向、选中方向、6–12 个 plots、可选 outlines 与完整 planning patch。用户在抽屉确认后才由服务端原子提交。
7. 若用户指定其他方向或要求重拟，只为该方向重新生成完整预览。状态变化时重新读取 `get_story_state`，不得移除 expected_revision 或强行覆盖。
8. 素材库默认不查；确需参考时最多一次主查询、coverage 不足最多补查一次。

只保存结构化决策结果，不输出或持久化私有思维链。
