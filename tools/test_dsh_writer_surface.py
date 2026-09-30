#!/usr/bin/env python3
"""Writer runtime surface regression checks (no LLM invocation)."""
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries import dsh_bridge as DB  # noqa: E402
from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402


def main() -> None:
    assert PROFILE_TOOLS["write"] == {"prepare_plot_run", "save_plot_draft",
                                        "prepare_plot_revision", "save_plot_revision"}

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

    # ── 建书两段子 run：与 writer 同样自带契约，不注入 NOVEL_AGENT.md（会被 20KB 预算截断，
    # 恰好截掉「第二部分：护栏」），并同样禁掉联网 / skill catalog / 计划模式。──
    for profile, expected in (("build", {"get_build_context", "query_arc_library", "query_plots",
                                         "validate_build", "save_build_draft"}),
                              ("build-candidates", {"navigate", "drive_ui", "get_build_status",
                                                    "query_profiles"})):
        assert PROFILE_TOOLS[profile] <= expected, (profile, PROFILE_TOOLS[profile] ^ expected)
    overlay_build = Path(DB._write_runtime_overlay(mcp_profile="build")).read_text(encoding="utf-8")
    assert "maxBytes: 0" in overlay_build
    assert "instructionFileCandidates: []" in overlay_build
    for plugin in ("tool-web", "web", "web-search-deepseek", "tool-skill", "skill",
                   "skill-filesystem", "tool-plan", "plan-mode"):
        assert f"- id: {plugin}\n  disabled: true" in overlay_build, plugin
    assert "NovelEngine Build Agent" in overlay_build
    # 非建书/写作 profile 仍拿 NOVEL_AGENT.md，但预算要装得下全文（25.3KB）——此前 20000
    # 会静默截掉尾部护栏段
    overlay_scout = Path(DB._write_runtime_overlay(mcp_profile="scout")).read_text(encoding="utf-8")
    assert "maxBytes: 30000" in overlay_scout
    assert "instructionFileCandidates: ['NOVEL_AGENT.md']" in overlay_scout

    # 建书 skill 不得提到本 profile 之外的任何工具名（否则子 run 因 unknown-tool 停摆）
    build_skill = DB._skill_text_for_profile("build")
    for banned in ("drive_ui", "get_build_status", "validate_storyline", "validate_world",
                   "save_outlines", "navigate", "get_book_detail"):
        assert banned not in build_skill, banned

    # ── 工具卡摘要必须如实：失败的 drive_ui 曾一律显示「已暂存续规划预览」 ──
    fail_msg = {"content": [{"isError": True, "content": [
        {"type": "text", "text": "Error: 命令 set_replan_preview planning_patch 校验失败：h1 必须是非空 list"}]}]}
    fail_summary = DB._zh_tool_summary("drive_ui", {"cmd": "set_replan_preview"}, fail_msg, False)
    assert fail_summary.startswith("失败：") and "已暂存" not in fail_summary, fail_summary
    ok_msg = {"content": [{"content": [{"type": "text", "text": "{}"}]}]}
    assert DB._zh_tool_summary("drive_ui", {"cmd": "set_replan_preview"}, ok_msg, True) == "已暂存续规划预览"

    print("[OK] writer overlay and write/revision contract")
    print("[OK] build/build-candidates overlay: 自带契约 + stock 工具已禁")
    print("[OK] 工具卡摘要如实：失败的 drive_ui 不再显示「已暂存」")


if __name__ == "__main__":
    main()
