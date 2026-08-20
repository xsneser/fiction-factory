"""Agent 聊天端点 — 侧栏对话面板的后端（SSE 流式，无状态）。

浏览器持有 user/assistant 消息历史，POST /api/agent/chat 全量带上。
后端按根 config.json 的 agent.driver 分发：
  - builtin（默认）：跑内置 function calling 循环（plugins/agent_loop.py）；
  - dsh：转发到 dsh headless 一次性子进程（libraries/dsh_bridge.py，经 MCP 驱动平台）。
两种 driver 的 SSE 事件协议兼容（reply/done/error/navigate/canvas/tool_start/tool_result）。
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, request, jsonify  # noqa: E402
from .ctx import sse_stream_response  # noqa: E402
from plugins.agent_loop import run_agent_loop, get_tool_log, clear_tool_log  # noqa: E402
from agent_tools import TOOL_REGISTRY  # noqa: E402
from libraries.nav_intent import take_nav_intents  # noqa: E402
from libraries.dsh_bridge import run_dsh_task  # noqa: E402
from core.json_store import read_json  # noqa: E402

bp = Blueprint("agent", __name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _agent_driver() -> str:
    """当前聊天大脑 driver（config.json agent.driver，默认 builtin）。"""
    try:
        cfg = read_json(os.path.join(_ROOT, "config.json"), {}) or {}
        return (cfg.get("agent") or {}).get("driver") or "builtin"
    except Exception:
        return "builtin"


@bp.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """侧栏 Agent 对话。body: {"messages": [{"role": "user"|"assistant", "content": "..."}]}。

    返回 SSE 事件：tool_start / tool_result / reply / navigate / canvas / error / done。
    driver=dsh 时最后一条 user 消息为任务、其余为历史（转发 dsh headless）。
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
            if _agent_driver() == "dsh":
                # 最后一条 user 消息作为当前任务，其余作为历史（多轮语义）
                task, history = "", list(messages)
                if history and history[-1].get("role") == "user":
                    last = history.pop(-1)
                    task = last.get("content", "")
                for evt in run_dsh_task(task, history):
                    yield emit(evt)
            else:
                for line in run_agent_loop(messages, emit):
                    yield line
        except Exception as e:
            import traceback
            yield emit({"type": "error", "message": f"{e}\n{traceback.format_exc()}"})
            yield emit({"type": "done"})

    return sse_stream_response(generate())


@bp.route("/api/agent/driver", methods=["GET"])
def agent_driver():
    """当前聊天大脑 driver（builtin / dsh），供前端调试展示。"""
    return jsonify({"ok": True, "driver": _agent_driver()})


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


@bp.route("/api/agent/nav-intents", methods=["GET"])
def agent_nav_intents():
    """navigate 外部驱动桥：浏览器轮询消费外部（MCP）写入的跳转意图（取后即清空）。"""
    return jsonify({"ok": True, "intents": take_nav_intents()})
