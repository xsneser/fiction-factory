"""纯规则 MCP 工具 profile 与运行时前置条件。"""
from __future__ import annotations

import os


PROFILE_TOOLS = {
    "build-candidates": {"navigate", "drive_ui", "get_build_status", "query_profiles"},
    # 建书步 3 = 5 个薄工具（工具面即能力边界）：读权威上下文 → 查素材 → 校草稿 → 落草稿。
    # 移除 drive_ui / get_build_status / validate_storyline / validate_world / navigate /
    # get_book_detail / query_gags / query_characters：
    #   · drive_ui 是「可见命令面 >> 可执行面」的唯一来源（2026-09-10 事故的直接原因）；
    #   · validate_* 已由 validate_build 聚合（agent 不必自己决定先校世界还是先校故事线）；
    #   · 本阶段的契约是「填完 → 汇报 → 停 → 等用户提交」，提交后的相位属下一阶段 orchestrator；
    #   · query_characters 先不给：人物按势力与剧情自主设计（要复用角色原型库再一行加回）。
    "build": {"get_build_context", "query_arc_library", "query_plots",
              "validate_build", "save_build_draft"},
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
    "set_tags": {"profiles": {"build-candidates"}},
    "set_candidates": {"profiles": {"build-candidates"}},
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

# 无 UI_COMMAND_POLICY 条目的 drive_ui 命令 = **对所有 profile 开放**（纯步进/清空，
# 不写建书事实）。与 `check_ui_command` 同源：那里只在 policy 带 profiles 时才拦，
# 没有条目就不拦。**新增向导命令时若不进 policy，就必须登记到这里**——
# tools/test_skill_profile_contract.py 有暴力比对断言（`allowed_ui_commands` vs
# `check_ui_command` 逐命令判定），漏登记会当场失败。
FREE_UI_COMMANDS = {"next", "prev", "reset", "load_candidates", "skip_candidates", "fill_world"}


def allowed_ui_commands(profile: str) -> set:
    """该 profile **真正可执行**的 drive_ui 命令集（能力边界的唯一真源）。

    `mcp_server` 用它裁剪 drive_ui 的可见 schema（description + cmd enum），
    `check_ui_command` 用它判执行。两者必须同源——否则又会出现
    「模型看得见 set_world、调了却 ui_command_forbidden」那种平台错误恢复
    （2026-09-10 实测：模型把整轮预算花在试工具上）。
    """
    out = set(FREE_UI_COMMANDS)
    for cmd, policy in UI_COMMAND_POLICY.items():
        if policy.get("actor") == "user_only":   # submit：任何 profile 都不给 agent
            continue
        profiles = policy.get("profiles")
        if not profiles or not profile or profile in profiles:
            out.add(cmd)
    return out


def filter_drive_ui_doc(doc: str, allowed: set, profile: str) -> str:
    """只保留本 profile 允许命令的条目（含其缩进续行），头部标出本 profile 的命令集。

    `submit` 条目例外地**始终保留**：它是禁止性说明（不是能力），删掉反而少了护栏。
    放在本模块（而非 mcp_server）是为了让契约测试能离线调用——import mcp_server 会
    触发 FastMCP 注册与 startup 记录等副作用。
    """
    import re
    lines = (doc or "").splitlines()
    # 原 docstring 头两行是「全量命令清单」（set_field/set_tags/.../submit 一串）——
    # 必须整段丢掉：留着就等于把越界命令又写回描述里。功能说明用下面这段固定文案。
    head = ["驱动「启动新书」向导 UI（命令桥）：把命令写入意图队列，浏览器每 ~2.5s 轮询消费。",
            "建书必须走系统向导，**提交只能由用户在页面点击**（护栏：无直建工具）。"]
    bullets, cur = [], None
    for ln in lines:
        if ln.strip().startswith("- "):
            if cur:
                bullets.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur:
        bullets.append(cur)
    kept = []
    for b in bullets:
        names = re.findall(r"[A-Za-z_]+", b[0].strip()[2:].split(":", 1)[0])
        if not names or any(n in allowed or n == "submit" for n in names):
            b = [b[0].lstrip()] + b[1:]   # 去条目首行缩进（续行保留悬挂缩进）
            kept.append(b)
    scope = ("本 profile（%s）可用的 cmd：%s。\n"
             "**其它命令不在本 profile 的工具面内**（调用会被拒），不要尝试、也不要据此"
             "推断阶段；阶段由服务端决定。\n\n" % (profile or "legacy", "、".join(sorted(allowed))))
    return scope + "\n".join(head + [ln for b in kept for ln in b]).strip()


def make_drive_ui_for_profile(profile: str, base):
    """返回 (按 profile 裁剪签名/描述的 drive_ui 包装, description)。

    `cmd` 的类型注解在**运行时**用 `Literal[tuple(cmds)]` 收成真枚举（`Literal` 会
    展平元组），pydantic/FastMCP 据此生成 `{"enum": [...]}` —— 于是本 profile 之外的
    命令在 schema 层**不可表达**，而不是等调用时才被服务端拒绝。
    `base` 由调用方传入（agent_tools 的 drive_ui），避免本模块反向依赖 agent_tools。
    """
    from typing import Literal
    allowed = allowed_ui_commands(profile)
    if base is None or not allowed or not profile:
        return base, (getattr(base, "__doc__", "") or "")   # legacy/未知 profile：维持旧行为

    Cmd = Literal[tuple(sorted(allowed))]

    def drive_ui(cmd: Cmd, args: dict = None) -> dict:
        return base(cmd=cmd, args=args)

    drive_ui.__name__ = "drive_ui"
    drive_ui.__doc__ = filter_drive_ui_doc(base.__doc__ or "", allowed, profile)
    # 显式写回注解对象（若环境开了 PEP 563，字符串注解无法解析到局部 Cmd）
    drive_ui.__annotations__ = {"cmd": Cmd, "args": dict, "return": dict}
    return drive_ui, drive_ui.__doc__


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
    # 注：此前这里在「无书」时 discard("arc_material_candidates")，但该工具不在任何
    # PROFILE_TOOLS 里（build 用 query_arc_library），那段是死代码，已删。
    if pen_selected:
        allowed.discard("query_profiles")
        reasons.append("PEN_ALREADY_SELECTED")
    return {"profile": profile, "allowed_tools": sorted(allowed), "reason_codes": reasons,
            "book_exists": book_exists, "storyline_exists": storyline_exists}


def check_ui_command(cmd: str, profile: str = "") -> None:
    """命令级护栏。报错必须**可行动**——带上当前 profile 的合法命令集。

    此前只回一句「不属于 profile=X」，模型只能靠一次次试探去猜边界（2026-09-10 实测
    它把整轮预算花在这种恢复上）。正常路径下模型根本看不到越界命令（mcp_server 会按
    profile 裁 cmd enum），这条是漏网时的兜底。
    """
    policy = UI_COMMAND_POLICY.get(cmd) or {}
    if policy.get("actor") == "user_only":
        raise RuntimeError("ui_command_forbidden: submit 只能由用户在向导中点击，Agent 不得调用")
    profiles = policy.get("profiles")
    if profile and profiles and profile not in profiles:
        raise RuntimeError(
            f"ui_command_forbidden: {cmd} 不属于 profile={profile}"
            f"（本 profile 可用：{'、'.join(sorted(allowed_ui_commands(profile)))}）。"
            "不要换命令重试——阶段由服务端决定，如阶段不对请让用户重发任务。")


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
