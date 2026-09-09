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


def record_startup(*, profile: str, source: str, tools: list[str]) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    item = {
        "instance_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "profile": profile or "legacy",
        "source": source or "mcp",
        "tools": sorted(set(tools or [])),
        "tool_count": len(set(tools or [])),
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


def status(*, profile_tools: dict[str, set[str]], limit: int = 100) -> dict:
    sessions = read_sessions(limit)
    dsh = next((x for x in sessions if x.get("source") == "dsh"), None)
    return {
        "configured_profiles": {
            name: len(tools) for name, tools in sorted(profile_tools.items())
        },
        "dsh": {
            "last_profile": dsh.get("profile") if dsh else None,
            "last_seen": dsh.get("last_seen") if dsh else None,
            "tool_count": dsh.get("tool_count") if dsh else 0,
        },
        "observed_mcp_instances": sessions,
    }
