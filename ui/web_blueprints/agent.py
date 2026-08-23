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
from libraries.dsh_bridge import run_dsh_task, interrupt_current_task, get_current_task_status  # noqa: E402
from libraries.build_status import set_build_status  # noqa: E402
from libraries.tool_log import get_tool_log, clear_tool_log  # noqa: E402

bp = Blueprint("agent", __name__)


@bp.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """侧栏 Agent 对话。body: {"messages": [{"role": "user"|"assistant", "content": "..."}]}。

    返回 SSE 事件：tool_start / reply / error / done。
    最后一条 user 消息作为当前任务、其余作为历史（多轮语义），转发 dsh headless。
    """
    data = request.get_json(silent=True) or {}
    raw_messages = data.get("messages") or []
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
            for evt in run_dsh_task(task, history):
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


@bp.route("/api/agent/build-status", methods=["POST"])
def agent_build_status():
    """浏览器上报建书向导状态（WZ.reportStatus），写入 storage/build_status.json 供 MCP 工具读取。"""
    data = request.get_json(silent=True) or {}
    set_build_status(data)
    return jsonify({"ok": True})


@bp.route("/api/agent/tool-log", methods=["GET"])
def agent_tool_log():
    """右侧面板「工具日志」页签数据：所有暴露工具数 + 工具调用汇总与时间线。"""
    log = get_tool_log()
    success = sum(1 for x in log if x.get("ok"))
    return jsonify({
        "ok": True,
        "tools_exposed": len(TOOL_REGISTRY),
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
