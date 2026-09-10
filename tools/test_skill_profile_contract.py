#!/usr/bin/env python3
"""skill ↔ MCP profile 契约测试（P0）。

对 agent-sidecar/skills/*/SKILL.md 逐一断言：
  referenced_tools(SKILL.md) ⊆ PROFILE_TOOLS[SKILL_PROFILE_MAP[skill]]
任一 skill 引用了其 profile 未暴露的工具即失败，并打印缺失清单；
顺带断言 .dsh/skills 镜像与源 byte-identical（防运行期镜像漂移）。

用法：python tools/test_skill_profile_contract.py
"""
import os
import sys
import typing

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries.skill_profile import (  # noqa: E402
    SKILL_PROFILE_MAP, profile_allows, referenced_tools, skill_text,
    skill_path, missing_for_skill, dsh_mirror_mismatch,
)
from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def main():
    from agent_tools import TOOL_REGISTRY
    registry_names = {entry["name"] for entry in TOOL_REGISTRY}
    for profile, names in sorted(PROFILE_TOOLS.items()):
        missing_registry = sorted(set(names) - registry_names)
        check(f"profile={profile} ⊆ TOOL_REGISTRY", not missing_registry,
              f"缺失: {missing_registry}" if missing_registry else f"({len(names)} 个)")

    for skill, profile in SKILL_PROFILE_MAP.items():
        text = skill_text(skill)
        refs = referenced_tools(text)
        allowed = profile_allows(profile)
        missing = sorted(refs - set(allowed))
        used = sorted(refs & set(allowed))
        if missing:
            lines = "\n".join(f"  - {m}" for m in missing)
            check(f"{skill} references only profile={profile} tools", False,
                  f"\n{skill} references tools NOT exposed by profile={profile}:\n{lines}\n  实际引用且在集内: {used}")
        else:
            check(f"{skill} ⊆ profile={profile}", True,
                  f"(引用 {len(used)} 个: {used})")
        unused = sorted(set(profile_allows(profile)) - refs)
        if unused:
            print(f"  ⚠️ {skill} profile 未被 skill 文本直接引用（非阻断）: {unused}")

    # ── drive_ui 的可见 schema 必须等于能力边界（P1）────────────────────────────
    # 回归（2026-09-10）：build-candidates 的 run 里模型看得见 set_world、调了才被拒，
    # 于是整轮预算花在「试 set_world → 试 set_characters → navigate → 再读状态」上。
    import agent_tools as AT
    from libraries.agent_tool_router import (UI_COMMAND_POLICY, allowed_ui_commands,
                                             check_ui_command, filter_drive_ui_doc,
                                             make_drive_ui_for_profile)
    all_cmds = sorted(AT._WIZARD_CMDS)
    for profile in sorted(PROFILE_TOOLS):
        allowed = allowed_ui_commands(profile)
        # ① 能力集与 check_ui_command 的判定必须同源（暴力比对，防两张表漂移）
        real = set()
        for c in all_cmds:
            try:
                check_ui_command(c, profile)
                real.add(c)
            except RuntimeError:
                pass
        check(f"allowed_ui_commands({profile}) 与 check_ui_command 同源", allowed == real,
              f"表={sorted(allowed - real)} 判定={sorted(real - allowed)}")
        if "drive_ui" not in PROFILE_TOOLS[profile]:
            continue   # 没暴露 drive_ui 的 profile：裁不裁描述都无所谓，不必断言
        # ② 暴露的 cmd enum 就是能力集（schema 层不可表达越界命令）
        cands = [c for c in all_cmds if c in allowed]
        keep = [c for c in all_cmds if c not in allowed]
        if not cands or not keep:
            continue
        base = next((e["func"] for e in AT.TOOL_REGISTRY if e["name"] == "drive_ui"), None)
        fn, desc = make_drive_ui_for_profile(profile, base)
        got = set(typing.get_args(typing.get_type_hints(fn).get("cmd")) or ())
        check(f"drive_ui({profile}) 的 cmd 注解 == 能力集", got == set(cands),
              f"多={sorted(got - set(cands))} 少={sorted(set(cands) - got)}")
        # ③ description 不泄露越界命令（submit 例外：它是禁止性说明）
        leaked = [c for c in keep if c != "submit" and c in (desc or "")]
        check(f"drive_ui({profile}) 描述不含越界命令", not leaked, f"泄露={leaked}")
        check(f"drive_ui({profile}) 描述保留 submit 禁令（护栏）",
              "submit" in (desc or ""), "")

    # ④ 越界调用的报错必须可行动（带上本 profile 的合法命令集）
    try:
        check_ui_command("set_world", "build-candidates")
        check("越界命令报错可行动", False, "未抛错")
    except RuntimeError as e:
        check("越界命令报错可行动",
              "set_world" in str(e) and "set_field" in str(e), str(e)[:120])

    mirror = dsh_mirror_mismatch()
    check(".dsh/skills 镜像与源 byte-identical", not mirror, ("；".join(mirror)) if mirror else "(6 skill 一致)")

    # 运行时预检（P0-3）：注入 skill 引用 profile 外工具 → 明确报错并拒绝启动，不 spawn、不 fail-open。
    import libraries.dsh_bridge as DB
    _orig_skill_text = DB._skill_text_for_profile
    DB._skill_text_for_profile = (lambda p: "【坏样例】本页会调 save_outlines(book_id=…)"
                                  if p == "write" else _orig_skill_text(p))
    try:
        evs = list(DB.run_dsh_task("写下一章"))
    finally:
        DB._skill_text_for_profile = _orig_skill_text
    err = next((e for e in evs if e.get("type") == "error"), {})
    check("运行时预检：profile 外工具 → 拒绝启动",
          "契约不匹配" in (err.get("message") or "") and any(e.get("type") == "done" for e in evs),
          (err.get("message") or "")[:100])

    print("\n" + "=" * 60)
    print("  每 profile 工具数:", {p: len(s) for p, s in sorted(PROFILE_TOOLS.items())})
    for skill, profile in SKILL_PROFILE_MAP.items():
        used = sorted(referenced_tools(skill_text(skill)) & set(profile_allows(profile)))
        print(f"  [{skill}] profile={profile} 实际引用 {len(used)}: {used}")
    print("=" * 60)
    print(f"  Skill/Profile 契约验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
