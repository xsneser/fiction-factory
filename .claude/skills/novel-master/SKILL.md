---
name: novel-master
description: >-
  NovelEngine 创作平台的统一入口。Use when the user wants to 写小说/开新书/生成弧/写正文/上架/续写
  (start the platform and drive any novel creation task on NovelEngine)。
  职责：① 确保平台服务（58080）已启动；② 按用户意图分发到分 skill（novel-build / novel-write /
  novel-publish）并执行；③ 用 get_book_detail 校验阶段，跨阶段时引导到正确 skill，
  不跨阶段硬做。本 skill 是调度器，具体流程见各分 skill 正文。
  注：弧构建/深化已并入 dsh 建书步3（深化式生成、用户提交即 phase=ready）；Claude 侧不做弧构建。
---
# NovelEngine 主 skill（启动器 + 调度表）

## 第一步：启动平台（只做一次）

1. 探测 `http://localhost:58080` 是否可访问（`curl -s -m 3 http://localhost:58080`）。
2. 未启动 → **必须用 `launch.bat` 打开可见的独立终端窗口**（用户可随时关窗停服）：
   - Git Bash：`cmd //c start "" launch.bat`（`start` 开新 cmd 窗口跑 launch.bat，命令立即返回不阻塞本会话；该窗口前台跑 `python ui/web_ui.py`，**关窗即停服**）。
   - PowerShell 备选：`Start-Process -FilePath .\launch.bat`。
   - **不要**再用 `run_in_background` 跑 `python ui/web_ui.py`——无可见窗口，用户无法手动关闭。
   - 等 1-2 秒再探测一次；未起再等/重试一次，仍不起则提示用户手动跑 `launch.bat`。
3. 服务就绪后**确认浏览器已打开**：`launch.bat` 会自动 `start "" http://localhost:58080`；若未开再 `python -m webbrowser http://localhost:58080` 兜底（跨 shell 无引号坑）。
4. 服务本已就绪 → 不重复启动，用 `mcp__novel-engine__navigate` 切到相关页面（浏览器已在轮询，能消费意图）。

## 第二步：判断用户意图，分发到分 skill

| 用户意图（中英触发词） | 分 skill | 分 skill 文件 |
|---|---|---|
| 开新书 / 建书 / 写设定 / 构思世界观 / 借鉴已有书 / 生成书名 / 开头几章 | `novel-build` | `.claude/skills/novel-build/SKILL.md`（注：dsh 侧/按钮流程已拆为 `novel-build-candidates`（步 1 候选呈现）→ `novel-build`（步 2 建书），与 Claude 侧交互式 `novel-build` 独立、不做镜像） |
| 生成弧 / 排故事线 / 深化弧 / 一键完整弧 | 交给 dsh 侧（建书步3 深化式生成 → 用户提交即 ready）；config 兜底补弧 → `save_outlines` → 用户在书详情「✅ 确认弧+桥段」；Claude 不代跑排弧 | `.claude/skills/novel-build/SKILL.md` |
| 写正文 / 写下一章 / 继续写 / 写桥段 / 续写 / 扩写（正文续写） | `novel-write` | `.claude/skills/novel-write/SKILL.md` |
| 上架 / 发布 / 完本 / 导出 / 生成书名简介 / 检查能否发书 | `novel-publish` | `.claude/skills/novel-publish/SKILL.md` |
| 删书 / 删除一本书 | 无 skill——`navigate("/books")` 让用户**手动点删除按钮**（护栏：直删工具不在工具面，外部 agent 不能删） | — |

分发方式：用 Read 工具读对应 `SKILL.md` 全文，按其「前置检查 → 决策点 → 批处理」逐步执行。

## 第三步：跨阶段引导（防硬做）

- 拿不准用户在哪个阶段 → 先 `mcp__novel-engine__list_books` 看有哪些书，再 `mcp__novel-engine__get_book_detail` 看目标书 `phase`：
  - `config` → 未落弧：引导 dsh 建弧（建书步3 深化式生成），或 Claude 侧只做设定不排弧。
  - `plots`（config 补弧后/遗留恢复；新书提交即 ready，正常不经此）→ 弧+桥段已落未 ready：引导用户在书详情页「✅ 确认弧+桥段」进 ready。
  - `ready` → 引导到 write（写作）或 publish（上架）。
- 用户没指定具体书 → 先问「对哪本书操作？」；书多时列出书名让用户挑。

## 退出状态
- 分 skill 完成 → 汇报结果 + 用 `navigate` 切到对应页面可视化（建书 → `/books/start`；弧 → `/books/generator`；写作 → `/books/<book_id>/continue`；上架 → `/publish`）。

## 失败处置
- 服务起不来 → 报出错误，建议用户手动 `launch.bat` 后重试。
- `BookBusyError`（写工具被锁）→ 稍等重试。
- 分 skill 的前置检查不满足 → 明确告知缺哪一步，引导先做前一阶段。
