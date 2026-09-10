"""纯规则 MCP 工具 profile 与运行时前置条件。"""
from __future__ import annotations

import os


PROFILE_TOOLS = {
    "build-candidates": {"navigate", "drive_ui", "get_build_status", "query_profiles"},
    "build": {"get_build_status", "drive_ui", "query_arc_library", "query_plots",
              "query_gags", "query_characters", "validate_storyline", "validate_world",
              "navigate", "get_book_detail"},
    # Writer 是一次性 Plot 生成器：其余流程由服务端 FSM 负责。
    "write": {"prepare_plot_run", "save_plot_draft"},
    "replan": {"get_story_state", "save_outlines", "validate_storyline", "validate_world",
               "query_arc_library", "query_plots", "drive_ui"},
    "publish": {"get_book_detail", "save_book_meta", "publish_check", "publish_book",
                 "mark_finished", "export_book"},
    "scout": {"drive_ui", "fetch_book", "fetch_novel", "fetch_webnovel", "discover_hot",
              "list_rankings", "list_crawled_novels", "read_crawled_novel", "extract_state",
              "judge_extraction", "ingest_library_assets",
              "query_plots", "query_arc_library", "query_gags", "query_characters"},
    "style": {"query_profiles", "get_pen_style", "add_style_rule", "delete_style_rule",
              "add_style_sample", "delete_style_sample", "list_style_samples", "get_style_sample"},
    "inspect": {"list_books", "get_book_detail", "get_book_state", "get_build_status",
                "get_storyline", "get_story_state", "validate_storyline", "validate_world"},
}

UI_COMMAND_POLICY = {
    "set_field": {"profiles": {"build-candidates"}},
    "add_candidate": {"profiles": {"build-candidates"}},
    "pick_candidate": {"profiles": {"build-candidates"}},
    "set_world": {"profiles": {"build"}},
    "set_characters": {"profiles": {"build"}},
    "set_outline": {"profiles": {"build"}},
    "set_picks": {"profiles": {"build"}},
    "set_review": {"profiles": {"scout"}},
    "set_replan_preview": {"profiles": {"replan"}},
    "submit": {"actor": "user_only"},
}


def selected_profile(argv=None) -> str:
    argv = list(argv or [])
    if "--profile" in argv:
        i = argv.index("--profile")
        return argv[i + 1] if i + 1 < len(argv) else ""
    return os.environ.get("NOVEL_MCP_PROFILE", "").strip()


def filter_registry(registry: list[dict], profile: str) -> list[dict]:
    if not profile:
        return registry
    if profile not in PROFILE_TOOLS:
        raise ValueError(f"未知 MCP profile: {profile}")
    allowed = PROFILE_TOOLS[profile]
    return [entry for entry in registry if entry["name"] in allowed]


def resolve_profile(profile: str, *, book_exists: bool = False, storyline_exists: bool = False,
                    pen_selected: bool = False) -> dict:
    allowed = set(PROFILE_TOOLS.get(profile) or set())
    reasons = []
    if profile == "build" and not book_exists:
        allowed.discard("arc_material_candidates")
        reasons.append("NO_BOOK_ID")
    if pen_selected:
        allowed.discard("query_profiles")
        reasons.append("PEN_ALREADY_SELECTED")
    return {"profile": profile, "allowed_tools": sorted(allowed), "reason_codes": reasons,
            "book_exists": book_exists, "storyline_exists": storyline_exists}


def check_ui_command(cmd: str, profile: str = "") -> None:
    policy = UI_COMMAND_POLICY.get(cmd) or {}
    if policy.get("actor") == "user_only":
        raise RuntimeError("ui_command_forbidden: submit 只能由用户在向导中点击，Agent 不得调用")
    profiles = policy.get("profiles")
    if profile and profiles and profile not in profiles:
        raise RuntimeError(f"ui_command_forbidden: {cmd} 不属于 profile={profile}")


def manifest(registry: list[dict]) -> list[dict]:
    return [{k: v for k, v in entry.items() if k != "func"} for entry in registry]


def tool_metadata(name: str, *, allowed_phases=None, locked: bool = False) -> dict:
    profiles = sorted(profile for profile, names in PROFILE_TOOLS.items() if name in names)
    write_prefixes = ("save_", "add_", "delete_", "publish", "mark_", "ingest_", "fetch_", "drive_ui")
    read_write = "write" if name.startswith(write_prefixes) else "read"
    requires_book = name in {
        "get_book_state", "prepare_plot_run", "get_storyline", "get_story_state", "get_book_detail",
        "save_basic_info", "save_outlines", "save_book_meta", "arc_material_candidates",
        "save_plot_draft", "save_chapter_text", "publish_check", "mark_finished", "publish_book",
        "export_book", "chapter_quality_gate", "pick_plot_sample",
    }
    return {
        "profiles": profiles,
        "read_write": read_write,
        "requires_book": requires_book,
        "requires_storyline": requires_book and name not in {"get_book_detail"},
        "allowed_phases": sorted(allowed_phases or []),
        "confirmation": "subcommand_policy" if name == "drive_ui" else "none",
        "lock": bool(locked),
    }
