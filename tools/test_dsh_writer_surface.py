#!/usr/bin/env python3
"""Writer runtime surface regression checks (no LLM invocation)."""
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries import dsh_bridge as DB  # noqa: E402
from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402


def main() -> None:
    assert PROFILE_TOOLS["write"] == {"prepare_plot_run", "save_plot_draft"}

    overlay_path = Path(DB._write_runtime_overlay(mcp_profile="write"))
    overlay = overlay_path.read_text(encoding="utf-8")

    assert "maxBytes: 0" in overlay
    assert "instructionFileCandidates: []" in overlay
    assert "includeHarnessIdentity: false" in overlay
    assert "includeRuntimeContext: false" in overlay
    assert "[当前 Skill，必须遵守]" not in overlay
    for plugin in (
        "tool-web",
        "web",
        "web-search-deepseek",
        "tool-skill",
        "skill",
        "skill-filesystem",
        "tool-plan",
        "plan-mode",
    ):
        assert f"- id: {plugin}\n  disabled: true" in overlay, plugin

    skill = DB._skill_text_for_profile("write")
    assert "NOVEL_AGENT.md" not in skill
    assert "save_chapter_text" not in skill
    assert "chapter_quality_gate" not in skill

    mapped = list(DB._map_dsh_event({
        "type": "llm/call",
        "data": {
            "request": {"tools": [{"name": "prepare_plot_run"}]},
            "input_budget": {"total_chars": 100},
        },
    }, {}))
    assert mapped[0]["host_tool_count"] == 1
    assert mapped[0]["input_budget"]["total_chars"] == 100

    print("[OK] writer overlay and two-tool contract")


if __name__ == "__main__":
    main()
