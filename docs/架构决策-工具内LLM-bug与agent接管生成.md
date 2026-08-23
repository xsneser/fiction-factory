# 架构决策：工具内 LLM 是 bug → agent 接管生成，工具薄化为填入/落盘

## 背景（为什么这是 bug）

旧机制下，dsh agent 是「厚工具的门面」——`write_next_bridge` / `generate_outlines` / `generate_world` 等 15 个 MCP 工具**内部调 LLM 生成内容**。这与「Claude Code 调本地视觉模型」同构：agent 传个 book_id/意图，工具内部 LLM 从头生成，再把结果压成摘要还回来。

**这个机制损失信息传递效率**（用户确认是最大 bug）：

1. **上下文断裂**：工具内部 LLM 是全新上下文，不知道 agent 在想什么、上一轮规划了什么；它从文件重新组装上下文，丢掉了 agent 对话里积累的细腻理解（人物弧线/风格手感/前后文呼应）。
2. **双重 LLM 浪费**：agent 先「思考该调工具」花一轮 token，工具 LLM 再「从头生成」花一轮；前者基本白费。
3. **结果压缩**：工具只回传摘要/状态，agent 看不到生成过程的推理；且 **>8KB 被 dsh 裁剪**（`tool-result-pruner`）。
4. **无法连续创作**：agent 看不到正文细节，难以延续语气、呼应伏笔，只能在「结果摘要」层面隔靴搔痒。

## 开源 skill 生态共识（.research/ 实证）

- **novel-skills / creative-writing-skills / deep-novel-system**（纯 skill 套件）：LLM 生成 **100% 在 agent 对话循环内**——skill 只是提示词/规则手册，agent 每次 LLM 输出正文/大纲/角色，然后工具只做**薄操作**（Write 落盘、复制模板、保存状态、读上下文）。**没有任何 skill 套件有「工具内部调 LLM」的脚本**。
- 引擎类项目（AI-Novel-Writing-Assistant / OpenNovel）才是「厚操作」（工具内部调 LLM）——NovelEngine 旧机制正属此类。

## 迁移方向（已实施）

**agent 生成一切 → 工具薄化为 save_* 落盘（内部零 LLM）**：

| 阶段 | 旧（工具内 LLM） | 新（agent 生成 + 薄工具） |
|---|---|---|
| 写作 | write_next_bridge / write_chapter | agent 逐桥段生成正文 → `save_bridge_draft` / `save_chapter_text` |
| 大纲 | generate_full_outline / generate_outlines(ai) | agent 生成大纲+桥段 → `save_outlines` → `fill_gags`(规则) |
| 世界观/角色 | generate_core_conflict / factions / characters / rest_world | agent 生成 → `save_basic_info` / `drive_ui(set_*)`（书未建前） |
| 元数据 | generate_book_meta | agent 生成书名/简介 → `save_book_meta` |

**规则质检保留**（薄工具，无 LLM）：`deai_text` / `review_text` / `diagnose_*` / `tag_punch_points` / `chapter_quality_gate` / `publish_*`——agent 生成后调用做后处理。

**复用资产**：`libraries/prompt_harness.py` 是纯提示词渲染器（不调 LLM），可作 agent 侧提示词参考。

## 旧工具处置（废弃留档）

以下 15 个工具 docstring 已标 `[DEPRECATED]`，**保留定义可回退**，但主流程不再使用：

`write_next_bridge` `write_chapter` `generate_title` `generate_outlines` `generate_full_outline` `generate_outline_preview` `extend_outline` `outline_agent` `generate_world` `world_candidates` `generate_characters` `generate_core_conflict` `generate_factions` `generate_rest_world` `generate_book_meta`

## 相关 skill

novel-write / novel-outline / novel-build / novel-publish 已改为 agent 自主生成 + save_* 薄工具流程。
