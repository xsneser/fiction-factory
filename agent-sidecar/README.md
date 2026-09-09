# NovelEngine × dsh Headless 侧车

用 **DeepSeek Harness(`@deepseek-ai/dsh`,Node 侧车)** 作为现成开源 agent,经 MCP 客户端驱动 NovelEngine。
Spike 结论与 dsh 现状见 `docs/架构总览.md` §七(3 摩擦点;spike 文档已删)。

> ⚠️ **状态**:spike 已验证「桥接 + 建书向导」可行,但暴露长工具超时 / 建书保真度差 / 自主循环失控三个摩擦点(已被护栏层解决)。**侧车已是侧栏唯一大脑(`libraries/dsh_bridge.py`),事件流推送 2026-08-20 落地后实时工具卡/导航不再靠轮询。** **2026-08-28+:dsh 侧 skill 已重写为 6 个(`agent-sidecar/skills/novel-*`: build-candidates / build / story / replan / publish / scout);profiles-on 时 bridge 直接读本目录注入,镜像需 `cp` 到 `.dsh/skills/`(有契约测试 `tools/test_skill_profile_contract.py` 校验引用⊆profile 与镜像一致)。** 本目录同时是复现模板与交付物(events-runner 为生产运行文件)。

## 环境

- Node `>=22.19 || >=24`(本机 v24.19.0)。
- `npm install -g @deepseek-ai/dsh`(0.1.0-rc.x,developer preview)。
- `~/.dsh/.env` 配 `DEEPSEEK_API_KEY`。

## 配置

1. `cordis.patch.yml`(本目录)拷到 `~/.dsh/profiles/headless/cordis.patch.yml` —— 挂 `python mcp_server.py` 为 MCP 客户端 + 注入四阶段 persona。

> 运行入口说明：dsh 是主支持入口，由 `dsh_bridge` 按任务动态选择 profile。项目 `.mcp.json` 与通用外部 MCP 客户端接入仅作 Deprecated 兼容用途；多服务注册不等于单 session 只启一个 profile，legacy 只能显式启用。
2. **skill 镜像**：`agent-sidecar/skills/<name>/SKILL.md` 是源；改后 `cp` 到 `D:\NovelEngine\.dsh\skills\<name>\SKILL.md`（profiles-on 时 bridge 直读源注入，不依赖镜像；fallback/原生 skill 发现仍扫 `.dsh/skills`）。契约测试 `tools/test_skill_profile_contract.py` 会校验引用⊆profile 与镜像 byte-identical。
3. 首次 `dsh --profile headless` 自动初始化 profile。

## 运行

```bash
cd D:/NovelEngine
dsh --profile headless --dump-config        # 验证配置树含 mcp__novelengine 工具
dsh --profile headless "列出所有书"          # 只读冒烟（原 headless-runner，纯文本最终回复）
# 事件流模式（NovelEngine 定制，浏览器侧栏走的即是它）——单 patch 即可：
# storage/dsh_runtime.yml 已由 dsh_bridge 生成并内嵌 events-runner，勿再叠加
# agent-sidecar/events-runner.yml（否则 duplicate loader entry id: events-runner）。
node vendor/dsh-ne/lib/bin.js --profile headless \
  --patch storage/dsh_runtime.yml "<任务>"
#   → stdout 逐行实时 NDJSON：tool/call → tool/result → … → reply → done
```

## events-runner（事件流，NovelEngine 定制）

- **为何**：dsh headless 原 runner 用 `summarize` 丢弃全程中间事件、只打印最终文本；浏览器侧栏曾靠 2.5s/3s 轮询补实时感。
- **机制**：`vendor/dsh-ne/events-runner.mjs`（照抄 headless-runner 的 run 流程，把 summarize 换成监听 `session/event`）——每个 `tool/call` / `tool/result` 写成一行 NDJSON 推 stdout。经 `--patch` 挂载：先 `disabled: true` 掉 `headless-runner`，再 `insert` 本插件（`name` 用 `file:///` 绝对 URL，须位于 `vendor/dsh-ne/` 内以解析 `@deepseek-ai/*` 依赖）。
- **消费端**：`libraries/dsh_bridge.py` 用 `Popen` 逐行读 stdout，实时转 SSE（tool_call/tool_result/navigate/ui_command/reply/done）给侧栏；MCP 侧仍照写 `storage/tool_log.jsonl` 供「工具日志」页签轮询聚合（兼作外部 Claude Code 经 MCP 调用的总览）。
- **手动 vs 事件流**：不加事件流 patch 的 CLI 仍是原 headless-runner（纯文本最终回复）；`agent-sidecar/events-runner.yml` 是 events-runner 挂载的文档化模板，运行时由 `dsh_bridge` 内嵌进 `storage/dsh_runtime.yml`（单 patch），两侧互不干扰。

## 已知摩擦(见结论文档)

- **长工具超时**:`generate_full_outline` 阻塞数分钟,`toolCallTimeoutMs` 必须 ≥600000。
- **循环失控**:phase 未达 ready 时 agent 会反复轮询 `get_book_detail`,需护栏层熔断。
- **建书保真度**:set_field/set_tags/pick_candidate 未忠实传达任务设定,需向导状态保护。
- **护栏**:直建/直删工具不存在(45 工具),建书必须经浏览器向导 drive_ui。
- **系统工具已禁(2026-08-20)**:dsh 自带 tool-fs/tool-bash/subagent 等系统工具默认会暴露(cwd=D:/NovelEngine 无沙箱,可绕过 MCP 直操文件)。`cordis.patch.yml` 已用 `disabled: true` 批量禁掉,只留 MCP + 联网(web 三件;skill 已于 2026-08-24 删除)。改此模板须同步 `~/.dsh/profiles/headless/cordis.patch.yml`。

## 结论

侧车路线已投产为侧栏唯一大脑:三摩擦点分别被 `toolCallTimeoutMs=600000`(长工具超时)、`loop_guard.py` 语义环熔断(循环失控)、`tool_policy.py` phase 门控 + 建书 reset(建书保真度/越权)承接;事件流 runner 让工具进度/导航实时推送。历史结论(v0.4 自建 / 纯 Claude Code 外部驱动)已被用户拍板的 dsh 替换路线取代,见 `docs/架构总览.md` §七(dsh 侧车架构)。
