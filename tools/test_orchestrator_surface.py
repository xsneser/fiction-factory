#!/usr/bin/env python3
"""Main-agent orchestration surface checks without an LLM run."""
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402
from libraries import dsh_bridge as DB  # noqa: E402


def main() -> None:
    assert "orchestrate" in PROFILE_TOOLS
    assert PROFILE_TOOLS["critic"] == {"get_plot_review_context"}
    assert {"prepare_plot_revision", "save_plot_revision"} <= PROFILE_TOOLS["write"]

    parts = DB._mcp_tool_parts("mcp__novelengine-write__prepare_plot_run")
    assert parts == ("novelengine-write", "prepare_plot_run",
                     "mcp__novelengine-write__prepare_plot_run")

    pending = {}
    calls = list(DB._map_dsh_event({
        "type": "tool/call",
        "data": {"name": "mcp__novelengine-write__prepare_plot_run",
                 "callId": "c1", "sessionId": "child-1", "arguments": "{}"},
    }, pending))
    assert calls[0]["name"] == "prepare_plot_run"
    assert calls[0]["server_name"] == "novelengine-write"
    results = list(DB._map_dsh_event({
        "type": "tool/result",
        "data": {"sessionId": "child-1",
                 "message": {"source": {"callId": "c1"},
                             "content": [{"type": "text", "text": "{}"}]}},
    }, pending))
    assert results[0]["session_id"] == "child-1"
    assert results[0]["name"] == "prepare_plot_run"

    overlay = Path(DB._write_runtime_overlay(mcp_profile="orchestrate")).read_text(encoding="utf-8")
    for needle in ("mcp-novelengine-orch", "serverName: novelengine-write",
                   "mcp-novelengine-critic", "subagent\n  disabled: false"):
        assert needle in overlay, needle
    # 编排 profile 注入评审门禁；legacy/叶子 profile 不注入（否则一次性 Writer 无人 accept 会锁死）
    assert DB._needs_review_gate("orchestrate") is True
    for leaf in ("write", "replan", "build", "publish", "scout", ""):
        assert DB._needs_review_gate(leaf) is False, leaf

    # 子代理生命周期是 cordis 事件（不是 session/event），必须单独映射且带子会话 id
    started = list(DB._map_dsh_event({
        "type": "subagent/start",
        "data": {"runId": "r1", "provider": "spawn", "sessionId": "child-9", "local": True},
    }, {}))
    assert started[0]["type"] == "subagent_start", started
    assert started[0]["session_id"] == "child-9", started
    ended = list(DB._map_dsh_event({
        "type": "subagent/end",
        "data": {"runId": "r1", "provider": "spawn", "sessionId": "child-9",
                 "stopReason": "completed", "lastAssistantText": "已保存"},
    }, {}))
    assert ended[0]["type"] == "subagent_end" and ended[0]["stop_reason"] == "completed", ended

    print("[OK] orchestrator profiles, namespaced events, review gate scope, subagent lifecycle")


if __name__ == "__main__":
    main()
