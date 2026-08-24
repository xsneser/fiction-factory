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
  - 强化指令拼进任务文本前缀（persona 已在 headless profile 注入，这里按任务重申
    护栏：禁直建/直删（工具不在面）、phase 门控、防死循环轮询）。

本模块零新增 Python 依赖（subprocess + 标准库 + core.json_store.read_json）。
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

from libraries.token_proxy import ensure_proxy   # 拉起本地 token 检测代理（dsh 走它计 token）

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OVERLAY_PATH = os.path.join(_ROOT, "storage", "dsh_runtime.yml")

# ─── 全服务单任务：当前 dsh 子进程 + 打断（kill 整树）───
_current_proc = None
_current_task_meta = None   # {"started_at": float, "task": str}：当前运行任务元数据（前端切页恢复感知用）
_current_proc_lock = threading.Lock()
_last_interrupted_pid = None


def _set_current_proc(proc, task: str = ""):
    global _current_proc, _current_task_meta
    with _current_proc_lock:
        _current_proc = proc
        _current_task_meta = {"started_at": time.time(),
                              "task": (task or "").strip()[:80]}


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


# 强化指令：拼在任务文本前的护栏/编排提醒（persona 已在 headless profile 注入，
# 这里按任务重申关键约束，防 dsh 擅调越权工具 / 死循环轮询）。
_REINFORCEMENT = """[系统约束]
你是 NovelEngine 平台的外部驱动 agent。dsh 侧无 skill（2026-08-24 已删，仅 MCP 工具面），按 CLAUDE.md 四阶段 + MCP 工具直接驱动：
- 建书（开新书/建书/写设定/构思世界观/生成候选）：侧栏先 `navigate('/books/start')` 翻到步 1 表单（已给全 idea/tags 就预填，笔名留用户选），交用户点「🚀 让 Agent 构建」走按钮路径——你自主生成候选（每个必含 `title`，可带 `one_liner`/`world_brief`）逐个 `drive_ui(cmd="add_candidate", args={candidate:{title, one_liner, world_brief}})` 填入步 2，**title 不能缺否则浏览器拒收**；**停在步 2 等用户挑选，不自动选/跳步**；已选候选/补全世界观/继续建书→你自主生成步 3 内容（核心矛盾→大纲+桥段→势力→人物→其余世界观），`drive_ui(set_world/set_outline/set_characters)` 落表单 → `drive_ui(submit)` 建书（书创建即 phase=ready）→ `get_build_status` 拿 book_id 校验。
- 大纲（生成大纲/排故事线/续写扩写）：你自主生成 outlines/plots/threads/themes → `save_outlines` 落盘 → `fill_gags` 到 ready。
- 写作（开始写/写正文/写下一章）：你自主生成桥段正文 → `save_bridge_draft` 逐桥段落草稿 → 章满 `save_chapter_text` 落盘。
- 上架（上架/发布/完本/导出）：你自主生成书名简介 → `save_book_meta` → `publish_check` → `publish_book`/`mark_finished`/`export_book`。
- 删书→无 skill，`navigate(/books)` 让用户手动删（直删工具不在工具面）。
- 拿不准阶段→先 list_books + get_book_detail 看目标书 phase 再推进；书多先问「对哪本书操作」，不跨阶段硬做。
- 建书必须 drive_ui 驱动浏览器向导，删书必须 navigate /books 让用户手动删——直建/直删工具不在工具面。
- 工具被 phase 门控拒绝或抛 BookBusyError 时调整策略或稍后重试；同一只读工具同参调用超过 3 次即为循环，应停止并如实汇报。
- 薄工具（save_outlines / save_chapter_text）可能阻塞数分钟属正常，等待结果，不要反复用同参重查。"""


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
        "    # --source dsh：mcp_server 据此把工具日志 source 记为 dsh（不写 JSONL），\n"
        "    # 右侧「工具日志」页签只展示外部 agent（source=mcp）调用，内部 dsh 不混入\n"
        "    args: ['mcp_server.py', '--source', 'dsh']\n"
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


# ─── 工具结果中文转义（前端工具卡直接展示，去英文 key）───
_CMD_ZH = {
    "set_world": "写入世界观", "set_characters": "写入角色", "set_candidates": "填入候选",
    "pick_candidate": "选中候选", "set_field": "填写字段", "set_tags": "设置标签",
    "next": "下一步", "prev": "上一步", "reset": "重置向导", "submit": "提交建书",
    "skip_candidates": "跳过候选", "load_candidates": "加载候选", "fill_world": "重新补全",
    "set_picks": "记录选材",
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
    "keyword": "关键词", "plot": "桥段", "plots": "桥段", "structure": "结构", "structures": "模板",
    "gag": "梗", "gags": "梗", "count": "数量", "total": "总计", "storyline": "时间线",
    "outlines": "大纲", "timeline": "时间线", "archetype_id": "原型", "source": "来源",
    "id": "ID", "tweak": "微调", "pen": "笔名", "outline": "大纲",
    "url": "地址", "words": "字数", "word_count": "字数", "target_words": "目标字数",
    "passed": "通过", "score": "评分", "message": "消息", "recent_n": "最近章数",
    "chapter_num": "章节号", "max_outlines": "大纲数", "struct": "结构",
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
    if name == "world_candidates":
        try:
            obj = json.loads(text)
            cand = (obj or {}).get("candidate") or {}
            total = (obj or {}).get("total")
            head = f"已生成第 {total or '?'} 个候选"
            t = (cand.get("title") or "").strip()
            if t:
                head += f"：「{t}」"
            ol = (cand.get("one_liner") or "").strip()
            return head + (("　" + ol) if ol else "")
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return json.dumps(_zh_keys(obj), ensure_ascii=False, separators=(",", ":")).strip()
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return text[:200].strip()


def _result_candidate(msg):
    """从 world_candidates 的 tool/result 全文抽新增候选 dict（供 dsh 会话实时 add_candidate）。"""
    try:
        obj = json.loads(_extract_result_text(msg))
        cand = (obj or {}).get("candidate")
        if isinstance(cand, dict) and cand.get("title"):
            return cand
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


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
        pending[call_id] = {"name": name, "callId": call_id, "args": args}
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
        yield {"type": "tool_result",
               "name": name,
               "callId": call_id or p.get("callId") or "",
               "ok": ok,
               "summary": _zh_tool_summary(name, p.get("args"), msg)}
        # 合并工具 world_candidates 已内部「生成 1 个候选 + 自动填入」：dsh 会话 busy 期间
        # 浏览器跳过 nav-intent 轮询，这里从结果里实时合成 add_candidate SSE 渲染
        # （外部 MCP 路径由工具自身 push 的 add_candidate 意图轮询兜底，done 后清空不重放）。
        if ok and name == "world_candidates":
            cand = _result_candidate(msg)
            if cand:
                yield {"type": "ui_command", "cmd": "add_candidate",
                       "args": {"candidate": cand}}
    elif t == "llm/call":
        # 调试模式（NOVEL_AGENT_DEBUG=1 时 events-runner 才 emit）：一次 LLM 调用的
        # 提示词/MCP工具/返回JSON，前端渲染「LLM 调用」调试卡。不持久化 task_events。
        yield {"type": "llm_call",
               "seq": data.get("seq"), "turn": data.get("turn"), "step": data.get("step"),
               "request": data.get("request"), "response": data.get("response"),
               "usage": data.get("usage")}
    elif t == "reply":
        yield {"type": "reply", "content": data.get("text") or ""}
    elif t == "error":
        yield {"type": "error", "message": data.get("message") or "dsh 任务出错"}
    elif t == "done":
        yield {"type": "done"}


def run_dsh_task(task: str, history: list | None = None,
                 timeout_s: int = 900, debug: bool = False):
    """跑一次 dsh headless 任务，实时产出 SSE 事件 dict。

    事件序列（由 events-runner 的 NDJSON 流实时驱动）：tool_call / tool_result /
    navigate / ui_command …… → reply（最终回复）→ done。
    失败/异常/被打断：error + done。task 为最新用户消息，history 为浏览器持有的消息列表。

    全服务单任务：本任务启动前先 interrupt_current_task() 打断任何正在跑的 dsh；
    本任务也可被后续任务 / `/api/agent/chat/cancel` 打断（被打断则 error+done 收尾）。
    finally 里收尸（杀残留 proc + wait），避免孤儿进程。
    """
    # 全服务单任务：新任务先打断正在跑的旧任务；清空旧任务事件存储（刷新重建只反映当前任务）
    interrupt_current_task()
    clear_task_events()
    overlay = _write_runtime_overlay()
    cmd = get_dsh_argv() + [
        "--profile", get_dsh_profile(),
        "--patch", overlay, _build_task_text(task, history),
    ]

    proc = None
    saw_any = False
    saw_done_event = False
    try:
        try:
            ensure_proxy()   # 保证本地 token 代理(58082)已监听，dsh 的 LLM 调用才能走它计 token
            env = {**os.environ, "DEEPSEEK_BASE_URL": "http://127.0.0.1:58082"}
            if debug:
                # 调试模式：通知 events-runner 把每次 LLM 调用的提示词/MCP工具/返回JSON emit 成 llm/call。
                # 关闭时不注入 → 子进程不发数据，零开销。
                env["NOVEL_AGENT_DEBUG"] = "1"
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
        _set_current_proc(proc, task)

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
                    if sse.get("type") in ("tool_call", "tool_result"):
                        _append_task_event(sse)   # 持久化工具事件，供刷新后重建工具卡流
                    if sse.get("type") == "done":
                        saw_done_event = True
                    yield sse
            elif kind == "eof":
                break
            else:
                break

        exit_code = proc.wait()
        # 未以 done 收尾且退出非 0：判为被打断 / 崩溃（events-runner 正常结束必有 done 事件）
        if exit_code != 0 and not saw_done_event:
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
