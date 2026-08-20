"""dsh headless 驱动桥 —— 侧栏聊天后端转发到 dsh 一次性子进程（事件流版）。

背景：用户拍板用 dsh（DeepSeek Harness，Node agent 框架）核心替换内置 agent
（plugins/agent_loop.py 已删除），见 docs/交接文档-2026-08-20-dsh替换内置agent.md。
dsh headless profile 是 one-shot：给一个任务文本，内部反复调 MCP 工具直到完成。
本桥把浏览器持有的消息历史拼进任务文本，Popen 起 dsh 子进程并**实时逐行读
stdout 事件流**——vendor/dsh-ne/events-runner.mjs 把每个 tool/call、tool/result
写成一行 NDJSON（已替换 headless-runner 的 summarize 丢弃）——转成 SSE 事件推给
浏览器。浏览器侧栏实时看到工具步骤，不再靠 2.5s/3s 轮询补实时感。

实时进度：dsh 侧由 events-runner 逐事件推（工具名/参数/结果）；MCP 侧仍照写
storage/tool_log.jsonl（source=mcp）供「工具日志」页签轮询聚合（兼作外部
Claude Code 经 MCP 调用的总览）。

护栏：
  - 运行期 overlay 强制注入 toolCallTimeoutMs=600000（generate_full_outline /
    write_next_bridge 阻塞数分钟，否则被 MCP 掐断，spike 已验证），并同时挂
    events-runner（禁 headless-runner 的 summarize、换事件流输出）。
  - 强化指令拼进任务文本前缀（persona 已在 headless profile 注入，这里按任务重申
    护栏：禁 create_book/delete_book、phase 门控、防死循环轮询）。

本模块零新增 Python 依赖（subprocess + 标准库 + core.json_store.read_json）。
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVERLAY_PATH = os.path.join(_ROOT, "storage", "dsh_runtime.yml")

# 强化指令：拼在任务文本前的护栏/编排提醒（persona 已在 headless profile 注入，
# 这里按任务重申关键约束，防 dsh 擅调越权工具 / 死循环轮询）。
_REINFORCEMENT = """[系统约束]
你是 NovelEngine 平台的外部驱动 agent。
- 意图→skill：开新书/建书/写设定→novel-build；生成大纲/排故事线/续写扩写→novel-outline；写正文/写下一章→novel-write；上架/发布/完本/导出→novel-publish；删书→无 skill，navigate(/books) 让用户手动删（delete_book 不在工具面）。
- 拿不准阶段→先 list_books + get_book_detail 看目标书 phase 再定 skill；书多先问「对哪本书操作」，不跨阶段硬做。
- 按四阶段推进（建书→大纲→写作→上架），每阶段前用 get_book_detail 校验 phase，phase 不满足不跨阶段硬做。
- 严禁调用 create_book / delete_book（web-only，不在工具面）；建书必须 drive_ui 驱动浏览器向导。
- 工具被 phase 门控拒绝或抛 BookBusyError 时调整策略或稍后重试；同一只读工具同参调用超过 3 次即为循环，应停止并如实汇报。
- 长工具（generate_full_outline / write_next_bridge）会阻塞数分钟属正常，等待结果，不要反复用同参重查。"""


def _agent_cfg(key: str, default):
    """读根 config.json agent.dsh 字段（现仅 profile）；读失败/缺失回退默认。"""
    try:
        from core.json_store import read_json
        cfg = read_json(os.path.join(_ROOT, "config.json"), {}) or {}
        return (cfg.get("agent") or {}).get("dsh", {}).get(key, default)
    except Exception:
        return default


def get_dsh_argv() -> list:
    """dsh 启动前缀（argv 列表）：`node vendor/dsh-ne/lib/bin.js`。

    dsh 已 vendor 到本仓库 `vendor/dsh-ne/`（精简核心，改名 dsh-ne，见交接文档），
    用 `node vendor/dsh-ne/lib/bin.js` 直启——不依赖全局 npm 安装，且绕开 Windows
    .CMD shim 对中文参数按 ANSI 转码的坑（Node 经 CreateProcessW 收 UTF-16，中文无损）。
    """
    vendored = os.path.join(_ROOT, "vendor", "dsh-ne", "lib", "bin.js")
    return [shutil.which("node") or "node", vendored]


def get_dsh_profile() -> str:
    """dsh profile 名（config.json agent.dsh.profile 可覆盖，默认 'headless'）。"""
    return _agent_cfg("profile", "headless")


def _events_runner_url() -> str:
    """events-runner 插件的 file:/// 绝对 URL（Cordis loader 按 ESM 路径 import）。"""
    runner = os.path.join(_ROOT, "vendor", "dsh-ne", "events-runner.mjs")
    return "file:///" + runner.replace(os.sep, "/")


def _write_runtime_overlay(timeout_ms: int = 600000) -> str:
    """写运行期 overlay（storage/dsh_runtime.yml）：长工具超时 + 事件流 runner。

    dsh patch 层对 id-targeted entry 是「整体替换 config」（非深合并），故 mcp
    段必须给全 config——与已装 headless profile / 仓库模板
    agent-sidecar/cordis.patch.yml 保持一致——并把超时钉到 600000。
    事件流段：disabled 掉 headless-runner（summarize 丢弃中间事件），insert
    events-runner（vendor/dsh-ne/events-runner.mjs）逐事件推 NDJSON。
    双保险：即使已装 profile 旧值回归（如 180000），长工具也不被 MCP 掐断。
    任务级强化走任务文本（_REINFORCEMENT），不碰 persona。
    """
    cwd = _ROOT.replace(os.sep, "/")   # YAML 用正斜杠，与模板一致
    yaml_text = (
        "# dsh 运行期 overlay（dsh_bridge 生成）—— 强制长工具超时 + 事件流 runner。\n"
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
        "# 事件流：禁 headless-runner（只打印最终文本），换 events-runner 推 NDJSON。\n"
        "- id: headless-runner\n"
        "  disabled: true\n"
        "- insert:\n"
        "    - id: events-runner\n"
        f"      name: '{_events_runner_url()}'\n"
        "      inject: [headlessStartup]\n"
        "      config:\n"
        "        task: !!js ctx.headlessStartup.task\n"
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


# ─── NDJSON 事件 → SSE 事件映射 ───

def _short_name(name: str) -> str:
    """MCP 客户端工具名 `mcp__<server>__<tool>` → `<tool>`。"""
    if not name:
        return ""
    for prefix in ("mcp__novelengine__", "mcp__novel-engine__", "mcp__"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _parse_args(raw) -> dict:
    """tool/call 的 arguments（JSON 字符串或 dict）→ dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _result_error(msg: dict) -> bool:
    """tool/result 的 message 是否携带 isError（content 块内）。"""
    try:
        for block in (msg.get("content") or []):
            if isinstance(block, dict) and block.get("isError"):
                return True
    except Exception:
        pass
    return False


def _extract_tool_summary(msg: dict) -> str:
    """从 tool/result 的 message 抽一行摘要（content 可能是 string 或 text blocks）。"""
    try:
        parts = []
        for block in (msg.get("content") or []):
            if isinstance(block, dict):
                inner = block.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for b in inner:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text") or "")
        return (" ".join(p for p in parts if p)).strip()[:200]
    except Exception:
        return ""


def _map_dsh_event(evt: dict, pending: dict):
    """一行 NDJSON 事件 → SSE 事件（生成器，可产 0..N 条）。

    pending: {callId: {"name","callId"}} —— tool/call 按 callId 记、tool/result 按
    message.source.callId 取，用来给 result 补工具名（result 自身不带 name）。
    用 callId 作键（而非 {turn}.{step}）：dsh 一个 assistant 消息可带多个 tool_calls
    （并行），同 turn/step 会碰撞覆盖，callId 天然去重。
    navigate / drive_ui 的 tool/call 除了转成 navigate / ui_command 推送（浏览器
    执行跳转/向导命令），**同时**发 tool_call 建卡——否则它们的 tool/result 在前端
    找不到卡，会 fallback 污染上一张卡（曾把 drive_ui 错误贴到 world_candidates 卡）。
    """
    t = evt.get("type")
    data = evt.get("data") or {}
    if t == "tool/call":
        name = _short_name(data.get("name", ""))
        call_id = data.get("callId", "")
        args = _parse_args(data.get("arguments"))
        pending[call_id] = {"name": name, "callId": call_id}
        yield {"type": "tool_call", "name": name, "args": args, "callId": call_id}
        if name == "navigate":
            url = args.get("url") if isinstance(args, dict) else ""
            if url:
                yield {"type": "navigate", "url": url}
        elif name == "drive_ui":
            yield {"type": "ui_command",
                   "cmd": args.get("cmd") if isinstance(args, dict) else "",
                   "args": args if isinstance(args, dict) else {}}
    elif t == "tool/result":
        msg = data.get("message") or {}
        source = msg.get("source") or {}
        call_id = source.get("callId") or ""
        p = pending.pop(call_id, {}) or {}
        ok = not data.get("error") and not _result_error(msg)
        yield {"type": "tool_result",
               "name": p.get("name") or "",
               "callId": call_id or p.get("callId") or "",
               "ok": ok,
               "summary": _extract_tool_summary(msg)}
    elif t == "reply":
        yield {"type": "reply", "content": data.get("text") or ""}
    elif t == "error":
        yield {"type": "error", "message": data.get("message") or "dsh 任务出错"}
    elif t == "done":
        yield {"type": "done"}


def run_dsh_task(task: str, history: list | None = None,
                 timeout_s: int = 900):
    """跑一次 dsh headless 任务，实时产出 SSE 事件 dict。

    事件序列（由 events-runner 的 NDJSON 流实时驱动）：tool_call / tool_result /
    navigate / ui_command …… → reply（最终回复）→ done。
    失败/异常：error + done。task 为最新用户消息，history 为浏览器持有的消息列表。

    说明：Popen 起子进程，读线程逐行读 stdout（事件流每行一条 NDJSON），每行实时
    转 SSE yield；超时 kill、文件缺失、非零退出各有兜底。stderr 由独立线程读走
    （防管道缓冲堵死），仅失败时用于报错摘要。实时工具进度本身由事件流呈现，
    工具日志页签仍由浏览器 3s 轮询 tool-log 聚合（source=mcp）。
    """
    overlay = _write_runtime_overlay()
    cmd = get_dsh_argv() + [
        "--profile", get_dsh_profile(),
        "--patch", overlay, _build_task_text(task, history),
    ]
    try:
        proc = subprocess.Popen(
            cmd, cwd=_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        yield {"type": "error",
               "message": "vendor/dsh-ne 或 node 缺失：请确认 `vendor/dsh-ne/node_modules` 已 `npm install`"
                          "（源码入库，依赖重建），且 Node 在 PATH"}
        yield {"type": "done"}
        return

    q = queue.Queue()
    stderr_buf = []

    def _reader():
        try:
            for line in proc.stdout:
                q.put(("line", line))
            q.put(("eof", None))
        except Exception as e:  # pragma: no cover
            q.put(("read_error", e))

    def _stderr_reader():
        try:
            for chunk in proc.stderr:
                stderr_buf.append(chunk)
        except Exception:  # pragma: no cover
            pass

    threading.Thread(target=_reader, daemon=True).start()
    threading.Thread(target=_stderr_reader, daemon=True).start()

    pending = {}
    started = time.time()
    saw_any = False
    while True:
        try:
            kind, payload = q.get(timeout=0.5)
        except queue.Empty:
            # 子进程退出后 stdout 关闭，reader 必发 eof；此处只做超时兜底
            if time.time() - started > timeout_s:
                proc.kill()
                yield {"type": "error",
                       "message": f"dsh 任务超时（>{timeout_s}s），请拆分任务或稍后重试"}
                yield {"type": "done"}
                return
            continue
        if kind == "line":
            line = payload.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            saw_any = True
            for sse in _map_dsh_event(evt, pending):
                yield sse
        elif kind == "eof":
            break
        else:
            break

    exit_code = proc.wait()
    if exit_code != 0 and not saw_any:
        # 进程在产出任何事件前就崩溃（如插件加载失败）：stderr 兜底报错
        err = "".join(stderr_buf)[-500:].strip() or "(dsh 无输出)"
        yield {"type": "error", "message": f"dsh 任务失败（exit {exit_code}）：{err}"}
        yield {"type": "done"}
