"""dsh headless 驱动桥 —— 侧栏聊天后端转发到 dsh 一次性子进程。

背景：用户拍板用 dsh（DeepSeek Harness，Node agent 框架）核心替换内置 agent
（plugins/agent_loop.py 已删除），见 docs/交接文档-2026-08-20-dsh替换内置agent.md。
dsh headless profile 是 one-shot：给一个任务文本，内部反复调 MCP 工具直到完成，
打印最终回复后退出。本桥把浏览器持有的消息历史拼进任务文本，subprocess 跑 dsh，
把最终回复作为 SSE reply 事件返回。

实时进度：不重复实现逐工具 SSE —— 浏览器工具日志页签本就 3s 轮询
libraries/tool_log.py（dsh 经 MCP 的真实调用写 source=mcp 到 storage/tool_log.jsonl，
跨进程可见）。

护栏：
  - 运行期 overlay 强制注入 toolCallTimeoutMs=600000（generate_full_outline /
    write_next_bridge 阻塞数分钟，否则被 MCP 掐断，spike 已验证）。
  - 强化指令拼进任务文本前缀（persona 已在 headless profile 注入，这里按任务重申
    护栏：禁 create_book/delete_book、phase 门控、防死循环轮询）。

本模块零新增 Python 依赖（subprocess + 已有 core.json_store.read_json）。
"""
import os
import shutil
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVERLAY_PATH = os.path.join(_ROOT, "storage", "dsh_runtime.yml")

# 强化指令：拼在任务文本前的护栏/编排提醒（persona 已在 headless profile 注入，
# 这里按任务重申关键约束，防 dsh 擅调越权工具 / 死循环轮询）。
_REINFORCEMENT = """[系统约束]
你是 NovelEngine 平台的外部驱动 agent。
- 按四阶段推进（建书→大纲→写作→上架），每阶段前用 get_book_detail 校验 phase，phase 不满足不跨阶段硬做。
- 严禁调用 create_book / delete_book（web-only，不在工具面）；建书必须 drive_ui 驱动浏览器向导。
- 工具被 phase 门控拒绝或抛 BookBusyError 时调整策略或稍后重试；同一只读工具同参调用超过 3 次即为循环，应停止并如实汇报。
- 长工具（generate_full_outline / write_next_bridge）会阻塞数分钟属正常，等待结果，不要反复用同参重查。"""


def _agent_cfg(key: str, default):
    """读根 config.json agent.dsh 字段；读失败/缺失回退默认。"""
    try:
        from core.json_store import read_json
        cfg = read_json(os.path.join(_ROOT, "config.json"), {}) or {}
        return (cfg.get("agent") or {}).get("dsh", {}).get(key, default)
    except Exception:
        return default


def get_dsh_argv() -> list:
    """dsh 启动前缀（argv 列表）。

    Windows 下 npm 全局装的 dsh 是 .CMD shim——批处理会把中文参数按 ANSI 编码
    弄坏（cmd.exe 转码，实测 exit 1）。故把 .cmd 解析到真正的 Node 入口
    `lib/bin.js`，用 `node <bin.js>` 直接启动：Node 经 CreateProcessW 收 UTF-16
    参数，中文无损。非 .cmd（用户自配可执行名）原样返回。
    """
    binary = _agent_cfg("binary", "dsh")
    resolved = shutil.which(binary)
    if not resolved:
        return [binary]
    low = resolved.lower()
    if low.endswith((".cmd", ".bat")):
        pkg = os.path.join(os.path.dirname(resolved), "node_modules", "@deepseek-ai", "dsh")
        binjs = os.path.join(pkg, "lib", "bin.js")
        if os.path.exists(binjs):
            return [shutil.which("node") or "node", binjs]
    return [resolved]


def get_dsh_profile() -> str:
    """dsh profile 名（config.json agent.dsh.profile 可覆盖，默认 'headless'）。"""
    return _agent_cfg("profile", "headless")


def _write_runtime_overlay(timeout_ms: int = 600000) -> str:
    """写运行期 overlay（storage/dsh_runtime.yml）：强制长工具超时。

    dsh patch 层对 id-targeted entry 是「整体替换 config」（非深合并），故这里
    必须给全 mcp-novelengine 的 config——与已装 headless profile / 仓库模板
    agent-sidecar/cordis.patch.yml 保持一致——并把超时钉到 600000。
    双保险：即使已装 profile 旧值回归（如 180000），长工具也不被 MCP 掐断。
    任务级强化走任务文本（_REINFORCEMENT），不碰 persona。
    """
    cwd = _ROOT.replace(os.sep, "/")   # YAML 用正斜杠，与模板一致
    yaml_text = (
        "# dsh 运行期 overlay（dsh_bridge 生成）—— 强制长工具超时。\n"
        "# 注意：dsh patch 对 id-targeted entry 整体替换 config，必须给全；\n"
        "# 与 headless profile / agent-sidecar/cordis.patch.yml 保持同步。\n"
        "- id: mcp-novelengine\n"
        "  config:\n"
        "    serverName: novelengine\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['mcp_server.py']\n"
        f"    cwd: '{cwd}'\n"
        f"    toolCallTimeoutMs: {timeout_ms}\n"
    )
    os.makedirs(os.path.dirname(_OVERLAY_PATH), exist_ok=True)
    with open(_OVERLAY_PATH, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    return _OVERLAY_PATH


_MAX_TASK_CHARS = 20000   # 任务文本上限：Windows 命令行 ~32K，留余量


def _build_task_text(task: str, history: list | None) -> str:
    """浏览器持有的 user/assistant 历史 + 当前任务拼成一个 headless 任务文本。

    与内置 agent 的「messages 浏览器持有」模型同构：多轮语义靠前文回放维持。
    超长时截断中间旧历史（保头部强化指令 + 尾部最新消息），防 Windows 命令行超限。
    """
    prefix = _REINFORCEMENT.strip()
    parts = []
    for m in history or []:
        role = "用户" if m.get("role") == "user" else "助手"
        content = (m.get("content") or "").strip()
        if content:
            parts.append(f"[{role}] {content}")
    final = f"[用户] {task.strip()}"
    text = "\n\n".join([prefix] + parts + [final])
    if len(text) <= _MAX_TASK_CHARS:
        return text
    budget = _MAX_TASK_CHARS - len(prefix) - len(final) - 60
    kept_body = "\n\n".join(parts)[-budget:]
    return prefix + "\n\n...(历史过长已截断，仅保留最近内容)...\n\n" + kept_body + "\n\n" + final


def run_dsh_task(task: str, history: list | None = None,
                 timeout_s: int = 900):
    """跑一次 dsh headless 任务，产出 SSE 事件 dict。

    事件序列：tool_start（伪条目 dsh.task）→ reply（最终回复）→ done。
    失败：error + done。task 为最新用户消息，history 为浏览器持有的消息列表。

    说明：headless 是 one-shot，最终回复一次性打印（无真正的流式），
    故 reply 事件单条返回；实时工具进度靠浏览器 tool-log 3s 轮询呈现。
    """
    overlay = _write_runtime_overlay()
    cmd = get_dsh_argv() + [
        "--profile", get_dsh_profile(),
        "--patch", overlay, _build_task_text(task, history),
    ]
    yield {
        "type": "tool_start", "tool": "dsh.task",
        "args": {"profile": get_dsh_profile(), "task": (task or "")[:200]},
    }
    try:
        proc = subprocess.run(
            cmd, cwd=_ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        yield {"type": "error", "message": f"dsh 任务超时（>{timeout_s}s），请拆分任务或稍后重试"}
        yield {"type": "done"}
        return
    except FileNotFoundError:
        yield {"type": "error",
               "message": "dsh 未安装或不在 PATH：请 `npm i -g @deepseek-ai/dsh`（0.1.0-rc.x）"}
        yield {"type": "done"}
        return

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        snippet = (err or out)[-500:]
        yield {"type": "error", "message": f"dsh 任务失败（exit {proc.returncode}）：{snippet}"}
        yield {"type": "done"}
        return
    if out:
        yield {"type": "reply", "content": out}
    else:
        yield {"type": "reply", "content": err or "(dsh 无输出)"}
    yield {"type": "done"}
