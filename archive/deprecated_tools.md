# 已存档的厚工具（工具内调 LLM，已移出 MCP）

> 这些工具在 2026-08-23 架构重构中**已从 MCP 工具注册表移除**（agent 不可调用）。
> 原因：工具内部调 LLM 损失信息传递效率（见 `docs/架构决策-工具内LLM-bug与agent接管生成.md`）。
> 迁移方向：agent 在自身上下文自主生成 → 调薄工具 `save_*` / `drive_ui` 落盘填入。
>
> **代码位置**：函数定义仍保留在 `agent_tools.py`（带 `[DEPRECATED]` 标记），只是不注册进
> `TOOL_REGISTRY`。如需恢复，从 `_build_registry` 的 `fns` 列表加回即可。

## 存档清单（16 个）

| 工具 | 旧行为 | 替代（agent 生成 → 薄工具/驱动） |
|---|---|---|
| `write_next_bridge` | 引擎内 LLM 逐桥段写正文 | agent 生成桥段 → `save_bridge_draft` |
| `write_chapter` | 引擎内 LLM 写整章 | agent 生成整章 → `save_chapter_text` |
| `generate_title` | LLM 生成书名 | agent 生成 → `save_book_meta` |
| `generate_outlines` | LLM/规则生成大纲 | agent 生成 → `save_outlines` |
| `generate_full_outline` | 6 阶段 LLM 完整大纲 | agent 生成 → `save_outlines` + `fill_gags` |
| `generate_outline_preview` | 步3内 LLM 大纲+直推向导 | agent 生成 → `drive_ui(set_outline)` |
| `extend_outline` | 续写追加大纲弧 | agent 生成 → `save_outlines(mode=append)` |
| `outline_agent` | LLM 解析修改意图 | agent 直接生成 → `save_outlines` |
| `generate_world` | LLM 生成世界观 | agent 生成 → `save_basic_info` |
| `world_candidates` | LLM 生成世界观候选 + 自动填步2 | agent 生成候选 → `drive_ui(add_candidate)` |
| `generate_characters` | LLM 生成角色 | agent 生成 → `save_basic_info` / `drive_ui(set_characters)` |
| `generate_core_conflict` | LLM 生成核心矛盾 | agent 生成 → `drive_ui(set_world)` |
| `generate_factions` | LLM 生成势力 | agent 生成 → `drive_ui(set_world)` |
| `generate_rest_world` | LLM 补全其余世界观维度 | agent 生成 → `drive_ui(set_world)` / `save_basic_info` |
| `generate_book_meta` | LLM 生成书名+简介 | agent 生成 → `save_book_meta` |
| `fill_plots` | 规则匹配桥段（含多余 LLM 依赖） | agent 提供 plots → `save_outlines` |

## 相关 skill

novel-build-candidates / novel-build 已改为 agent 自主生成 → `drive_ui` 填入；
novel-outline / novel-write / novel-publish 已改为 agent 自主生成 → `save_*` 落盘。
