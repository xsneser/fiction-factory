"""Agent 聊天端点 — 侧栏对话面板的后端（SSE 流式，无状态）。

浏览器持有 user/assistant 消息历史，POST /api/agent/chat 全量带上；
后端转发到 dsh headless 一次性子进程（libraries/dsh_bridge.py，经 MCP 驱动平台）。
内置 agent（plugins/agent_loop.py）已删除，dsh 是唯一大脑。

SSE 事件协议：tool_start / reply / error / done（dsh 桥）；navigate 等由
nav-intent 意图队列经浏览器 2.5s 轮询消费，不走 SSE。
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, request, jsonify  # noqa: E402
from .ctx import sse_stream_response  # noqa: E402
from agent_tools import TOOL_REGISTRY  # noqa: E402
from libraries.nav_intent import take_nav_intents  # noqa: E402
from libraries.dsh_bridge import run_dsh_flow, interrupt_current_task, get_current_task_status, _DEBUG_PROMPT_DIR  # noqa: E402
from libraries.build_status import set_build_status  # noqa: E402
from libraries.tool_log import get_tool_log, clear_tool_log  # noqa: E402
from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402
from libraries.mcp_runtime import status as mcp_runtime_status  # noqa: E402

bp = Blueprint("agent", __name__)


@bp.route("/api/agent/mcp-status", methods=["GET"])
def agent_mcp_status():
    """按当前任务身份返回 MCP 观测，避免把并发任务的实例当成当前 profile。"""
    task = get_current_task_status()
    flow_id = request.args.get("flow_id", "") or task.get("flow_id", "")
    child_run_id = request.args.get("child_run_id", "") or task.get("child_run_id", "")
    instance_id = request.args.get("mcp_instance_id", "")
    return jsonify({
        "ok": True,
        **mcp_runtime_status(
            profile_tools=PROFILE_TOOLS,
            registry_tool_count=len(TOOL_REGISTRY),
            flow_id=flow_id,
            child_run_id=child_run_id,
            mcp_instance_id=instance_id,
        ),
    })


@bp.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """侧栏 Agent 对话。body: {"messages": [{"role": "user"|"assistant", "content": "..."}]}。

    返回 SSE 事件：tool_start / reply / error / done。
    最后一条 user 消息作为当前任务、其余作为历史（多轮语义），转发 dsh headless。
    """
    data = request.get_json(silent=True) or {}
    raw_messages = data.get("messages") or []
    debug = bool(data.get("debug"))   # 调试模式：前端 🔍 开关，透传给 dsh 子进程 emit llm/call
    messages = []
    for m in raw_messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    def emit(evt):
        return "data: " + json.dumps(evt, ensure_ascii=False) + "\n\n"

    def generate():
        try:
            task, history = "", list(messages)
            if history and history[-1].get("role") == "user":
                last = history.pop(-1)
                task = last.get("content", "")
            for evt in run_dsh_flow(task, history, debug=debug):
                yield emit(evt)
        except Exception as e:
            import traceback
            yield emit({"type": "error", "message": f"{e}\n{traceback.format_exc()}"})
            yield emit({"type": "done"})

    return sse_stream_response(generate())


@bp.route("/api/agent/chat/cancel", methods=["POST"])
def agent_chat_cancel():
    """打断当前正在跑的 dsh 任务（全服务单任务；无任务也返回 ok，幂等）。"""
    interrupted = interrupt_current_task()
    return jsonify({"ok": True, "interrupted": interrupted})


@bp.route("/api/agent/chat/status", methods=["GET"])
def agent_chat_status():
    """当前是否有 dsh 任务在跑（前端切页/刷新后恢复感知用）。

    返回 {running, started_at, task, pid}；运行中给展示信息，未运行 running=False。
    """
    return jsonify({"ok": True, **get_current_task_status()})


@bp.route("/api/agent/task-events", methods=["GET"])
def agent_task_events():
    """刷新后重建工具卡流：读取 dsh 工具事件（ts >= since，unix 秒）。

    侧栏 dsh 的工具调用落盘 task_events.jsonl（source=dsh 不进 tool_log）；
    前端刷新后按任务 started_at 拉取，重建「刷新前」的工具卡流。
    """
    since = float(request.args.get("since", "0") or 0)
    from libraries.dsh_bridge import get_task_events
    return jsonify({"ok": True, "events": get_task_events(since)})


@bp.route("/api/agent/task-events/clear", methods=["POST"])
def agent_task_events_clear():
    """清空 dsh 工具事件存储（前端「清空对话」时调用）。"""
    from libraries.dsh_bridge import clear_task_events
    clear_task_events()
    return jsonify({"ok": True})


@bp.route("/api/agent/token-usage", methods=["GET"])
def agent_token_usage():
    """实时 token 流量（本地 API 代理检测器累计；前端 2s 轮询）。"""
    from libraries.token_proxy import ensure_proxy, get_token_usage
    ensure_proxy()
    return jsonify({"ok": True, **get_token_usage()})


@bp.route("/api/agent/token-usage/clear", methods=["POST"])
def agent_token_usage_clear():
    """清零 token 流量累计（新任务/清空对话时调用）。"""
    from libraries.token_proxy import clear_token_usage
    clear_token_usage()
    return jsonify({"ok": True})


@bp.route("/api/agent/build-status", methods=["POST"])
def agent_build_status():
    """浏览器上报建书向导状态（WZ.reportStatus），写入 storage/build_status.json 供 MCP 工具读取。

    ⚠️ 这里写的是 **UI 现状快照**（向导当前页、submit_error 原文），**不是阶段权威**——
    阶段权威在 `libraries/build_draft`（服务端 canonical 记录，只由 /api/build/transition
    原子推进）。2026-09-10 的事故正是把本快照当阶段判据：前端先发 agent 任务、后上报
    `cur=3`，服务端读到 `cur=2` 便起了 build-candidates profile（工具面里没有
    set_world/set_outline），模型整轮只能做平台错误恢复。
    """
    data = request.get_json(silent=True) or {}
    set_build_status(data)
    return jsonify({"ok": True})


@bp.route("/api/build/transition", methods=["POST"])
def build_transition():
    """建书阶段转场（**原子**）——阶段权威的唯一写入口。

    body: {build_session_id, step, selected_candidate?, idea?, tags?, pen_name?}
    返回: {ok, step, revision}

    前端必须在拿到本端点成功响应**之后**才允许派发 agent 任务：状态没落服务端就启动
    依赖它的模型，是「先发任务后上报」事故的根源，宁可让用户多等几十毫秒。
    """
    from libraries import build_draft
    data = request.get_json(silent=True) or {}
    sid = str(data.get("build_session_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "build_session_id 缺失"}), 400
    step = data.get("step")
    if step is None:
        return jsonify({"ok": False, "error": "step 缺失"}), 400
    try:
        step = int(step)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": f"step 非法: {data.get('step')!r}"}), 400

    # 只透传请求里**确实给了**的字段，避免用 None 覆盖已有值
    fields = {}
    for key in ("idea", "tags", "pen_name"):
        if data.get(key) is not None:
            fields[key] = data[key]
    cand = data.get("selected_candidate")
    if isinstance(cand, dict) and cand.get("title"):
        # 服务端已有的选择优先（/api/build/pick 才是选择的正式入口）——
        # 浏览器带的只是「记录还没落过时」的补位，不作权威
        fields["selected_candidate"] = (build_draft.load(sid).get("selected_candidate")
                                        or cand)
    try:
        rec = build_draft.transition(sid, step=step, **fields)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "step": rec["step"], "revision": rec["revision"]})


@bp.route("/api/build/pick", methods=["POST"])
def build_pick():
    """选定候选方向（服务端持久化）——用户点候选卡时调用。

    body: {build_session_id, candidate?: {title, one_liner?, world_brief?}, idx?: int}
    `idx` 从 canonical 记录里的候选列表解析（候选由 add_candidate/set_candidates 落盘），
    因此浏览器不再是「用户选了什么」的事实源。选择**不**推进步号（推进走 transition）。
    """
    from libraries import build_draft
    data = request.get_json(silent=True) or {}
    sid = str(data.get("build_session_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "build_session_id 缺失"}), 400
    rec = build_draft.load(sid)
    cand = data.get("candidate")
    if not (isinstance(cand, dict) and cand.get("title")):
        cand = None
        idx = data.get("idx")
        if idx is not None:
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"idx 非法: {data.get('idx')!r}"}), 400
            cands = rec.get("candidates") or []
            if not (0 <= idx < len(cands)):
                return jsonify({"ok": False, "error":
                                f"idx 越界: {idx}（服务端已有 {len(cands)} 张候选）"}), 400
            cand = cands[idx]
    if not cand:
        return jsonify({"ok": False, "error": "需 candidate 或 idx 之一"}), 400
    # 服务端已落盘的候选副本优先（add_candidate/set_candidates 存的那份）：浏览器传来的
    # 只是「哪一张被点了」，快照内容以服务端为准，避免表单/窗口状态改动污染选择事实。
    stored = {c.get("title"): c for c in (rec.get("candidates") or [])}
    cand = stored.get(cand.get("title")) or cand
    rec = build_draft.update(sid, selected_candidate=cand)
    return jsonify({"ok": True, "revision": rec["revision"],
                    "selected_candidate": rec["selected_candidate"]})


@bp.route("/api/agent/tool-log", methods=["GET"])
def agent_tool_log():
    """工具日志与分层工具计数；当前实例按 flow/child identity 绑定。"""
    log = get_tool_log()
    success = sum(1 for x in log if x.get("ok"))
    task = get_current_task_status()
    runtime = mcp_runtime_status(
        profile_tools=PROFILE_TOOLS,
        registry_tool_count=len(TOOL_REGISTRY),
        flow_id=task.get("flow_id", ""),
        child_run_id=task.get("child_run_id", ""),
    )
    active = runtime.get("active", {})
    matched = bool(active.get("matched"))
    host_tool_count = active.get("host_tool_count") if matched else None
    if host_tool_count is None and task.get("running"):
        # Debug llm_call events carry the final dsh host catalog. Match the
        # same task identity; never borrow a concurrent flow's observation.
        from libraries.dsh_bridge import get_task_events
        for event in reversed(get_task_events(float(task.get("started_at") or 0))):
            if event.get("type") != "llm_call":
                continue
            if task.get("flow_id") and event.get("flow_id") != task.get("flow_id"):
                continue
            if task.get("child_run_id") and event.get("child_run_id") != task.get("child_run_id"):
                continue
            if isinstance(event.get("host_tool_count"), int):
                host_tool_count = event["host_tool_count"]
                break
    return jsonify({
        "ok": True,
        # Deprecated compatibility field: it is the matched profile count,
        # never the global registry count. Use the explicit fields below.
        "tools_exposed": active.get("profile_mcp_tool_count") if matched else None,
        "registry_tool_count": runtime.get("registry_tool_count"),
        "profile_mcp_tool_count": active.get("profile_mcp_tool_count") if matched else None,
        "host_tool_count": host_tool_count,
        "active_profile": active.get("profile") if matched else None,
        "active_instance_id": active.get("instance_id") if matched else None,
        "active_flow_id": active.get("flow_id") if matched else None,
        "active_child_run_id": active.get("child_run_id") if matched else None,
        "total": len(log),
        "success": success,
        "failed": len(log) - success,
        "log": log,
    })


@bp.route("/api/agent/tool-log/clear", methods=["POST"])
def agent_tool_log_clear():
    clear_tool_log()
    return jsonify({"ok": True, "total": 0})


@bp.route("/api/agent/nav-intents", methods=["GET"])
def agent_nav_intents():
    """navigate 外部驱动桥：浏览器轮询消费外部（MCP）写入的跳转意图（取后即清空）。"""
    return jsonify({"ok": True, "intents": take_nav_intents()})


@bp.route("/api/agent/debug-prompt/<seq>", methods=["GET"])
def agent_debug_prompt(seq):
    """调试卡「提示词/返回JSON」按需拉完整原文（SSE 事件里只有裁剪预览，完整载荷在 storage/debug-prompts/<seq>.json）。

    seq 数字白名单防路径穿越；文件不存在（新任务已清空/未写入）返回 404，前端保持裁剪预览。
    """
    if not seq.isdigit():
        return jsonify({"ok": False, "error": "bad seq"}), 400
    try:
        with open(os.path.join(_DEBUG_PROMPT_DIR, f"{seq}.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, **data})
