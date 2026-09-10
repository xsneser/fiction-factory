#!/usr/bin/env python3
"""MCP instance identity/metric observation tests."""
import json
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries import mcp_runtime  # noqa: E402
from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402


def main() -> None:
    original = mcp_runtime.SESSION_LOG
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mcp_runtime.SESSION_LOG = Path(tmp) / "sessions.jsonl"
            first = mcp_runtime.record_startup(
                profile="write",
                source="dsh",
                tools=sorted(PROFILE_TOOLS["write"]),
                registry_tool_count=45,
                flow_id="flow-a",
                child_run_id="child-a",
            )
            mcp_runtime.record_startup(
                profile="scout",
                source="dsh",
                tools=["fetch_book"],
                registry_tool_count=45,
                flow_id="flow-b",
                child_run_id="child-b",
            )

            selected = mcp_runtime.status(
                profile_tools=PROFILE_TOOLS,
                registry_tool_count=45,
                flow_id="flow-a",
                child_run_id="child-a",
            )
            active = selected["active"]
            assert active["matched"] is True
            assert active["instance_id"] == first["instance_id"]
            assert active["profile"] == "write"
            assert active["profile_mcp_tool_count"] == 2
            assert active["registry_tool_count"] == 45
            assert active["host_tool_count"] is None

            missing = mcp_runtime.status(
                profile_tools=PROFILE_TOOLS,
                registry_tool_count=45,
                flow_id="flow-missing",
            )
            assert missing["active"]["matched"] is False
            assert missing["active"]["profile"] is None

            unscoped = mcp_runtime.status(
                profile_tools=PROFILE_TOOLS,
                registry_tool_count=45,
            )
            assert unscoped["active"]["matched"] is False
            assert len(unscoped["observed_mcp_instances"]) == 2

    finally:
        mcp_runtime.SESSION_LOG = original

    print("[OK] MCP instance identity and metric observation")


if __name__ == "__main__":
    main()
