"""MCP 运行实例观测。

这是启动记录而非进程存活状态：多个 MCP stdio 进程追加到同一 JSONL，
Web 端以 instance_id 区分，不存在唯一 current_profile。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_LOG = ROOT / "storage" / "mcp_sessions.jsonl"


def record_startup(*, profile: str, source: str, tools: list[str],
                  registry_tool_count: int | None = None,
                  flow_id: str = "", child_run_id: str = "",
                  host_tool_count: int | None = None) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    visible_tools = sorted(set(tools or []))
    item = {
        "instance_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "profile": profile or "legacy",
        "source": source or "mcp",
        "flow_id": flow_id or "",
        "child_run_id": child_run_id or "",
        "tools": visible_tools,
        # Keep tool_count as a compatibility alias for older UI/log consumers.
        "tool_count": len(visible_tools),
        "profile_mcp_tool_count": len(visible_tools),
        "registry_tool_count": registry_tool_count,
        # The Python MCP process cannot observe dsh's final host catalog.
        "host_tool_count": host_tool_count,
        "started_at": now,
        "last_seen": now,
        "observed": True,
    }
    try:
        SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SESSION_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return item


def read_sessions(limit: int = 100) -> list[dict]:
    try:
        lines = SESSION_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get("instance_id"):
            out.append(item)
    return list(reversed(out))


def status(*, profile_tools: dict[str, set[str]], limit: int = 100,
           registry_tool_count: int | None = None,
           flow_id: str = "", child_run_id: str = "",
           mcp_instance_id: str = "") -> dict:
    sessions = read_sessions(limit)
    selectors = {
        "flow_id": flow_id or "",
        "child_run_id": child_run_id or "",
        "instance_id": mcp_instance_id or "",
    }
    has_selector = any(selectors.values())

    def matches(item: dict) -> bool:
        return all(not value or item.get(key) == value
                   for key, value in selectors.items())

    # A task-specific query never falls back to another instance. Without an
    # identity there is no active instance; callers can inspect the history.
    dsh = next((x for x in sessions
                if x.get("source") == "dsh" and has_selector and matches(x)), None)
    active = {
        "instance_id": dsh.get("instance_id") if dsh else None,
        "flow_id": dsh.get("flow_id") if dsh else None,
        "child_run_id": dsh.get("child_run_id") if dsh else None,
        "profile": dsh.get("profile") if dsh else None,
        "tools": dsh.get("tools", []) if dsh else [],
        "registry_tool_count": dsh.get("registry_tool_count") if dsh else None,
        "profile_mcp_tool_count": dsh.get("profile_mcp_tool_count",
                                         dsh.get("tool_count")) if dsh else None,
        "host_tool_count": dsh.get("host_tool_count") if dsh else None,
        "last_seen": dsh.get("last_seen") if dsh else None,
        "matched": bool(dsh),
    }
    return {
        "registry_tool_count": registry_tool_count,
        "configured_profiles": {
            name: len(tools) for name, tools in sorted(profile_tools.items())
        },
        "selectors": selectors,
        "active": active,
        # Compatibility summary for callers that do not yet pass identity.
        "dsh": {
            "last_profile": dsh.get("profile") if dsh else None,
            "last_seen": dsh.get("last_seen") if dsh else None,
            "tool_count": dsh.get("tool_count") if dsh else 0,
        },
        "observed_mcp_instances": sessions,
    }
