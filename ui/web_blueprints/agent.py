"""Agent 聊天端点 — 侧栏对话面板的后端（SSE 流式，无状态）。

浏览器持有 user/assistant 消息历史，POST /api/agent/chat 全量带上；
后端跑 function calling 循环（plugins/agent_loop.py），逐事件流式返回。
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, request, jsonify  # noqa: E402
from .ctx import sse_stream_response  # noqa: E402
from plugins.agent_loop import run_agent_loop, get_tool_log, clear_tool_log  # noqa: E402
from agent_tools import TOOL_REGISTRY  # noqa: E402

bp = Blueprint("agent", __name__)


@bp.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """侧栏 Agent 对话。body: {"messages": [{"role": "user"|"assistant", "content": "..."}]}。

    返回 SSE 事件：tool_start / tool_result / reply / navigate / canvas / error / done。
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
            for line in run_agent_loop(messages, emit):
                yield line
        except Exception as e:
            import traceback
            yield emit({"type": "error", "message": f"{e}\n{traceback.format_exc()}"})
            yield emit({"type": "done"})

    return sse_stream_response(generate())


@bp.route("/api/agent/tool-log", methods=["GET"])
def agent_tool_log():
    """右侧面板「工具日志」页签数据：所有暴露工具数 + 本次会话工具调用汇总与时间线。"""
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
