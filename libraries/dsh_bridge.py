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
  - 运行期 overlay 强制注入 toolCallTimeoutMs=600000（save_outlines / save_chapter_text
    等薄工具与 agent 长生成阻塞数分钟，否则被 MCP 掐断，spike 已验证），并同时挂
    events-runner（禁 headless-runner 的 summarize、换事件流输出）。
  - 规则（四阶段/路由/护栏）唯一来源 NOVEL_AGENT.md，经 agent-instructions 注入为
    workspace 指令（system-reminder）；任务文本只拼历史回放 + 当前任务，不再内联前缀。

本模块零新增 Python 依赖（subprocess + 标准库 + core.json_store.read_json）。
"""
import contextlib
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

_log = logging.getLogger("novel-engine")

from libraries.token_proxy import ensure_proxy   # 拉起本地 token 检测代理（dsh 走它计 token）

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVERLAY_PATH = os.path.join(_ROOT, "storage", "dsh_runtime.yml")
_DEBUG_PROMPT_DIR = os.path.join(_ROOT, "storage", "debug-prompts")   # 调试卡完整 prompt 文件（每任务清空）

# ─── 全服务单任务：当前 dsh 子进程 + 打断（kill 整树）───
_current_proc = None
_current_task_meta = None   # 当前运行任务元数据（含 flow/child identity，前端切页恢复感知用）
_current_proc_lock = threading.Lock()
_last_interrupted_pid = None


def _set_current_proc(proc, task: str = "", flow_id: str = "", child_run_id: str = ""):
    global _current_proc, _current_task_meta
    with _current_proc_lock:
        _current_proc = proc
        _current_task_meta = {
            "started_at": time.time(),
            "task": (task or "").strip()[:80],
            "flow_id": flow_id or "",
            "child_run_id": child_run_id or "",
        }


def _clear_current_proc(proc):
    global _current_proc, _current_task_meta
    with _current_proc_lock:
        if _current_proc is proc:
            _current_proc = None
            _current_task_meta = None


def get_current_task_status() -> dict:
    """当前是否有 dsh 任务在跑（前端切页/刷新后恢复感知用）。

    断连后任务自行跑完但没人收尸（proc 已退出、槽位未清）→ 惰性清槽返回
    running=False；运行中返回 running=True（含 pid/started_at/task 供展示）。
    """
    global _current_proc, _current_task_meta
    with _current_proc_lock:
        proc = _current_proc
        if proc is None:
            return {"running": False}
        if proc.poll() is None:
            meta = _current_task_meta or {}
            return {
                "running": True,
                "pid": proc.pid,
                "started_at": meta.get("started_at"),
                "task": meta.get("task", ""),
                "flow_id": meta.get("flow_id", ""),
                "child_run_id": meta.get("child_run_id", ""),
            }
        _current_proc = None
        _current_task_meta = None
        return {"running": False}


def interrupt_current_task() -> bool:
    """打断当前正在跑的 dsh 子进程（kill 整树）。

    dsh 的 node 进程会 spawn `python mcp_server.py` 子进程，`Popen.kill()` 只杀父进程
    会让 python 变孤儿（还可能持书锁），故 Windows 用 `taskkill /F /T` 杀整树。
    返回是否真打断了（无任务/已退出返回 False，幂等）。被打断的任务会在其
    run_dsh_task 里以 error+done 收尾，前端 busy 复位。
    """
    global _current_proc, _current_task_meta, _last_interrupted_pid
    with _current_proc_lock:
        proc = _current_proc
        _current_proc = None
        _current_task_meta = None
        if proc is None or proc.poll() is not None:
            return False
        _last_interrupted_pid = proc.pid
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return True


# ─── dsh 工具事件持久化：供前端刷新后重建工具卡流 ───
# 侧栏 dsh 的 MCP 调用（source=dsh，mcp_server 以 --source dsh 拉起）只写在临时 mcp_server
# 进程内存缓冲，进程退出即丢、不进 Web 可查的 tool_log；故在桥层把 tool_call/tool_result
# 事件落盘 task_events.jsonl，前端刷新后按 started_at 拉取重建「刷新前的工具卡流」。
_TASK_EVENTS_PATH = os.path.join(_ROOT, "storage", "task_events.jsonl")
_MAX_TASK_EVENTS_BYTES = 256 * 1024
_MAX_TASK_EVENTS_LINES = 1000
_KEEP_TASK_EVENTS_LINES = 500


def _append_task_event(evt: dict) -> None:
    """把 dsh 工具事件追加到 task_events.jsonl（尽力而为，异常静默降级）。

    超长自动截断：字节阈值快筛 + 行数上限，防磁盘无限增长（仿 tool_log）。
    """
    try:
        rec = dict(evt or {})
        rec["ts"] = time.time()
        os.makedirs(os.path.dirname(_TASK_EVENTS_PATH) or ".", exist_ok=True)
        with open(_TASK_EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            if os.path.getsize(_TASK_EVENTS_PATH) > _MAX_TASK_EVENTS_BYTES:
                with open(_TASK_EVENTS_PATH, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                if len(all_lines) > _MAX_TASK_EVENTS_LINES:
                    with open(_TASK_EVENTS_PATH, "w", encoding="utf-8") as f:
                        f.writelines(all_lines[-_KEEP_TASK_EVENTS_LINES:])
        except Exception:
            pass
    except Exception:
        pass


def get_task_events(since: float = 0.0, limit: int = 300) -> list:
    """读取 task_events.jsonl 中 ts >= since 的事件（升序，供刷新重建工具卡流）。"""
    try:
        if not os.path.exists(_TASK_EVENTS_PATH):
            return []
        with open(_TASK_EVENTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        items = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts", 0) >= since:
                items.append(rec)
        items.sort(key=lambda e: e.get("ts", 0))
        return items
    except Exception:
        return []


def clear_task_events() -> None:
    """清空 dsh 工具事件存储（前端「清空对话」时调用，防清空后旧工具卡回显）。"""
    try:
        if os.path.exists(_TASK_EVENTS_PATH):
            with open(_TASK_EVENTS_PATH, "w", encoding="utf-8") as f:
                f.write("")
    except Exception:
        pass


# 精简 persona：与已装 headless profile / agent-sidecar/cordis.patch.yml 的 system-prompt persona 保持一致。
# 运行时 overlay 会整体替换 system-prompt config（patch 非深合并，必须给全），故 persona 在此内联。
# 只做角色 + 指向 NOVEL_AGENT.md（唯一业务规则源）；不再内联四阶段细节。原 _REINFORCEMENT（任务前缀
# 中文强化块）已删——规则全在 NOVEL_AGENT.md，任务文本不再拼前缀。
# 注意：必须是普通字符串（非 f-string），保留字面 {{model}}/{{cwd}} 供 dsh 后续插值。
_PERSONA = """You are a coding agent powered by the {{model}} model. Your working directory is {{cwd}}.
You drive the NovelEngine novel-creation platform through its MCP tools (mcp__novelengine__*).
Follow the workflow, routing, and guardrails in NOVEL_AGENT.md (your workspace instructions)
— it is the single source of truth."""


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


_PY_WITH_MCP = None  # 进程内缓存：可 import mcp 的解释器（mcp_server 子进程用它拉起）


def _python_with_mcp():
    """挑一个能 `import mcp` 的 python 解释器，供 overlay 里 mcp_server 子进程使用。

    背景(2026-09-06 实测)：本机存在双运行时——全局 Python310 装了 mcp，而 Doubao
    沙箱 python 没装。overlay 若写死 `python`(靠 PATH)或只写 `sys.executable`，一旦
    服务器跑在无 mcp 的解释器上，mcp_server import 失败 → 工具服务器起不来 → dsh 任务
    零工具 → 模型猜 mcp__novelengine__* 名字 → 满屏 unknown tool。
    这里优先 sys.executable(与服务器同环境)，不行再回退到已装 mcp 的 Python310 / PATH
    python；都不可用就仍用 sys.executable(至少与服务器一致，错误也一致可诊断)。
    """
    global _PY_WITH_MCP
    if _PY_WITH_MCP:
        return _PY_WITH_MCP

    def _ok(py):
        if not py:
            return False
        try:
            r = subprocess.run([py, "-c", "import mcp"], capture_output=True,
                               timeout=20)
            return r.returncode == 0
        except Exception:
            return False

    for cand in (sys.executable,
                 r"C:\Users\lenovo\AppData\Local\Programs\Python\Python310\python.exe",
                 shutil.which("python")):
        if _ok(cand):
            _PY_WITH_MCP = cand
            break
    if not _PY_WITH_MCP:
        _PY_WITH_MCP = sys.executable
    return _PY_WITH_MCP


def _write_runtime_overlay(timeout_ms: int = 600000, mcp_profile: str = "") -> str:
    """写运行期 overlay（storage/dsh_runtime.yml）：长工具超时 + 事件流 runner。

    dsh patch 层对 id-targeted entry 是「整体替换 config」（非深合并），故 mcp
    段必须给全 config——与已装 headless profile / 仓库模板
    agent-sidecar/cordis.patch.yml 保持一致——并把超时钉到 600000。
    事件流段：disabled 掉 headless-runner（summarize 丢弃中间事件），insert
    events-runner（vendor/dsh-ne/events-runner.mjs）逐事件推 NDJSON。
    双保险：即使已装 profile 旧值回归（如 180000），长工具也不被 MCP 掐断。
    persona 在此内联（system-prompt 整体替换必须给全）；规则源 NOVEL_AGENT.md 由
    agent-instructions 注入 workspace 指令。
    """
    cwd = _ROOT.replace(os.sep, "/")   # YAML 用正斜杠，与模板一致
    # persona 每行缩进 6 空格（YAML `>-` 折叠标量的块缩进），经 {persona_block} 值替换插入 f-string——
    # 值内 {{model}}/{{cwd}} 不会被 f-string 二次解析，保持字面供 dsh 插值。
    is_writer = mcp_profile == "write"
    writer_persona = """You are a Plot Writer. Use only the two provided NovelEngine MCP tools.
Prepare exactly one Plot, write it, save it once, and stop. The server owns all routing and planning."""
    persona_block = "\n".join("      " + ln for ln in (writer_persona if is_writer else _PERSONA).strip().splitlines())
    instruction_candidates = "[]" if is_writer else "['NOVEL_AGENT.md']"
    instruction_budget = "0" if is_writer else "20000"
    yaml_text = (
        "# dsh 运行期 overlay（dsh_bridge 生成）—— 强制长工具超时 + 事件流 runner。\n"
        "# 注意：dsh patch 对 id-targeted entry 整体替换 config，必须给全；\n"
        "# 与 headless profile / agent-sidecar/cordis.patch.yml 保持同步。\n"
        "- id: mcp-novelengine\n"
        "  config:\n"
        "    serverName: novelengine\n"
        "    transport: stdio\n"
        # 钉到「能 import mcp」的解释器(_python_with_mcp):若裸 `python` 从 PATH 或服务
        # 进程解释器未装 mcp,mcp_server.py import 失败 → 工具服务器起不来 → dsh 任务
        # 零工具,模型只能猜 mcp__novelengine__* 名字 → 满屏 unknown tool(2026-09-06 实测)。
        f"    command: '{_python_with_mcp()}'\n"
        "    # --source dsh：mcp_server 据此把工具日志 source 记为 dsh（不写 JSONL），\n"
        "    # 右侧「工具日志」页签只展示外部 agent（source=mcp）调用，内部 dsh 不混入\n"
        "    args: ['mcp_server.py', '--source', 'dsh'" +
        (f", '--profile', '{mcp_profile}'" if mcp_profile else "") + "]\n"
        f"    cwd: '{cwd}'\n"
        f"    toolCallTimeoutMs: {timeout_ms}\n"
        "# Writer 不接收全局 NOVEL_AGENT；非 Writer 保持原开发/路由指令。\n"
        "- id: agent-instructions\n"
        "  config:\n"
        f"    maxBytes: {instruction_budget}\n"
        f"    instructionFileCandidates: {instruction_candidates}\n"
        "    localInstructionFileCandidates: []\n"
        "# 系统提示词：persona + 关运行时快照（沙箱/审批对纯 MCP 小说 agent 无意义，对应工具已禁）\n"
        "- id: system-prompt\n"
        "  config:\n"
        "    includeHarnessIdentity: false\n"
        "    includeRuntimeContext: false\n"
        f"    persona: >-\n{persona_block}\n"
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
    if not os.environ.get("NE_KEEP_TOOL_PRUNE"):
        # 关 tool 结果裁剪(2026-09-06):get_pen_style 全量注入 md+四条样文也不被裁中段。
        # 逃生口:NE_KEEP_TOOL_PRUNE=1 时不注入该条(保留 dsh-base 默认 8192 阈值裁剪)。
        yaml_text += (
            "- id: tool-result-pruner\n"
            "  disabled: true\n"
        )
    if is_writer:
        # Writer 不可联网、不可读取 skill catalog 或进入计划模式。
        for plugin in ("tool-web", "web", "web-search-deepseek", "tool-skill", "skill", "skill-filesystem",
                       "tool-plan", "plan-mode"):
            yaml_text += f"- id: {plugin}\n  disabled: true\n"
    os.makedirs(os.path.dirname(_OVERLAY_PATH), exist_ok=True)
    with open(_OVERLAY_PATH, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    return _OVERLAY_PATH


_MAX_TASK_CHARS = 20000   # 任务文本上限：Windows 命令行 ~32K，留余量


def _build_task_text(task: str, history: list | None) -> str:
    """浏览器持有的 user/assistant 历史 + 当前任务拼成一个 headless 任务文本。

    与内置 agent 的「messages 浏览器持有」模型同构：多轮语义靠前文回放维持。
    规则（四阶段/路由/护栏）由 agent-instructions 注入 NOVEL_AGENT.md，任务文本不再拼前缀。
    超长时截断中间旧历史（保头部 + 尾部最新消息），防 Windows 命令行超限。
    """
    parts = []
    for m in history or []:
        role = "用户" if m.get("role") == "user" else "助手"
        content = (m.get("content") or "").strip()
        if content:
            parts.append(f"[{role}] {content}")
    final = f"[用户] {task.strip()}"
    body = parts + [final]
    text = "\n\n".join(body)
    if len(text) <= _MAX_TASK_CHARS:
        return text
    budget = _MAX_TASK_CHARS - len(final) - 60
    kept_body = "\n\n".join(parts)[-budget:]
    return "...(历史过长已截断，仅保留最近内容)...\n\n" + kept_body + "\n\n" + final


# 未分类哨兵：任务既不是明确的阶段动作、也不是只读问句。
#
# 调用方约定（**别把空值写进 profile**）：`run_dsh_flow` 收到哨兵时产出显式报错 + 阶段
# 指引，不 spawn；`run_dsh_task` 内部另行收敛为只读面——因为 MCP 侧 `filter_registry("")`
# 会 fail-open 到**全量 45 工具**（agent_tool_router 的 `if not profile: return registry`），
# 空 profile 是比 inspect 更危险的回落，绝不能让空值流到 `--profile`。
UNROUTABLE_PROFILE = ""


def _task_tool_profile(task: str) -> str:
    """按用户任务选择最小工具面；未分类返回 UNROUTABLE_PROFILE（不再静默回落只读面）。

    判定顺序有意固定，改前先读这几条：
      - 续规划优先：`延伸故事线`/`扩弧` 属 replan，不是 build；
      - 明确写作动作优先于提示中附带的“风格规则”/“情节段”，否则“继续写…全部情节段”
        会被判成 style/build；
      - “完本”不能用裸子串匹配，否则“写完本章”会被误判为 publish；
      - 建书有两段（步 1-2 候选 / 步 3 内容构建），**文本上不可分**：步 1 的自动任务原文
        同时含「候选」和「世界观」。这里只保证“别漏、别误伤”，真正的阶段判定交给服务端
        ——`run_dsh_flow` 把 build/build-candidates 一并交给 `_build_fsm`，由它读
        build_status.cur 决定起哪个 profile。所以下面只想清“强标记”，不追求文本互斥。
      - 只读问句是**正面命中**（保留「问一句书的状态」这类正当用法），排在所有动作之后；
        全都命不中才返回哨兵，由调用方显式报错——这正是「大纲生成失败」被静默当成只读
        提问、于是只能读不能写的根因。
    """
    text = (task or "").lower()
    if any(k in text for k in ("重规划", "续规划", "规划边界", "扩弧", "延伸故事线", "下一段弧",
                               "再排一段", "自动续规划", "往下想", "replan")):
        return "replan"
    if any(k in text for k in ("写正文", "写下一章", "写第", "写情节段", "一键写", "完整章",
                               "继续写", "接着写", "再写", "续写", "往下写", "继续正文")):
        return "write"
    publish_markers = ("发布", "上架", "导出", "书名简介", "检查能否发书",
                       "标记完本", "设为完本", "全书完本", "作品完本", "完本状态")
    if text.strip() == "完本" or any(k in text for k in publish_markers):
        return "publish"
    if any(k in text for k in ("侦察", "抓取", "热榜", "提取", "下载小说", "书库扫描")):
        return "scout"
    if any(k in text for k in ("样文", "风格规则", "禁词", "替换词", "风格匹配", "仿写", "笔风")):
        return "style"
    # 步 3 的强标记**必须排在「候选」之前**：步 2→3 的交接原文带着「已选定候选」，
    # 若让「候选」先命中，步 3 就会被判成步 1-2 的 profile——那个工具面没有
    # set_outline/set_world/set_characters（UI_COMMAND_POLICY 拒收），故事线无从写入。
    if any(k in text for k in ("步 3", "步3", "故事线", "大纲", "情节段", "内容构建",
                               "生成弧", "排弧", "补弧", "完整弧")):
        return "build"
    if any(k in text for k in ("候选", "开新书", "启动新书", "新书想法")):
        return "build-candidates"
    if any(k in text for k in ("建书", "世界观", "补全设定")):
        return "build"
    # 只有前面的明确领域意图都未命中时，泛化“继续”才默认解释为继续写作。
    if "继续" in text:
        return "write"
    # 只读问句：必须排在所有动作分支之后，否则“继续写，顺便看下状态”会被抢走。
    if any(k in text for k in ("查看", "看看", "看一下", "什么情况", "是什么", "讲什么",
                               "写到哪", "哪了", "状态", "进度", "怎么样", "为什么", "为何",
                               "有没有", "是否", "列出", "多少")):
        return "inspect"
    return UNROUTABLE_PROFILE


def _resolve_run_profile(task: str, explicit: str = "") -> str:
    """定本次 run 的 MCP profile：显式声明优先，任务文本只作兜底。

    **返回值在 profiles 开启时永不为空**：MCP 侧 `filter_registry("")` 会 fail-open 到
    全量 45 工具，所以「未分类」必须在这里收敛到只读面（inspect），而不是放空值下去。
    面向用户的「未分类」显式报错在 `run_dsh_flow` 那一层做（它拦在 spawn 之前）；
    本函数只保证底层原语**按构造成立地安全**，任何调用方都不可能靠放空值拿到全量工具。
    """
    if not _profiles_enabled():
        return ""
    return (explicit or "").strip() or _task_tool_profile(task) or "inspect"


def _profiles_enabled() -> bool:
    """MCP profile 最小工具面是否启用（默认开）。"""
    v = (os.environ.get("AGENT_TOOL_PROFILES") or "1").strip().lower()
    return v not in {"0", "false", "no", "off"}


def _unscoped_tools_allowed() -> bool:
    """Legacy 全量工具面必须再有一个显式 unsafe 开关。"""
    v = (os.environ.get("ALLOW_UNSCOPED_AGENT_TOOLS") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _skill_text_for_profile(profile: str) -> str:
    skill_name = {"build-candidates": "novel-build-candidates", "build": "novel-build",
                  "write": "novel-story", "replan": "novel-replan", "publish": "novel-publish",
                  "scout": "novel-scout"}.get(profile)
    if not skill_name:
        return ""
    path = os.path.join(_ROOT, "agent-sidecar", "skills", skill_name, "SKILL.md")
    if not os.path.exists(path):
        raise RuntimeError(f"skill_not_found: {skill_name}；为安全起见禁止退化为裸 MCP")
    with open(path, encoding="utf-8") as f:
        return f.read()


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


# ─── 工具结果中文转义（前端工具卡直接展示，去英文 key）───
_CMD_ZH = {
    "set_world": "写入世界观", "set_characters": "写入角色", "set_candidates": "填入候选",
    "pick_candidate": "选中候选", "set_field": "填写字段", "set_tags": "设置标签",
    "next": "下一步", "prev": "上一步", "reset": "重置向导", "submit": "提交建书",
    "skip_candidates": "跳过候选", "load_candidates": "加载候选", "fill_world": "重新补全",
    "set_picks": "记录选材", "set_replan_preview": "暂存续规划预览",
}
_KEY_ZH = {
    "core_conflict": "核心矛盾", "genre": "题材", "sub_genre": "题材细分", "factions": "势力",
    "faction": "势力", "name": "名称", "stance": "立场", "desc": "描述", "characters": "人物",
    "protagonist": "主角", "supporting_cast": "配角", "identity": "身份", "personality": "性格",
    "golden_finger": "金手指", "catchphrase": "口癖", "role": "角色", "importance": "重要度",
    "relation": "关系", "brief": "简介", "title": "标题", "idea": "一句话设定", "tags": "标签",
    "pen_name": "笔名", "candidates": "候选", "one_liner": "一句话梗概", "world_brief": "世界观简述",
    "templates": "模板", "era": "时代", "power_system": "力量体系", "geography": "地理",
    "culture": "文化", "history": "历史", "social_structure": "社会结构", "rules": "规则",
    "world_summary": "设定概述", "tone": "基调", "target_audience": "目标读者", "pov": "视角",
    "era_language": "时代语言", "description": "描述", "status": "状态", "ok": "成功",
    "error": "错误", "book_id": "书 ID", "phase": "阶段", "world_building": "世界观",
    "cmd": "命令", "__ui_command__": "命令", "book": "书", "chapter": "章节", "age": "年龄",
    "death_year": "去世年份", "gender": "性别", "mode": "模式", "category": "分类",
    "keyword": "关键词", "plot": "情节段", "plots": "情节段", "structure": "情节弧", "structures": "情节弧库",
    "gag": "梗", "gags": "梗", "count": "数量", "total": "总计", "storyline": "时间线",
    "outlines": "弧", "timeline": "时间线", "archetype_id": "原型", "source": "来源",
    "id": "ID", "tweak": "微调", "pen": "笔名", "outline": "弧",
    "url": "地址", "words": "字数", "word_count": "字数", "target_words": "目标字数",
    "passed": "通过", "score": "评分", "message": "消息", "recent_n": "最近章数",
    "chapter_num": "章节号", "max_outlines": "弧数", "struct": "结构",
    "validate_storyline": "校验故事线", "coverage": "弧覆盖", "leaf_arcs": "情节段叶弧",
    "gaps": "叙事空白", "violations": "叶弧违例", "leading_gap": "开头空白",
    "top_arc_count": "顶层弧数", "leaf_arc_count": "叶弧数", "plot_count": "情节段数",
    "issue_count": "问题数", "suggestions": "建议", "gap_words": "空白字数",
    "validate_world": "校验世界观", "arc_fill": "弧内填充", "orphan_characters": "孤儿人物",
    "duplicates": "重复势力", "without_characters": "无人物势力",
}


def _zh_keys(v):
    """把 JSON 的键递归译成中文（值保留）。"""
    if isinstance(v, list):
        return [_zh_keys(x) for x in v]
    if isinstance(v, dict):
        return {_KEY_ZH.get(k, k): _zh_keys(val) for k, val in v.items()}
    return v


def _extract_result_text(msg) -> str:
    """从 tool/result 的 message 抽完整文本（content 可能是 string 或 text blocks）。"""
    text = ""
    try:
        for block in (msg.get("content") or []):
            if isinstance(block, dict):
                inner = block.get("content")
                if isinstance(inner, str):
                    text += inner
                elif isinstance(inner, list):
                    for b in inner:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text += b.get("text") or ""
    except Exception:
        text = ""
    return text


def _zh_tool_summary(name, args, msg):
    """把 tool/result 的消息转成中文一行摘要（前端直接展示，不带英文 key）。"""
    text = _extract_result_text(msg)
    if name == "drive_ui":
        cmd = (args or {}).get("cmd", "") if isinstance(args, dict) else ""
        zh = _CMD_ZH.get(cmd, cmd or "")
        return "已" + zh if zh else "已执行向导命令"
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return json.dumps(_zh_keys(obj), ensure_ascii=False, separators=(",", ":")).strip()
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return text.strip()


def _update_extract_progress(name, args, msg, ok):
    """把阅读/提取工具结果转成 /extract 可轮询的轻量状态。"""
    if name not in ("read_crawled_novel", "ingest_library_assets", "extract_state"):
        return
    from libraries.extract_progress import read_extract_progress, update_extract_progress
    args = args if isinstance(args, dict) else {}
    folder = args.get("folder", "") or read_extract_progress().get("folder", "")
    if not folder:
        return
    raw = _extract_result_text(msg)
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        result = {}
    if name == "read_crawled_novel":
        chapters = result.get("chapters") or []
        if chapters:
            lo = chapters[0].get("index", 0)
            hi = chapters[-1].get("index", 0)
        else:
            lo = hi = 0
        update_extract_progress(
            folder=folder, title=result.get("title", ""),
            active=True, status="reading", read_start=lo, read_end=hi,
            cursor=hi, last_tool=name,
            message=f"正在阅读第 {lo}-{hi} 章" if hi else "正在读取章节",
        )
    elif name == "ingest_library_assets" and ok:
        stats = result.get("stats") or result
        old = read_extract_progress().get("committed") or {}
        committed = {k: int(old.get(k, 0) or 0) + int(stats.get(k, 0) or 0)
                     for k in ("plots", "structures", "gags", "characters")}
        update_extract_progress(
            folder=folder, active=True, status="staging", committed=committed,
            last_tool=name, message="候选已通过闸门并存入暂存区",
        )
    elif name == "extract_state" and ok and args.get("action") == "save":
        state = args.get("state") or {}
        update_extract_progress(
            folder=folder, active=state.get("status", "running") != "done",
            status="done" if state.get("status") == "done" else "paused",
            cursor=state.get("cursor", 0), last_tool=name,
            message="整本提取完成" if state.get("status") == "done" else "已保存阅读记忆",
        )


_DOMAIN_BY_TOOL = {
    "save_plot_draft": "plot_run_changed",
    "save_chapter_text": "chapter_changed",
    "save_outlines": "plan_committed",
    "chapter_quality_gate": "quality_gate",
}


def _domain_event_from_tool(name, args, msg) -> dict | None:
    """写作相关工具成功后，派生领域事件（WS6，修订 7）。

    用 call_id → tool args 任务级映射取参数（**禁止从格式化字符串猜**）；storyline_revision
    尽量从结果 JSON 读，读不到就省去（refresh 时会重算）。事件只是 UI 刷新信号，非持久业务状态。
    """
    dom = _DOMAIN_BY_TOOL.get(name)
    if not dom:
        return None
    args = args or {}
    ev = {"name": dom, "book_id": args.get("book_id") or ""}
    try:
        if args.get("chapter_num") is not None:
            ev["chapter"] = int(args["chapter_num"])
    except (TypeError, ValueError):
        pass
    if args.get("plot_id"):
        ev["plot_id"] = str(args["plot_id"])
    try:
        txt = _extract_result_text(msg) if callable(_extract_result_text) else ""
        obj = json.loads(txt) if (txt or "").lstrip().startswith("{") else None
        if isinstance(obj, dict):
            if obj.get("storyline_revision") is not None:
                ev["storyline_revision"] = obj["storyline_revision"]
            if "chapter" not in ev and obj.get("chapter") is not None:
                ev["chapter"] = obj["chapter"]
            if "plot_id" not in ev and obj.get("plot_id"):
                ev["plot_id"] = obj["plot_id"]
            if obj.get("book_id"):
                ev["book_id"] = obj["book_id"]
            if obj.get("flow_id"):
                ev["flow_id"] = obj["flow_id"]
    except Exception:
        pass
    return ev


def _map_dsh_event(evt: dict, pending: dict):
    """一行 NDJSON 事件 → SSE 事件（生成器，可产 0..N 条）。

    pending: {callId: {"name","callId"}} —— tool/call 按 callId 记、tool/result 按
    message.source.callId 取，用来给 result 补工具名（result 自身不带 name）。
    用 callId 作键（而非 {turn}.{step}）：dsh 一个 assistant 消息可带多个 tool_calls
    （并行），同 turn/step 会碰撞覆盖，callId 天然去重。
    navigate / drive_ui 的 tool/call 除了转成 navigate / ui_command 推送（浏览器
    执行跳转/向导命令），**同时**发 tool_call 建卡——否则它们的 tool/result 在前端
    找不到卡，会 fallback 污染上一张卡（曾把 drive_ui 错误贴到别的工具卡）。
    """
    t = evt.get("type")
    data = evt.get("data") or {}
    if t == "tool/call":
        name = _short_name(data.get("name", ""))
        call_id = data.get("callId", "")
        args = _parse_args(data.get("arguments"))
        pending[call_id] = {"name": name, "callId": call_id, "args": args}
        if name in ("read_crawled_novel", "ingest_library_assets"):
            try:
                from libraries.extract_progress import read_extract_progress, update_extract_progress
                folder = (args or {}).get("folder", "") or read_extract_progress().get("folder", "")
                if folder and name == "read_crawled_novel":
                    lo = int((args or {}).get("start_chapter") or (args or {}).get("chapter") or 0)
                    hi = int((args or {}).get("end_chapter") or lo)
                    update_extract_progress(
                        folder=folder, active=True, status="reading",
                        read_start=lo, read_end=hi, last_tool=name,
                        message=f"正在阅读第 {lo}-{hi} 章" if hi else "正在读取章节",
                    )
                elif folder:
                    staged = {k: len((args or {}).get(k) or [])
                              for k in ("plots", "structures", "gags", "characters")}
                    update_extract_progress(
                        folder=folder, active=True, status="staging",
                        staged=staged, last_tool=name, message="候选正在进入暂存区",
                    )
            except Exception:
                pass
        yield {"type": "tool_call", "name": name, "args": args, "callId": call_id,
               "usage": data.get("usage")}   # dsh agent 该工具调用的真实 token 用量（events-runner 转发）
        if name == "navigate":
            url = args.get("url") if isinstance(args, dict) else ""
            if url:
                yield {"type": "navigate", "url": url}
        elif name == "drive_ui":
            # 工具参数是 {cmd, args:{...}} 两层：cmd 取顶层，实际载荷取内层 args，
            # 与 nav-intent 通道（push_ui_command 存内层 args）保持一致；否则浏览器
            # 拿到整参（含 cmd 键），set_candidates/set_world 等带参命令 args 全部落空。
            inner = args.get("args") if isinstance(args, dict) and isinstance(args.get("args"), dict) else {}
            if args.get("cmd") == "set_review":
                try:
                    from libraries.extract_progress import update_extract_progress
                    review = dict(inner)
                    staged = {k: len(review.get(k) or [])
                              for k in ("plots", "structures", "gags", "characters")}
                    update_extract_progress(
                        folder=review.get("folder", ""),
                        title=review.get("title", ""), active=True, status="staging",
                        review=review, staged=staged, last_tool="set_review",
                        message="候选已存入暂存区，等待确认入库",
                    )
                except (ImportError, OSError, TypeError, ValueError):
                    pass
            yield {"type": "ui_command",
                   "cmd": args.get("cmd") if isinstance(args, dict) else "",
                   "args": inner}
    elif t == "tool/result":
        msg = data.get("message") or {}
        source = msg.get("source") or {}
        call_id = source.get("callId") or ""
        p = pending.pop(call_id, {}) or {}
        name = p.get("name") or ""
        ok = not data.get("error") and not _result_error(msg)
        try:
            _update_extract_progress(name, p.get("args"), msg, ok)
        except (ImportError, OSError, TypeError, ValueError):
            pass
        yield {"type": "tool_result",
               "name": name,
               "callId": call_id or p.get("callId") or "",
               "ok": ok,
               "summary": _zh_tool_summary(name, p.get("args"), msg)}
        # WS6：写作相关工具成功后追加领域事件（与既有事件同流下发，events-runner 无需改）
        if ok:
            _dom = _domain_event_from_tool(name, p.get("args"), msg)
            if _dom:
                yield {"type": "domain", **_dom}
    elif t == "llm/call":
        # 调试模式（NOVEL_AGENT_DEBUG=1 时 events-runner 才 emit）：一次 LLM 调用的
        # 提示词/MCP工具/返回JSON，前端渲染「LLM 调用」调试卡。不持久化 task_events。
        request = data.get("request") or {}
        host_tools = request.get("tools") if isinstance(request, dict) else None
        yield {"type": "llm_call",
               "seq": data.get("seq"), "turn": data.get("turn"), "step": data.get("step"),
               "request": request, "response": data.get("response"),
               "usage": data.get("usage"), "input_budget": data.get("input_budget"),
               "host_tool_count": len(host_tools) if isinstance(host_tools, list) else None}
    elif t == "reply":
        yield {"type": "reply", "content": data.get("text") or ""}
    elif t == "error":
        yield {"type": "error", "message": data.get("message") or "dsh 任务出错"}
    elif t == "done":
        yield {"type": "done"}


def run_dsh_task(task: str, history: list | None = None, debug: bool = False,
                 flow_id: str = "", child_run_id: str = "", book_id: str = "",
                 mcp_profile: str = ""):
    """跑一次 dsh headless 任务，实时产出 SSE 事件 dict。

    事件序列（由 events-runner 的 NDJSON 流实时驱动）：tool_call / tool_result /
    navigate / ui_command …… → reply（最终回复）→ done。
    失败/异常/被打断：error + done。task 为最新用户消息，history 为浏览器持有的消息列表。
    任务不受整任务时长限制：跑到完成（stdout EOF 自然收尾）或被 /api/agent/chat/cancel
    打断为止；单工具调用超时由运行期 overlay 的 toolCallTimeoutMs 独立兜底。

    全服务单任务：本任务启动前先 interrupt_current_task() 打断任何正在跑的 dsh；
    本任务也可被后续任务 / `/api/agent/chat/cancel` 打断（被打断则 error+done 收尾）。
    finally 里收尸（杀残留 proc + wait），避免孤儿进程。

    mcp_profile：**服务端显式指定的工具面**（`_build_fsm` / `_writer_fsm` 按阶段定）。
    给了就用它，不给才按任务文本猜；两者都不确定时收敛到只读 inspect——见
    `_resolve_run_profile` 的 fail-open 说明，任何情况下都别让空 profile 流下去。
    """
    # 全服务单任务：新任务先打断正在跑的旧任务。事件存储（task_events.jsonl）不清空——
    # 它是「刷新重建」的渲染源（重启服务才消失），跨任务累积，仅显式「清空对话」清空。
    interrupt_current_task()
    profiles_enabled = _profiles_enabled()
    if not profiles_enabled and not _unscoped_tools_allowed():
        yield {"type": "error", "message": (
            "AGENT_TOOL_PROFILES 已关闭；如需开发/诊断 legacy 全量工具，"
            "请同时显式设置 ALLOW_UNSCOPED_AGENT_TOOLS=1")}
        yield {"type": "done"}
        return
    profile = _resolve_run_profile(task, mcp_profile)
    skill_text = _skill_text_for_profile(profile) if profile else ""
    # P0 预检：注入的 skill 引用必须 ⊆ 该 profile 暴露的工具；不匹配=契约破损，明确报错并拒绝启动，
    # 绝不自动 fail-open 到全量 45（AGENT_TOOL_PROFILES=0 才是显式 legacy/debug 逃生）。
    if profile and skill_text:
        try:
            from libraries.skill_profile import missing_for_text
            _missing = missing_for_text(skill_text, profile)
            if _missing:
                _log.warning("skill/profile 契约不匹配 profile=%s missing=%s", profile, _missing)
                yield {"type": "error",
                       "message": (f"skill/profile 契约不匹配：profile={profile} 未暴露工具 "
                                   f"{_missing}。请修复 skill 引用或对应 PROFILE_TOOLS 后重试；"
                                   "不会自动回退全量工具。")}
                yield {"type": "done"}
                return
        except Exception:  # noqa: BLE001 —— 预检失败不应在已打通过的链路上制造崩溃，交给后续 unknown-tool 兜底
            _log.warning("skill/profile 预检异常（跳过，按现有行为继续）", exc_info=True)
    overlay = _write_runtime_overlay(mcp_profile=profile)
    task_text = _build_task_text(task, history)
    if skill_text:
        skill_block = "\n\n[当前 Skill，必须遵守]\n" + skill_text
        if len(task_text) + len(skill_block) > _MAX_TASK_CHARS:
            task_text = "...(旧历史已为 Skill 预算截断)...\n" + task_text[-(_MAX_TASK_CHARS - len(skill_block) - 40):]
        task_text += skill_block
    cmd = get_dsh_argv() + [
        "--profile", get_dsh_profile(),
        "--patch", overlay, task_text,
    ]

    proc = None
    saw_any = False
    saw_done_event = False
    _log.info("dsh task start: %s…", (task or "").strip()[:80])
    try:
        try:
            ensure_proxy()   # 保证本地 token 代理(58082)已监听，dsh 的 LLM 调用才能走它计 token
            env = {**os.environ, "DEEPSEEK_BASE_URL": "http://127.0.0.1:58082"}
            if flow_id:
                env["NOVEL_WRITE_FLOW_ID"] = flow_id
            if child_run_id:
                env["NOVEL_WRITE_CHILD_RUN_ID"] = child_run_id
            if book_id:
                # 让 save_plot_draft 免去「全库扫令牌账本」：子进程只带回 commit_token，
                # 归属书由服务端注入 → 只查一本书（O(1)），且提示不符时 fail-closed。
                env["NOVEL_WRITE_BOOK_ID"] = book_id
            if debug:
                # 调试模式：通知 events-runner 把每次 LLM 调用的提示词/MCP工具/返回JSON emit 成 llm/call；
                # 完整载荷写 storage/debug-prompts/<seq>.json（SSE 只发裁剪预览，前端按需 fetch）。
                # 关闭时不注入 → 子进程不发数据，零开销。
                env["NOVEL_AGENT_DEBUG"] = "1"
                env["DEBUG_PROMPT_DIR"] = _DEBUG_PROMPT_DIR
                # 每任务清空上次残留，防 seq 复用碰撞（顺带回收磁盘）
                shutil.rmtree(_DEBUG_PROMPT_DIR, ignore_errors=True)
                os.makedirs(_DEBUG_PROMPT_DIR, exist_ok=True)
            proc = subprocess.Popen(
                cmd, cwd=_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                # 让 dsh 的 LLM 走本地代理：DEEPSEEK_BASE_URL 是 bootstrap-only 变量
                # （只能来自启动进程环境，写 .env 会抛错），dsh 解析链 baseURL 优先取它。
                env=env,
            )
        except FileNotFoundError:
            yield {"type": "error",
                   "message": "vendor/dsh-ne 或 node 缺失：请确认 `vendor/dsh-ne/node_modules` 已 `npm install`"
                              "（源码入库，依赖重建），且 Node 在 PATH"}
            yield {"type": "done"}
            return
        _set_current_proc(proc, task, flow_id=flow_id, child_run_id=child_run_id)

        q = queue.Queue()
        stderr_buf = []

        def _reader():
            try:
                for line in proc.stdout:
                    q.put(("line", line))
                q.put(("eof", None))
            except Exception as e:  # pragma: no cover
                _log.error("dsh stdout reader failed: %s", e)
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
        while True:
            try:
                kind, payload = q.get(timeout=0.5)
            except queue.Empty:
                # 子进程退出后 stdout 关闭，reader 必发 eof；此处无事件仅空转限速，
                # 不做整任务时长兜底（长任务可能远超 15 分钟，靠 cancel / eof 收尾）
                continue
            if kind == "line":
                line = payload.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning("dsh bad json line (%d bytes): %.160s", len(line), line)
                    continue
                saw_any = True
                _log.debug("dsh event %s (%d bytes)", evt.get("type", ""), len(line))
                for sse in _map_dsh_event(evt, pending):
                    if flow_id:
                        sse.setdefault("flow_id", flow_id)
                    if child_run_id:
                        sse.setdefault("child_run_id", child_run_id)
                    # 持久化所有可渲染卡片（工具卡 + 调试模式 LLM 调用卡），供刷新后重建卡片流
                    if sse.get("type") in ("tool_call", "tool_result", "llm_call"):
                        _append_task_event(sse)
                    if sse.get("type") == "done":
                        saw_done_event = True
                    yield sse
            elif kind == "eof":
                _log.debug("dsh stdout EOF")
                break
            else:
                _log.warning("dsh reader error kind=%s", kind)
                break

        exit_code = proc.wait()
        _log.info("dsh task end: exit=%s saw_done=%s saw_any=%s elapsed=%.1fs",
                  exit_code, saw_done_event, saw_any, time.time() - started)
        # 未以 done 收尾且退出非 0：判为被打断 / 崩溃（events-runner 正常结束必有 done 事件）
        if exit_code != 0 and not saw_done_event:
            _log.warning("dsh abnormal end (stderr tail): %s", "".join(stderr_buf)[-500:])
            if _last_interrupted_pid == proc.pid:
                yield {"type": "error", "message": "任务已被打断"}
            elif not saw_any:
                err = "".join(stderr_buf)[-500:].strip() or "(dsh 无输出)"
                yield {"type": "error", "message": f"dsh 任务失败（exit {exit_code}）：{err}"}
            else:
                yield {"type": "error", "message": f"dsh 任务异常退出（exit {exit_code}）"}
            yield {"type": "done"}
    finally:
        # 客户端断连（GeneratorExit）时**不杀子进程**——任务继续在后台跑完
        # （dsh headless 一次性任务会自终止），前端切页后经 get_current_task_status
        # 感知/取消；仅当 proc 确已退出才清槽位（正常结束 / 被 interrupt 杀掉）。
        # 断连后跑完的任务由 get_current_task_status 惰性清槽，不会残留。
        if proc is not None and proc.poll() is not None:
            _clear_current_proc(proc)


# ─── R5：NEED_REPLAN 交接 + 自动 replan 编排（修订 1）───
#
# write run（profile=write，无 replan 工具）在 prepare_plot_run 读到
# planning.boundary.needs_replan 且本 run 无更多可写 plot 时，不得越权调 replan 工具，
# 而是在最终回复末尾输出一行机器可读交接：
#     [NEED_REPLAN] book_id=<id> reason=<PLOTS_LOW;WORDS_LOW>
# orchestrator 在此检测该标记：吞掉该 run 的中间 done → 自动 spawn profile=replan 的
# dsh run（skill 文本由 bridge 按 replan profile 注入）→
#   REPLAN_POLICY=auto：replan 生成 preview 后 orchestrator 经共享 replan_service 原子
#                       提交（storyline+planning 同边界）→ 再 spawn write run 续写；
#   REPLAN_POLICY=confirm：replan 只暂存 preview 即停，等用户在 UI 抽屉确认
#                       （commit-plan HTTP，同一 replan_service）。
# 保证：最终只发一个 done；子 run 的 done 全部吞掉（防前端误以为对话已结束）。

_NEED_REPLAN_RE = None  # 惰性编译（import re 一次）


def _need_replan_re():
    global _NEED_REPLAN_RE
    if _NEED_REPLAN_RE is None:
        import re as _re
        _NEED_REPLAN_RE = _re.compile(r"^\s*\[NEED_REPLAN\]\s+(.*)$", _re.MULTILINE)
    return _NEED_REPLAN_RE


def parse_need_replan(text: str) -> dict | None:
    """从 write run 最终文本里解析 [NEED_REPLAN] 交接。返回 {book_id, reason} 或 None。"""
    if not text:
        return None
    m = _need_replan_re().search(text)
    if not m:
        return None
    fields = {}
    import re as _re
    # 只按空白切字段，不把 reason 内部的 ";" 当分隔（reason_codes 是 PLOTS_LOW;WORDS_LOW）
    for kv in _re.split(r"\s+", m.group(1).strip()):
        if "=" in kv:
            k, _, v = kv.partition("=")
            fields[k.strip().lower()] = v.strip()
    bid = fields.get("book_id")
    return {"book_id": bid, "reason": fields.get("reason", "")} if bid else None


def _replan_policy(policy: str | None) -> str:
    p = (policy or os.environ.get("REPLAN_POLICY") or "auto").strip().lower()
    return p if p in ("auto", "confirm") else "auto"


def _replan_phase_task(book_id: str, reason: str, policy: str) -> str:
    if policy == "confirm":
        mode = "本任务只生成并暂存续规划预览即结束；等待用户在界面确认后才提交，请勿自行提交。"
    else:
        mode = "本任务由系统自动触发，生成的预览会被系统自动原子提交并继续写作；请勿等待界面确认。"
    return (f"[系统自动触发续规划] 书 {book_id} 已临近已承诺故事边界"
            f"（{reason or '规划余量不足'}）。请按 novel-replan 流程：只把当前事实当不可改、"
            f"生成下一段 H0 committed + H1/H2 forecast，经 drive_ui(set_replan_preview) 暂存 preview。{mode}")


def _resume_write_task(book_id: str) -> str:
    return (f"[系统自动续写] 书 {book_id} 的续规划已提交、故事线已前移。"
            "请继续按写作流程（novel-story）从下一个未写情节段开始写下一章正文。")


def _annotated_reply(content: str) -> dict:
    """去掉交接标记行，换成面向用户的一句说明。"""
    text = _need_replan_re().sub("", content or "").strip()
    note = "系统检测到已承诺故事边界，将自动续规划后继续写作。"
    return {"type": "reply", "content": (text + "\n\n" + note) if text else note}


# 同一个 Flow 内允许几轮「新起计划器」；用尽后不再烧 LLM 会话，直接失败并给出可操作提示。
# 在途预览会被复用（不重复起计划器），所以只有计划器真的没产出/产出陈旧时才计数。
MAX_REPLAN_ATTEMPTS = 2


def _current_revision(book_id: str) -> int:
    from agent_tools import load_tl
    tl = load_tl(book_id)
    return int(getattr(tl, "storyline_revision", 0) or 0) if tl is not None else 0


def _replan_attempts(book_id: str, flow_id: str) -> int:
    if not (book_id and flow_id):
        return 0
    from libraries.write_flow import load_flow
    flow = load_flow(book_id, flow_id) or {}
    state = flow.get("replan_state") if isinstance(flow.get("replan_state"), dict) else {}
    try:
        return int(state.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _fresh_replan_preview(book_id: str, revision: int) -> dict:
    """在途预览只有在 `expected_revision` 等于当前故事线版本时才可复用。

    陈旧预览必须重新规划——否则提交时会被 CAS 拒绝，白跑一轮计划器还中断写作。
    """
    from libraries.planning_state import load_replan_preview
    preview = load_replan_preview(book_id) or {}
    if not preview:
        return {}
    expected = preview.get("expected_revision")
    if isinstance(expected, bool) or not isinstance(expected, int):
        return {}
    return preview if int(expected) == int(revision) else {}


def _commit_pending_replan(book_id: str) -> dict:
    """把在途 preview 交给共享 replan_service 原子提交（与 UI commit-plan 同一入口）。

    成功判据只用 service 显式的 `commit_ok`：返回载荷里的 `ok` 是规划 UI 聚合接口的 ok，
    不是提交结果，用它会「提交成功却报失败」。
    """
    from libraries.planning_state import load_replan_preview
    from libraries.replan_service import commit_replan_preview
    preview = load_replan_preview(book_id) or {}
    if not preview:
        return {"commit_ok": False, "error": "no_pending_preview"}
    expected = preview.get("expected_revision")
    if isinstance(expected, bool) or not isinstance(expected, int):
        return {"commit_ok": False, "error": "preview_missing_revision"}
    return commit_replan_preview(book_id, preview.get("preview_id"), expected)


@contextlib.contextmanager
def _lease_heartbeat(book_id: str, flow_id: str, interval: float = 60.0):
    """子 run 运行期间后台续租（上下文管理器）。

    租约 TTL 15 分钟，而单个 Writer/Planner 子进程可能跑 10 分钟以上（单工具调用超时
    就是 600s），期间没有 transition → 不会心跳。若租约在此期间到期并被别的任务接管，
    本 run 后续 transition 会抛「写作租约不属于当前流程」，状态机半路失联。
    这里在有身份的 run 期间起守护线程定期 heartbeat；失败只记日志——真丢租约时，
    下一次 transition 会明确报错。
    """
    stop = threading.Event()
    thread = None

    def _beat():
        from libraries.write_flow import heartbeat_lease
        while not stop.wait(interval):
            try:
                heartbeat_lease(book_id, flow_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("写作租约续租失败 book=%s flow=%s: %s", book_id, flow_id, exc)

    if book_id and flow_id:
        thread = threading.Thread(target=_beat, name=f"lease-{flow_id[:8]}", daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1.0)


def _fail_flow(book_id: str, flow_id: str, error: str, message: str):
    """统一的失败出口：转 FAILED + **释放租约** + 一个 error 与 done。

    此前各失败分支各写各的，其中两处漏释放租约 → 失败后租约白占 15 分钟，
    下一次写作还会被 active_flow_id 吸到这个已 FAILED 的 Flow 上。
    """
    from libraries.write_flow import release_lease, transition
    if flow_id:
        try:
            transition(book_id, flow_id, "FAILED", error=error)
        except Exception as exc:  # noqa: BLE001
            _log.warning("flow 置 FAILED 失败 book=%s flow=%s: %s", book_id, flow_id, exc)
        try:
            release_lease(book_id, flow_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("租约释放失败 book=%s flow=%s: %s", book_id, flow_id, exc)
    yield {"type": "error", "message": message}
    yield {"type": "done"}


def _writer_fsm(task: str, history: list | None, debug: bool, policy: str | None):
    """一个父 Flow 调度多个独立 Writer 子进程；模型从不决定下一状态。

    每轮先从磁盘草稿状态判定动作（含首轮，避免满章草稿 / 规划边界先空跑一个注定无法提交的
    Writer 子 run 而被误报「未完成有效 Plot 提交」）：满章草稿→服务端收章、无剩余可写 Plot
    且到边界→续规划；仅确有可写 Plot 时才 spawn 一次性 Writer 子 run。Writer 子 run 一律收
    归一化薄任务（工具面只有 prepare_plot_run/save_plot_draft），不喂整段用户长文本，杜绝任务
    里出现其工具面外工具名导致 unknown-tool 停摆。
    """
    from agent_tools import (_draft_read, _runtime_written_words, finalize_draft_chapter,
                             load_tl)
    from libraries.planning_state import detect_story_boundary, load_planning_state
    from libraries.write_flow import (chapter_status, next_action, load_flow, transition,
                                      start_flow, active_flow_id)
    import re

    pol = _replan_policy(policy)

    def _book_in(text: str) -> str:
        m = re.search(r"\b(book[_-][A-Za-z0-9_-]+)\b", text or "", re.I)
        return m.group(1) if m else ""

    def _plot_task(bid: str) -> str:
        # 归一化续写任务：只说「写当前 Plot」，不塞任何流程/工具名。
        return f"[服务端续写] 继续书 {bid} 的当前 Plot，写完后停止。"

    def _flow_for(bid: str, chapter_num: int = 1) -> str:
        """取已活跃 Flow；没有则补建一个（供收章/续规划/审计有落点）。"""
        if not bid:
            return ""
        try:
            return active_flow_id(bid) or start_flow(bid, chapter_num)["flow_id"]
        except Exception:
            return ""

    def _evaluate(bid: str):
        """按磁盘草稿返回 (status, action)；book 未知或故事线缺失 → (None, None)。"""
        if not bid:
            return None, None
        tl = load_tl(bid)
        if tl is None:
            return None, None
        draft = _draft_read(bid) or {}
        # 仅**读**规划态判边界：必须 persist=False。此前用默认 persist=True 且 book=None，
        # 会把 planning_state.written_until_word 覆写成 0 并落盘（与 UI 读路径互相打架）。
        ps = load_planning_state(bid, tl, None, persist=False)
        drafted = {x.get("plot_id") for x in draft.get("bridges") or []}
        remaining = sum(1 for p in tl.plots if not getattr(p, "written_chapter", 0) and p.id not in drafted)
        # 边界在**绝对轴**上比较，已写字数必须与 committed_until_word 同轴：
        # 已落盘章节正文 + 当前草稿（_runtime_written_words）。绝不能用 draft["words"]——
        # 那是本章草稿的内部计数（章内量纲），会让 WORDS_LOW 永不触发。
        boundary = detect_story_boundary(
            written_until_word=_runtime_written_words(bid, tl, None, draft),
            committed_until_word=int(ps.get("committed_until_word") or 0),
            remaining_plots=remaining,
            words_per_batch=int(tl.words_per_chapter or 3000),
            storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
            last_replan=ps.get("last_replan") or {})
        status = chapter_status(bid, tl, draft, needs_replan=bool(boundary.get("needs_replan")))
        return status, next_action(status)

    book_id = _book_in(task)
    flow_id = active_flow_id(book_id) if book_id else ""
    child_no = 0
    while True:
        status, action = _evaluate(book_id)
        # ── 收章：草稿已满但尚未落盘为章节（含中断恢复，不必先 spawn 写手）──
        if action == "COMMITTING_CHAPTER":
            if not flow_id:
                flow_id = _flow_for(book_id, int((_draft_read(book_id) or {}).get("chapter_num") or 1))
            try:
                transition(book_id, flow_id, "COMMITTING_CHAPTER")
                committed = finalize_draft_chapter(book_id, flow_id)
                yield {"type": "domain", "name": "chapter_changed", "book_id": book_id, "flow_id": flow_id,
                       "chapter": committed.get("chapter"), "phase": "DONE"}
                # 章已在盘上：下面的都是**提交后诊断**，只提示、绝不改口说「章没提交」。
                notes = []
                if committed.get("state_error"):
                    notes.append(f"注意：{committed['state_error']}")
                gate = committed.get("quality_gate") or {}
                if isinstance(gate, dict) and (gate.get("skipped") or gate.get("ok") is False):
                    notes.append("质量门禁异常已跳过（章已提交，可稍后重跑门禁）")
                if notes:
                    yield {"type": "reply", "content":
                           f"第{committed.get('chapter')}章已提交（正文已落盘）。" + "；".join(notes)}
                else:
                    yield {"type": "reply", "content": f"第{committed.get('chapter')}章已由服务端提交并完成质量门禁。"}
            except Exception as exc:
                yield from _fail_flow(book_id, flow_id, f"chapter_commit_failed:{exc}",
                                      f"章节提交失败：{exc}")
                return
            yield {"type": "done"}
            return
        # ── 续规划：无剩余可写承诺 Plot 且到达规划边界 ──
        if action == "REPLANNING":
            if not flow_id:
                flow_id = _flow_for(book_id)
            revision = _current_revision(book_id)
            # attempts 计的是「自上次成功出品以来连续失败/无产的续规划轮数」，成功出一段 Plot
            # 或提交成功即归零（见下面 EVALUATING / PREPARING_PLOT 两处 reset）——否则长章节里
            # 多次合法的续规划会被误判成死循环而中断写作。
            attempts = _replan_attempts(book_id, flow_id)
            # 在途预览（且版本仍新鲜）直接复用：不重复起计划器，不烧第二次 LLM 会话。
            preview = _fresh_replan_preview(book_id, revision)
            if not preview:
                if attempts >= MAX_REPLAN_ATTEMPTS:
                    yield from _fail_flow(
                        book_id, flow_id, "replan_attempts_exhausted",
                        f"续规划连续 {attempts} 轮未产出可用故事线（书 {book_id}）；本轮写作已停止。"
                        "请检查规划预览与情节段配置后再继续。")
                    return
                try:
                    transition(book_id, flow_id, "REPLANNING",
                               replan_state={"reason": status["reason"], "attempts": attempts + 1})
                except Exception as exc:  # noqa: BLE001
                    _log.warning("flow 置 REPLANNING 失败 book=%s flow=%s: %s", book_id, flow_id, exc)
                # Planner 仍是独立 profile；只让它按局部 state 产出下一小段 committed Plot。
                ok = True
                with _lease_heartbeat(book_id, flow_id):
                    for evt in run_dsh_task(_replan_phase_task(book_id, status["reason"], pol), None,
                                            debug=debug, book_id=book_id, flow_id=flow_id,
                                            child_run_id=f"planner:{attempts + 1}"):
                        if evt.get("type") == "error": ok = False
                        if evt.get("type") != "done": yield evt
                # 计划器可能改过故事线，按最新版本再判一次新鲜度。
                preview = _fresh_replan_preview(book_id, _current_revision(book_id)) if ok else {}
                if not preview:
                    detail = "计划器执行出错" if not ok else "计划器未暂存可用预览（或预览版本已过期）"
                    yield {"type": "reply", "content":
                           f"{detail}，将重试续规划（第 {attempts + 1}/{MAX_REPLAN_ATTEMPTS} 轮）。"}
                    continue     # 回到顶部重试；连续超限由上面的 attempts 上限兜住
            if pol == "confirm":
                # 只暂存预览即停，等用户在故事线面板确认（commit-plan 走同一 replan_service）。
                # 租约保留在 WAIT_CONFIRM 上（无进程在跑；下一次「继续写」会复用同一 Flow 并命中
                # 在途预览分支，不会重复起计划器）。
                try:
                    transition(book_id, flow_id, "WAIT_CONFIRM",
                               replan_state={"reason": status["reason"], "attempts": attempts,
                                             "preview_id": preview.get("preview_id")})
                except Exception as exc:  # noqa: BLE001
                    _log.warning("flow 置 WAIT_CONFIRM 失败 book=%s flow=%s: %s", book_id, flow_id, exc)
                yield {"type": "reply", "content":
                       "本章尚未达到目标字数且已承诺 Plot 用尽；续规划预览已生成，"
                       "请在故事线面板确认后再次「继续写」。"}
                yield {"type": "done"}
                return
            # auto：经共享 service 原子提交（在途预览直接复用）。
            result = _commit_pending_replan(book_id)
            if not result.get("commit_ok"):
                reason = result.get("error") or result.get("message") or "未知原因"
                yield {"type": "reply", "content":
                       f"续规划提交未成功（{reason}），将重试续规划"
                       f"（第 {attempts + 1}/{MAX_REPLAN_ATTEMPTS} 轮）。"}
                continue
            try:
                transition(book_id, flow_id, "PREPARING_PLOT",
                           replan_state={"reason": "", "attempts": 0})   # 出品成功 → 计数归零
            except Exception:
                pass
            continue   # 故事线已前移 → 回顶部应出现可写 Plot
        if action == "FAILED":
            # plot_exhausted_without_replan / 不变量：没有可写 Plot 且未满足续规划条件
            yield from _fail_flow(book_id, flow_id, "chapter_status_invariant",
                                  "章节状态不变量失败：没有可写 Plot 且未满足续规划条件。")
            return
        # ── 写一个 Plot：PREPARING_PLOT；book 未知时这是探路子 run（借其 domain 事件拿书与 flow）──
        child_no += 1
        child_id = f"writer:{child_no}"
        c_task = _plot_task(book_id) if book_id else task
        c_hist = None if book_id else history
        saved = None
        child_tail = ""
        with _lease_heartbeat(book_id, flow_id):   # 长子 run 期间持续续租，防租约到期易主
            for evt in run_dsh_task(c_task, c_hist, debug=debug, flow_id=flow_id,
                                    child_run_id=child_id, book_id=book_id):
                if evt.get("type") == "domain" and evt.get("name") == "plot_run_changed":
                    if evt.get("flow_id"):
                        flow_id = evt["flow_id"]
                    saved = evt
                if evt.get("type") == "reply":
                    child_tail = evt.get("content") or ""
                if evt.get("type") != "done":
                    yield evt
        if not book_id:
            # 探路子 run：从它的 domain 事件 / 任务文本恢复书与 flow
            book_id = (saved or {}).get("book_id") or _book_in(task)
            if not flow_id and book_id:
                flow_id = active_flow_id(book_id)
        # SSE/domain 只是 UI 信号，草稿账本才是提交事实。若子进程已写盘但 domain 事件在桥接层
        # 丢失，按草稿最后一桥恢复，绝不把已保存的 Plot 误报为失败。
        if book_id and (not saved or not flow_id):
            recovered_draft = _draft_read(book_id) or {}
            if recovered_draft.get("bridges"):
                if not flow_id:
                    flow_id = _flow_for(book_id, int(recovered_draft.get("chapter_num") or 1))
                last = (recovered_draft.get("bridges") or [])[-1]
                saved = {"book_id": book_id, "flow_id": flow_id, "plot_id": last.get("plot_id"),
                         "recovered_from_draft": True}
                yield {"type": "domain", "name": "write_flow_recovered", "book_id": book_id,
                       "flow_id": flow_id, "phase": "EVALUATING"}
        if not saved or not flow_id or not book_id:
            # 已判定「有可写 Plot」（或探路）却无任何提交 → 真实 Writer 失败，带上书号与子 run 尾回复便于排查
            msg = "Writer 未完成有效 Plot 提交；流程已停止。"
            if book_id:
                msg += f" book={book_id}"
            tail = child_tail.strip()
            if tail:
                msg += " " + tail[:200]
            yield {"type": "error", "message": msg}
            yield {"type": "done"}
            return
        # 子 Run 是 parent Flow 的可恢复审计记录，前端可按 flow 展开显示。
        flow = load_flow(book_id, flow_id) or {}
        children = list(flow.get("child_runs") or [])
        children.append({"child_run_id": child_id, "kind": "plot", "plot_id": saved.get("plot_id"),
                         "completed_at": time.time()})
        try:
            # 出一段 Plot = 实质推进 → 续规划连败计数归零（否则长章节里多次合法续规划会被上限误伤）
            transition(book_id, flow_id, "EVALUATING", child_runs=children,
                       replan_state={"reason": "", "attempts": 0})
        except Exception as exc:  # noqa: BLE001
            _log.warning("flow 置 EVALUATING 失败 book=%s flow=%s: %s", book_id, flow_id, exc)
        # 回到顶部：按新草稿状态决定收章 / 续写 / 续规划


# ─── 建书侧服务端 FSM：阶段由向导状态决定，模型不决定下一步 ───
#
# 修的是什么：`_task_tool_profile` 曾把向导步 2→3 的自动交接原文（含「已选定候选」）判成
# 步 1-2 的 build-candidates profile，而那个工具面没有 set_outline/set_world/set_characters
# （UI_COMMAND_POLICY 拒收）→ 故事线在工具层就没有写入路径（表现为「大纲生成失败」）。
# 现在阶段判定归服务端：`run_dsh_flow` 把 build/build-candidates 两类**一并**交给本函数，
# 由它读 storage/build_status.json（浏览器上报的向导快照）决定起哪个 profile 的子 run。
# 任务文本只用来取 `build_session` 标记，不再用来判阶段。

MAX_BUILD_ATTEMPTS = 2


def _parse_build_session(task: str) -> str:
    """取任务文本里的 `build_session=<sid>` 结构化标记（向导交接任务自带）。

    这是**机器可读标记**、不是中文关键词猜测：`build_status.json` 是全局单快照，
    标记用于把「这条任务属于哪个向导页」钉死（错配判据见 _build_fsm）。
    """
    import re
    m = re.search(r"build[_ ]?session\s*=\s*([A-Za-z0-9_-]+)", task or "", re.I)
    return m.group(1) if m else ""


def _build_step3_task() -> str:
    # 归一化薄任务：只说做什么，**不枚举工具名**——任务里出现工具面外的工具名会让 run 因
    # unknown-tool 停摆（同 _writer_fsm._plot_task 的注释）。
    return ("[服务端建书] 为当前建书向导会话填写步 3「内容构建工作台」：世界观、弧与情节段、"
            "势力与人物、其余表单维度。填完表单停下，向用户汇报设定概要并等待用户确认；"
            "不要自行提交建书。")


def _build_candidates_task() -> str:
    return "[服务端建书] 为当前建书向导会话生成世界观候选并逐张呈现，然后停下等用户在步 2 挑选。"


def _unroutable_hint() -> str:
    """未分类任务的显式指引（**不**静默回落只读面）。"""
    return ("这次没看出要做哪个阶段，所以没有启动 agent（避免落到只读面上白跑一轮）。\n"
            "请带上阶段动作再说一句，例如：\n"
            "  · 建书（步 1-2）：开新书 / 生成候选\n"
            "  · 建书（步 3）：继续建书 / 生成故事线 / 排弧 / 选情节段\n"
            "  · 写作：写下一章 / 继续写\n"
            "  · 续规划：续规划 / 扩弧\n"
            "  · 抓取：抓取番茄小说 / 侦察热榜\n"
            "  · 上架：检查能否发书 / 上架 / 完本\n"
            "若只是想问状态，直接问「查看/状态/进度」——那条是只读的，照常可用。")


def _build_fsm(task: str, history: list | None, debug: bool):
    """建书父 Flow：读向导快照定阶段 → 起一个对应 profile 的子 run → 停下等用户。

    **一轮只派发一个子 run**（不像 _writer_fsm 要循环出多段 Plot）：建书每步之后要么等用户
    挑选/确认、要么等用户自己点提交，没有「同一轮内持续推进」的动作。

    这里**不**阻塞等表单落地：`drive_ui` 是异步的（写 nav_intent 队列，浏览器每 ~2.5s
    轮询后才应用），而 58080 是**单线程** Flask——在此 sleep 等快照刷新会把浏览器自己的
    build-status 回报一起冻住，形成互等。所以只报「已发起填写」，成效留到下一轮看
    （`attempts` 记账见下）。同理，「建书已成功」只认快照里的 book_id，不猜。
    """
    from libraries.build_flow import (append_child_run, build_stage, load_flow,
                                      next_action, start_flow, transition)
    from libraries.build_status import get_build_status

    snap = get_build_status() or {}
    marker_sid = _parse_build_session(task)
    snap_sid = str(snap.get("build_session_id") or "")
    if marker_sid and snap_sid and marker_sid != snap_sid:
        # 快照属于另一个向导页 → 对本会话不可信，宁可当 STALE 退回关键词兜底
        _log.info("build flow: 快照 session 与任务标记不一致（%s vs %s），按 STALE 处理",
                  snap_sid, marker_sid)
        snap = {}
    session_id = marker_sid or snap_sid
    stage = build_stage(snap)
    action = next_action(stage)

    if action == "SUBMITTED":
        yield {"type": "reply", "content":
               f"这本已经建好了（book_id={stage['book_id']}，phase 随提交即 ready）。"
               "要开始写正文就说「写下一章」。"}
        yield {"type": "done"}
        return
    if action == "FAILED":
        yield {"type": "error", "message":
               f"建书提交失败：{stage['submit_error']}。请在向导页面按提示修正后重新提交。"}
        yield {"type": "done"}
        return

    if action == "BUILD":
        prof, target = "build", "BUILDING"
    elif action == "CANDIDATES":
        prof, target = "build-candidates", "STAGING"
    else:   # STALE：没有可信快照，退回任务文本判到的 profile（标记/关键词已是尽力而为）
        prof = _task_tool_profile(task) or "build-candidates"
        target = "BUILDING" if prof == "build" else "STAGING"
    child_task = _build_step3_task() if target == "BUILDING" else _build_candidates_task()

    # 连败记账：同一阶段重跑（上一轮没换来阶段推进）才累加，换阶段即归零；
    # 超过上限**显式失败**，不无限烧 LLM 会话。
    flow = load_flow(session_id) if session_id else None
    attempts = int((flow or {}).get("attempts") or 0)
    attempts = attempts + 1 if (flow or {}).get("resume_point") == target else 1
    if session_id and attempts > MAX_BUILD_ATTEMPTS:
        try:
            transition(session_id, "FAILED", attempts=attempts, error="no_progress")
        except Exception as exc:  # noqa: BLE001
            _log.warning("建书 Flow 置 FAILED 失败 session=%s: %s", session_id, exc)
        yield {"type": "error", "message":
               f"建书连续 {attempts - 1} 轮没换来阶段推进（当前停在 {target}），已停止以免空转。"
               "请打开「启动新书」向导自查页面状态（或手动填写步 3），再回来继续。"}
        yield {"type": "done"}
        return
    if session_id:
        try:
            if not flow:
                start_flow(session_id, mode=action.lower())
            transition(session_id, target, attempts=attempts)
        except Exception as exc:  # noqa: BLE001 —— 记账失败不该拦住真正的建书动作
            _log.warning("建书 Flow 置 %s 失败 session=%s: %s", target, session_id, exc)

    child_id = f"build:{attempts}"
    ok = True
    for evt in run_dsh_task(child_task, None, debug=debug, flow_id=session_id or "",
                            child_run_id=child_id, mcp_profile=prof):
        if evt.get("type") == "done":
            continue
        if evt.get("type") == "error":
            ok = False
        yield evt
    if session_id:
        try:
            append_child_run(session_id, target, kind=prof, ok=ok)
        except Exception as exc:  # noqa: BLE001
            _log.warning("建书 Flow 记子 run 失败 session=%s: %s", session_id, exc)

    if not ok:
        yield {"type": "error", "message":
               f"建书子任务（{prof}）执行出错，本轮未完成。可以重发一次；"
               "若仍失败，请在向导页面手动填写。"}
    elif prof == "build":
        yield {"type": "reply", "content":
               "步 3 内容已发起填写（表单由浏览器异步应用，几秒后生效）。请在页面 review "
               "世界观 / 弧与情节段 / 人物，确认无误后**自己点「创建并进入写作台」**"
               "——提交只能由你点，agent 不代提交。"}
    else:
        yield {"type": "reply", "content":
               "候选已逐张呈现在步 2。挑一个方向后点「已挑选完毕」，我会接着做步 3 的故事线。"}
    yield {"type": "done"}


def run_dsh_flow(task: str, history: list | None = None, debug: bool = False,
                 policy: str | None = None):
    """带 NEED_REPLAN 自动交接的多阶段 dsh 流（R5）。

    写作 / 建书两类走各自的服务端 FSM（阶段与 profile 由服务端状态决定，不问模型）；
    其余阶段（replan/publish/scout/style/inspect）走下面的通用单任务 + NEED_REPLAN 链。
    单次普通任务 → 与 run_dsh_task 等价（多一层 done 归一）。检测到 [NEED_REPLAN]
    交接则链式跑 replan（auto：提交后续写；confirm：停在预览等 UI 确认）。
    子 run 的 done 一律吞掉，全程只发一个尾部 done。
    """
    if _profiles_enabled():
        prof = _task_tool_profile(task)
        if prof == "write":
            yield from _writer_fsm(task, history, debug, policy)
            return
        # build 与 build-candidates **一并**交给 _build_fsm：两段交接文本不可分
        # （都含「候选」），阶段由它读向导快照定，别在这里按文本二选一。
        if prof in ("build", "build-candidates"):
            yield from _build_fsm(task, history, debug)
            return
        if not prof:
            # 未分类：不静默回落只读面（此前「大纲生成失败」只换来一串只读工具调用），
            # 显式报错 + 阶段指引。
            yield {"type": "error", "message": _unroutable_hint()}
            yield {"type": "done"}
            return
    import logging
    _log_local = logging.getLogger("dsh_bridge.flow")
    pol = _replan_policy(policy)
    need = None

    # Phase 1：用户任务（replan/publish/scout/style/inspect）。捕获 reply 里的 NEED_REPLAN。
    for evt in run_dsh_task(task, history, debug=debug):
        t = evt.get("type")
        if t == "reply":
            parsed = parse_need_replan(evt.get("content") or "")
            if parsed:
                need = parsed
                evt = _annotated_reply(evt.get("content") or "")
        if t == "done":
            continue
        yield evt

    if not need or not need.get("book_id"):
        yield {"type": "done"}
        return
    bid = need["book_id"]

    # Phase 2：replan run（profile=replan；skill 由 bridge 注入）。
    replan_ok = True
    for evt in run_dsh_task(_replan_phase_task(bid, need.get("reason", ""), pol),
                            None, debug=debug):
        t = evt.get("type")
        if t == "done":
            continue
        if t == "error":
            replan_ok = False
        if t == "reply" and pol == "auto":
            continue  # auto 下吞 replan 的散文回复，用 orchestrator 自己的状态行替代
        yield evt
    if not replan_ok:
        yield {"type": "error", "message": "自动续规划阶段出错，已停止。请稍后手动触发续规划。"}
        yield {"type": "done"}
        return

    if pol == "confirm":
        # 停在 preview；用户在 UI 确认 → commit-plan（同一 replan_service）→ 前端/后续续写。
        yield {"type": "done"}
        return

    # auto：经共享 service 原子提交 preview → 续写。
    # 成功判据用 service 的 commit_ok（不能看返回载荷的 ok——那是规划 UI 聚合接口的 ok）。
    committed = False
    try:
        committed = bool(_commit_pending_replan(bid).get("commit_ok"))
    except Exception as e:  # noqa: BLE001
        _log_local.warning("auto replan commit failed: %s", e)
    if not committed:
        yield {"type": "error", "message": "自动续规划提交未成功：请检查 replan 预览后在界面手动确认。"}
        yield {"type": "done"}
        return

    yield {"type": "reply", "content": "续规划已自动提交，故事线前移。继续写作。"}
    for evt in run_dsh_task(_resume_write_task(bid), None, debug=debug):
        if evt.get("type") == "done":
            continue
        yield evt
    yield {"type": "done"}
