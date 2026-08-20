# dsh Headless 侧车 Spike 结论(2026-08-20)

> 目的：回答「能否直接借用开源 agent(deepseek-harness / openclaw)的核心代码,不重写自己,直接驱动 NovelEngine?」
> 路线：**dsh headless 侧车**（`@deepseek-ai/dsh` 0.1.0-rc.7,Node 侧车,MCP 客户端挂 NovelEngine）。护栏维持:create_book/delete_book 不进 MCP 面,建书走浏览器向导。
> 状态：**已停止验证**,记录结论收尾。测试书 book_002 待用户在书库页手动删除。

## 结论(一句话)

**「现成开源 agent 能驱动 NovelEngine」= 能**,但**摩擦不少**:桥接与建书向导打通,长耗时工具(MCP 超时)会掐断、建书保真度差、agent 可能陷入检查死循环。**侧车路线可用但需大量补护栏**,系统内 v0.4 方案的价值被印证。

## 验证结果

| 验证项 | 结果 | 说明 |
|---|---|---|
| dsh 安装 / headless profile | ✅ | Node v24.19.0;`npm i -g @deepseek-ai/dsh`(0.1.0-rc.7);headless profile 内置 `deepseek-official / deepseek-v4-flash` 默认模型,与项目纪律一致 |
| MCP 桥接 | ✅ | `@deepseek-ai/dsh-mcp-client` 挂 `python mcp_server.py`(stdio),37 工具变成 `mcp__novelengine__*` 原生工具;`dsh --profile headless` 只读任务(list_books/get_book_detail)推理准确,自己判断出「phase=config 应进大纲阶段」 |
| 建书向导(drive_ui) | ✅ | dsh 跑完整序列 navigate→set_field→set_tags→pick_candidate→next→set_characters→submit,**真建出书**(book_002,phase=config,世界观 12 维 + 6 角色落库) |
| 大纲生成 | ⚠️ 失败 | dsh 经 MCP 调 `generate_full_outline` 返回「已执行」但 **0 大纲产出**,随后反复轮询 `get_book_detail` 等 phase=ready 陷入死循环;进程内直调同一工具却能正常产出大纲(2 outlines)。疑似 **MCP 3 分钟 toolCallTimeout 掐断长调用** 或 dsh 擅自 `save_basic_info` 破坏状态 |
| 写作阶段 | 未验证 | 停止前未跑 |

## 摩擦点(3 个)

1. **长耗时工具超时**:`generate_full_outline`(阻塞数分钟)超出 dsh `toolCallTimeoutMs=180000`(3 分钟),MCP 客户端掐断,agent 拿到残缺结果后死循环轮询。需把该工具超时调到 10 分钟+,或护栏层加循环熔断。
2. **建书保真度差**:任务要求「都市赘婿爽文」,建出《最后一次重生》(玄幻/重生),`tags` 一度为空。dsh 的 set_field/set_tags/pick_candidate 未忠实传达设定(推测:向导残留 sessionStorage 状态干扰 / dsh 自行改写设定 / set_tags 与向导 toggleTag 双写契约不完全对齐)。
3. **自主循环失控**:agent 在 phase 未达 ready 时反复调 `get_book_detail` 轮询,无语义环检测即无限跑(后台任务需手动 kill)。OpenClaw/dsh 自带的循环护栏在 headless one-shot 下未充分生效。

另发现:dsh 会在任务中途**擅自调用任务未要求的工具**(如 `save_basic_info`),可能破坏书状态——外部 agent 的自由度需要更强的工具白名单/护栏约束。

## 技术要点(复现侧车需要)

- **补丁语法**(dsh `cordis.patch.yml` 只支持 id-targeted override,新增条目用 `insert` 不带 id):见 `agent-sidecar/cordis.patch.yml`。
- **skill 挂载**:`dsh-skill-filesystem` 默认扫 `<projectRoot>/.dsh/skills`(cwd 的项目根);本项目 `agent-sidecar/skills/` 复制到 `.dsh/skills/`(`.dsh/` 已入 gitignore)。4 个 SKILL.md(novel-build/outline/write/publish)已改写为 dsh 侧版。
- **特化 persona**:patch `system-prompt.persona` 注入四阶段编排(建书走向导、不调 create_book/delete_book、phase 校验、BookBusyError 重试)。
- **自动加载**:dsh headless 从 cwd 读 `CLAUDE.md`(`dsh-agent-instructions` 支持 CLAUDE.md),NovelEngine 根目录的 CLAUDE.md 自动成为 agent 指令。
- **凭据**:`~/.dsh/.env` 已有 `DEEPSEEK_API_KEY`。

## 与 v0.4 的关系与建议

- v0.4(系统内自主 agent)保持「设计未实现」,其价值(长工具不超时、护栏内建、create_book 双轨、无 Node 依赖)被本 spike 摩擦点印证。
- **建议**:
  - 若坚持侧车路线,需先补:长工具 MCP 超时调大、护栏层循环熔断(如轮询 >N 次报错)、工具白名单收紧(禁 dsh 擅调 save_basic_info 等)、建书保真度(向导状态保护/重置)。**工作量大,不亚于自建薄包装。**
  - 更经济的路径:**回 v0.4 系统内方案**——自己写薄包装(复用现有 agent_loop + 40 工具),或退一步直接沿用 Claude Code 外部驱动(v0.3 已闭环,零新增)。
  - 混合形态(侧车交互 + v0.4 批量)留待后续立项,需先议建书护栏。

## 清理

- `book_002`(测试书,半成品大纲)待用户在书库页手动删除(护栏:agent 不代删)。
- dsh 侧配置在 `~/.dsh/profiles/headless/`(不入库);仓库侧仅 `agent-sidecar/`(skill 模板 + 配置模板 + README)入库。
