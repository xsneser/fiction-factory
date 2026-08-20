# NovelEngine × dsh Headless 侧车

用 **DeepSeek Harness(`@deepseek-ai/dsh`,Node 侧车)** 作为现成开源 agent,经 MCP 客户端驱动 NovelEngine。
Spike 结论见 `docs/agent-sidecar-spike-2026-08-20.md`。

> ⚠️ **状态**:spike 已验证「桥接 + 建书向导」可行,但暴露长工具超时 / 建书保真度差 / 自主循环失控三个摩擦点。**本目录为复现模板与交付物,非生产启用。**

## 环境

- Node `>=22.19 || >=24`(本机 v24.19.0)。
- `npm install -g @deepseek-ai/dsh`(0.1.0-rc.x,developer preview)。
- `~/.dsh/.env` 配 `DEEPSEEK_API_KEY`。

## 配置

1. `cordis.patch.yml`(本目录)拷到 `~/.dsh/profiles/headless/cordis.patch.yml` —— 挂 `python mcp_server.py` 为 MCP 客户端 + 注入四阶段 persona。
2. `skills/`(novel-build/outline/write/publish)复制到 `D:\NovelEngine\.dsh\skills\`(`dsh-skill-filesystem` 扫 `<projectRoot>/.dsh/skills`)。
3. 首次 `dsh --profile headless` 自动初始化 profile。

## 运行

```bash
cd D:/NovelEngine
dsh --profile headless --dump-config        # 验证配置树含 mcp__novelengine 工具
dsh --profile headless "列出所有书"          # 只读冒烟
dsh --profile headless "用 novel-build 流程建一本..."   # 全流程(建书需浏览器在 /books/start)
```

## 已知摩擦(见结论文档)

- **长工具超时**:`generate_full_outline` 阻塞数分钟,`toolCallTimeoutMs` 必须 ≥600000。
- **循环失控**:phase 未达 ready 时 agent 会反复轮询 `get_book_detail`,需护栏层熔断。
- **建书保真度**:set_field/set_tags/pick_candidate 未忠实传达任务设定,需向导状态保护。
- **护栏**:create_book/delete_book 不在 MCP 面(37 工具),建书必须经浏览器向导 drive_ui。

## 结论

侧车路线可用但补护栏工作量大;**更经济的是 v0.4 系统内方案(自建薄包装)** 或直接沿用 Claude Code 外部驱动(v0.3 已闭环)。
