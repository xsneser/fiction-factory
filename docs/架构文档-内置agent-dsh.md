# 内置 Agent（dsh）架构文档

> 版本：2026-08-22 ｜ 定位：**内置 agent（侧栏大脑 = dsh）的架构参考**——讲清楚「它是怎么工作、由哪些部件组成、数据怎么流」，供扩展/调试/重构前先读。
> 事实源：代码是最终真相，冲突以代码为准。
> 相关文档：全系统架构（分层/工具注册表/护栏/管线）见 `docs/架构总览.md`；主设计见 `docs/设计文档-总览-claude.md`；上手交接（现状/验证/待办/踩坑）见 `docs/交接文档-2026-08-25-建书链路Agent修复.md` 与 `docs/交接文档-2026-08-25-小说抓取入库.md`。
> 历史：2026-08-20 用户拍板用 dsh 核心**完整替换**旧内置 agent（`plugins/agent_loop.py` 已删除，无 builtin 可切回）。本文档由原 dsh 交接文档重写而来。

---

## 一、架构一句话

**侧栏聊天 / 建书向导按钮 → `libraries/dsh_bridge.py` 起 dsh-ne headless 子进程（事件流版）→ `vendor/dsh-ne/events-runner.mjs` 把每个工具调用/结果逐事件写 NDJSON → 实时转 SSE → 前端工具卡 / 导航 / 向导命令。全服务单任务（新任务打断旧任务）。**

```
浏览器（侧栏 / 🚀 让 Agent 构建）
  → POST /api/agent/chat（或 agentSendTask → busy 打断+排队）
  → run_dsh_task：interrupt_current_task() 打断旧任务 → Popen node vendor/dsh-ne/lib/bin.js --profile headless
  → events-runner.mjs 监听 session/event，tool/call·tool/result 逐行 NDJSON
  → _map_dsh_event 转 SSE（tool_call/tool_result/navigate/ui_command/reply/error/done）
  → 前端 handleEvent：callId 配对工具卡、实时导航、dispatchCommand 驱动向导
```

**护栏链**：系统工具已禁（headless profile `disabled: true` ~30 个，只留 MCP+skills+联网）→ phase 门控 `tool_policy.py` → MCP 循环熔断 `loop_guard.py` → 建书保真（前端自动 reset + 校验）→ 全服务单任务打断。

---

## 二、进程模型与启动链

**双进程并存**（与全系统一致，见 `架构总览.md` §一）：Web 进程（Flask 58080）与 MCP 进程（stdio 子进程）各自独立 import ctx，靠 `books/` 文件 JSON + `storage/` 运行时缓存协调；**勿同时操作同一本书**（书锁保护写）。

**启动链（一次 dsh 任务）**：
1. 浏览器 `POST /api/agent/chat`（`ui/web_blueprints/agent.py`，SSE）。
2. `dsh_bridge.run_dsh_task(task, history)`：启动前 `interrupt_current_task()` 打断旧任务 → `Popen(node vendor/dsh-ne/lib/bin.js --profile headless)`（`get_dsh_argv()` 直启，不依赖全局 npm；绕开 Windows .CMD shim 中文转码坑）。
3. dsh headless 内部经 MCP 客户端（`cordis.patch.yml` 挂 `@deepseek-ai/dsh-mcp-client`）拉起 `python mcp_server.py` stdio 子进程，**只经 41 个 MCP 工具**驱动平台。
4. **headless 是 one-shot**：答完一个任务即退出；`finally` 反注册 + 收尸（防孤儿进程）。

**长工具超时**：MCP `toolCallTimeoutMs: 600000`（**务必 ≥600000**），否则 `generate_full_outline` / `write_next_bridge` 阻塞数分钟会被 MCP 掐断。

---

## 三、事件流实时通道（主通道）

- **`vendor/dsh-ne/events-runner.mjs`**：替换 headless 原 runner（原 `summarize` 只打印最终文本、丢弃中间事件）。监听 `session/event`，把每个 `tool/call` / `tool/result` 写成**一行 NDJSON** 推 stdout；收尾补 reply/done/error。挂载方式 = 运行期 overlay `--patch` 禁 `headless-runner` + insert events-runner（模板 `agent-sidecar/events-runner.yml`、`storage/dsh_runtime.yml`）。
- **`libraries/dsh_bridge.py`**：`Popen` + 双线程逐行读 stdout/stderr → `_map_dsh_event` 转 SSE 事件；`pending` 字典按 **callId** 配对 tool/result 补工具名。
- **`drive_ui` 参数拆内层（2026-08-21 修复）**：`_map_dsh_event` 对 drive_ui 取**内层 `args.get("args")`** 作为 `ui_command.args`（而非整个 `{cmd, args:{…}}`），与 nav-intent 通道一致；前端 `onnecommand` 另加防御性拆包（守卫 `'cmd' in args`）。
- **前端 `ui/static/js/agent_panel.js`**：callId 配对工具卡（`toolCards` / `TOOL_CARD_LIMIT`）+ 工具耗时 `⏱`（`addToolCard` 记 `performance.now()`）+ busy 期间暂停 nav-intent 轮询 + done 时 drain nav-intent 残留。

**双通道关系**（见 `架构总览.md` §三）：

| 通道 | 消费方 | 延迟 |
|---|---|---|
| dsh 事件流（SSE，主通道） | 浏览器侧栏实时执行 | 实时 |
| MCP 意图队列（轮询兜底） | 浏览器每 2.5s 轮询 `/api/agent/nav-intents` | ~2.5s |

外部 agent（Claude Code 等无 SSE 通道）经 `navigate` / `drive_ui` 写 `storage/nav_intent.json`（TTL 30s，取即清空）由浏览器轮询消费；dsh 会话内走 SSE 实时通道。`agent_panel.js` 在 busy 期间暂停轮询消费，防双触发。

**工具日志双源**：`log_tool_call`（`libraries/tool_log.py`）写 `storage/tool_log.jsonl`。外部 MCP 调用 `source="mcp"`（写 JSONL）；dsh 内部调用经 runtime overlay `--source dsh` 拉起 mcp_server → `source="dsh"`（**不写 JSONL**）。「工具日志」页签 filter source=mcp 只展示外部 agent 调用。

---

## 四、护栏链（纵深防御）

| 层 | 实现 | 作用 |
|---|---|---|
| 1. 系统工具禁用 | `agent-sidecar/cordis.patch.yml`（headless profile）`disabled: true` ~30 个 | dsh 默认暴露文件/shell/子代理等，cwd=项目根、无 sandbox，**可绕过 MCP 护栏直操文件**；只留 `mcp__novelengine__*`（41）+ skills（novel-*）+ 联网 |
| 2. phase 门控 | `libraries/tool_policy.py` `PHASE_GATES` | 工具要求的 `storyline.phase` 与当前不符即拒绝（纯规则零 LLM）；写作类只放行 `ready`、大纲类放行 `config/outlines/plots`、建书类仅 `config` |
| 3. MCP 循环熔断 | `libraries/loop_guard.py` | 近 5 步同 (tool, args) ≥3 次且结果摘要一致 → 熔断；连续 ≥5 次失败 → 熔断；`NOVEL_DISABLE_LOOP_GUARD=1` 逃生阀 |
| 4. 建书保真 | 前端自动 reset + 校验 | 建书只能走「启动新书」向导（直建工具不在工具面）；删书只能书库页手动 |
| 5. 单任务打断 | `dsh_bridge` 模块级 `_current_proc` | 全服务单任务，新任务打断旧任务 |

**护栏与工具面关系**：直建/直删工具**不存在**于 `TOOL_REGISTRY`（无法经任何工具面调用）——这是平台对「外部 agent 越权」的最终兜底，不是靠 LLM 自觉。

---

## 五、skill 体系与意图分发（历史记录——dsh 侧 skill 已于 2026-08-24 删除，仅 MCP 工具面，重写待后续会话；本节描述旧机制，供重写参考）

**双副本**（**必须保持一致**，部署时复制同步）：
- `agent-sidecar/skills/novel-*/SKILL.md` —— **版本正本**（入库）。
- `.dsh/skills/novel-*/SKILL.md` —— 运行时副本（gitignore，dsh 直接读盘）。

**分发是 LLM 驱动，非硬编码路由**：dsh 的 skill 插件在 pre-step 把 skill 目录（name+frontmatter description）注入会话，任务命中某 skill 描述就调用 `skill` 工具加载全文；`libraries/dsh_bridge.py` 的 `_REINFORCEMENT`（拼在每个任务文本前的系统约束）重申意图→skill 表与护栏。五个分 skill：

| skill | 职责 | 出口 |
|---|---|---|
| `novel-build-candidates` | 建书步 1：生成候选并**呈现**（`set_candidates`），停步 2 等用户挑选 | — |
| `novel-build` | 建书步 3：精简分阶段建书 + submit + `generate_full_outline` | 入库跳书详情 |
| `novel-outline` | 大纲：选材候选 → 一键/分步生成 → 落 `phase=ready` | ready |
| `novel-write` | 写作：`write_next_bridge` 逐桥段推进 | 章节/桥段写完 |
| `novel-publish` | 上架：补元数据/检查/发布/导出 | published/finished |

**意图→skill 表**（`_REINFORCEMENT` 内）：开新书/建书/写设定/构思世界观/生成候选→novel-build-candidates；已选候选/补全世界观/继续建书→novel-build；生成大纲/排故事线/续写扩写→novel-outline；**开始写/开写/写正文/写下一章**→novel-write；上架/发布/完本/导出→novel-publish；删书→无 skill（`navigate(/books)` 让用户手动删）。拿不准→`list_books` + `get_book_detail` 看 phase 再定。

**phase 门控与 skill 的协同**：skill 前置检查用 `get_book_detail` 的 `phase` + `outlines`/`plots` 非空判断；phase 不满足时 skill 引导前一阶段，不跨阶段硬做。`generate_full_outline` 完成时由生成器**自动落 `phase=ready`**（2026-08-22 根因修复），避免「大纲已完成却停在 config 被反复重跑」。

---

## 六、建书向导驱动（两 skill 拆分，2026-08-21 重构）

**步 1 = `novel-build-candidates`（候选生成呈现，停）——按钮路径极简，只 2 次调用**：
- **🚀 按钮路径**（表单已填好、向导已在步 2、书必不存在）：禁止 `list_books`/`get_build_status`/`navigate`/`reset`/`set_field`/`set_tags`/`next`/`query_profiles`（全冗余）。
  1. `world_candidates(book_id="", idea, tags)` → 候选。
  2. `drive_ui(set_candidates, {candidates:[…]})` → 渲染**可点选候选卡**（`window.__CANDIDATES__`；用户点卡 `WZ.pickCandidate(i)` 高亮）。
  3. **停止**。笔名仅当任务为「（未选，请帮我选）」才 `query_profiles` + `set_field pen`。
- **侧栏路径**（「帮我建一本 / 开一本新书」）：`navigate(/books/start)` 翻到步 1 表单 → 预填（用户已给全）→ **停止交用户** → 点「🚀 让 Agent 构建」由按钮路径接管。
- **硬规则**：绝不 `pick_candidate` / `next` / `submit`（挑选是用户动作）。

**步 2 交棒（页面自动）**：用户点「已挑选完毕」→ `WZ.next()` 以 `_task2Fired` 守卫触发第二个任务（指名 `novel-build`）→ 置 `_agentDriving` 抑制旧 `fillWorld()` 一键补全。

**步 2 = `novel-build`（步 3 精简分阶段建书，~13 次调用）**——不做候选（已选）：
① `generate_core_conflict` → `set_world(core_conflict)` → ② 选材省略（`generate_full_outline` 自动选材）→ ③ `generate_factions` → `set_world(factions)` → ④ `generate_characters` → `set_characters` → ⑤ `generate_rest_world` → `set_world(… + 基调)` → `drive_ui(submit)` → 系统建书 → `get_build_status` 拿 book_id → `get_book_detail` 校验 → `generate_full_outline` → phase ready → `navigate(/books/<id>/continue)`。

**要点**：
- `get_build_status` **仅 submit 后用**（拿 book_id）；不要在 next 后立即查（浏览器异步消费滞后 → 误判重试）。
- **双驱动防重**：task2 触发后 agent 是步 3 唯一驱动者；页面 `_agentDriving` 抑制自动一键补全。

---

## 七、全服务单任务打断模型

- `dsh_bridge.py`：模块级 `_current_proc` + `interrupt_current_task()`——Windows `taskkill /F /T` **杀整树**（node 会 spawn python mcp_server 子进程，`Popen.kill()` 只杀父进程会留孤儿）。
- `run_dsh_task`：启动前打断旧任务；`finally` 反注册 + 收尸（孤儿进程 / 客户端断连 GeneratorExit 兜底）；被打断/崩溃且无 done 事件 → `error`+`done` 收尾（前端 busy 复位）。
- `POST /api/agent/chat/cancel` 幂等端点；前端 `agentSendTask` busy 时先 cancel 再排队（`pendingTask`，done 接力）。
- 验证：`python tools/test_single_task_interrupt.py`（B 打断 A → A error+done / B 正常）。
- **边界**：打断长工具（`generate_full_outline` 阻塞数分钟）会丢未落盘中间结果——管线逐步落盘，可重跑续接。

---

## 八、组件与文件地图

| 组件 | 实现 | 职责 |
|---|---|---|
| 大脑桥 | `libraries/dsh_bridge.py` | 起停 dsh 子进程 / 事件流转 SSE / 单任务打断 / `_REINFORCEMENT` 意图分发 / 运行期 overlay |
| dsh 引擎 | `vendor/dsh-ne/`（精简核心，改名防冲突；node_modules 不入库，`npm install` 重建） | headless 一次性子进程 |
| 事件流 runner | `vendor/dsh-ne/events-runner.mjs` | tool/call·result 逐事件 NDJSON |
| 工具注册表 | `agent_tools.py` `_build_registry()` | 41 工具（schema 自动生成；护栏：无直建/直删） |
| phase 门控 | `libraries/tool_policy.py` | `PHASE_GATES` 按 phase 拒越权工具 |
| 循环熔断 | `libraries/loop_guard.py` | 同参同结果 ≥3 次 / 连续失败 ≥5 次熔断 |
| 意图队列 | `libraries/nav_intent.py` | `storage/nav_intent.json`（TTL 30s），双通道共享 |
| 建书状态 | `libraries/build_status.py` | `storage/build_status.json` 快照 |
| 侧栏面板 | `ui/static/js/agent_panel.js` | 工具卡 + 耗时 + busy 打断排队 + SSE 消费 |
| 建书向导 | `ui/templates/start_book.html` | 步 1-3 + `onnecommand` 命令桥 + `reportStatus` + `_agentDriving` |
| Web 端点 | `ui/web_blueprints/agent.py` | `/api/agent/chat`（SSE）/cancel/build-status/tool-log/nav-intents |
| 技能 | `agent-sidecar/skills/novel-*.md` ↔ `.dsh/skills/novel-*.md` | ~~五个分 skill（双副本同步）~~ 已删除（2026-08-24，仅 MCP；重写待后续会话） |

---

## 九、与外部 agent 的关系（Claude Code 等）

- 外部 agent 经 MCP（`claude mcp add … -- python mcp_server.py`）驱动，**同一工具注册表**，同样受 phase 门控 / 循环熔断 / 书锁约束。
- 外部无 SSE 通道 → `navigate` / `drive_ui` 走意图队列轮询兜底（~2.5s），实时性弱于 dsh 内部。
- 外部 agent 侧有**独立的 skill 实现**（`.claude/skills/novel-master` 统一调度 + 分 skill）；dsh 侧 skill 已于 2026-08-24 删除、仅 MCP 工具面。Claude 侧 `.claude/skills/` **未动**，仍独立演进（见 `CLAUDE.md` 发现与编排规则）。

---

## 十、架构边界（已知取舍）

1. **headless one-shot**：每任务一次性进程，无进程级会话记忆；会话记忆 v1 = 浏览器内历史。
2. **长工具阻塞**：`generate_full_outline` / `write_next_bridge` 阻塞数分钟，`toolCallTimeoutMs` 必须 ≥600000。
3. **打断丢中间结果**：单任务打断是 kill 整树，未落盘中间结果丢失（管线逐步落盘可续接）。
4. **skill 双副本漂移风险（已解除，2026-08-24）**：`.dsh/skills/` 与 `agent-sidecar/skills/` 曾须手动同步；两目录现已删除，仅剩 MCP 工具面。
5. **dsh 版本锁定**：`vendor/dsh-ne/` 依赖去 caret 锁精确快照（rc.8 子包）；升级前回归 `mcp_smoke`/`test_all`。
6. **persona 级一致**：已装 headless profile 的 persona 靠 `_REINFORCEMENT` 每任务覆盖；如需 persona 级一致用 `agent-sidecar/cordis.patch.yml` 模板重装。
