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
