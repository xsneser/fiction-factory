#!/usr/bin/env python3
"""分阶段内容构建工具验收（真实 LLM，无 api.json 时跳过）：
① generate_core_conflict / ③ generate_factions / ④ generate_characters（带上文）
/ ⑤ generate_rest_world（不含 core_conflict/factions/characters 键）。

用法：python tools/test_world_stages.py
"""
import os
import sys
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def main():
    if not os.path.exists("api.json"):
        print("[skip] api.json 不存在，跳过真实 LLM 分阶段验收")
        return

    from ui.web_blueprints.ctx import get_llm
    from libraries.prompt_harness import PromptHarness
    from libraries.world_builder import WorldBuildingGenerator

    llm = get_llm()
    harness = PromptHarness()
    gen = WorldBuildingGenerator(llm_client=llm, harness=harness)
    idea = "灵气复苏我觉醒复制异能，系统在吞噬我的记忆"
    tags = ["都市", "系统流"]

    # ① 核心矛盾
    conflict = gen.generate_core_conflict(genre="都市", idea=idea, tags=tags, pen_name="枫落")
    check("① 核心矛盾非空", bool(conflict) and len(conflict) > 4, f"({conflict[:40]}…)")

    # ③ 势力
    factions = gen.generate_factions(genre="都市", idea=idea, core_conflict=conflict, tags=tags)
    ok_f = (2 <= len(factions) <= 4 and all(isinstance(f, dict) and f.get("name") for f in factions))
    check("③ 势力 2-4 个含 name", ok_f, f"({len(factions)} 个)")

    # ④ 角色（带上文）
    chars = gen.generate_characters(idea=idea, genre="都市", tags=tags, title="复制之王",
                                    core_conflict=conflict, factions=factions,
                                    outline_preview="开篇：便利店夜班觉醒复制异能")
    ok_c = chars and len(chars.get("protagonists") or []) >= 1 and len(chars.get("supporting_cast") or []) >= 1
    check("④ 角色带上文 ≥1 主角 + ≥1 配角", bool(ok_c))

    # ⑤ 其余维度（复用 generate 2 链）
    rest = gen.generate_rest_world(
        genre="都市", idea=idea, world_brief="2019 年灵气复苏，人类觉醒灵纹异能",
        core_conflict=conflict, factions=factions,
        outline_preview="开篇：便利店夜班觉醒复制异能，系统发布第一个任务",
        tags=tags, pen_name="枫落")
    wb = rest.get("world_building") or {}
    dims_filled = [k for k, v in wb.items() if (isinstance(v, list) and v) or str(v or "").strip()]
    check("⑤ 其余维度 ≥3 维填", len(dims_filled) >= 3, f"({len(dims_filled)} 维: {','.join(dims_filled[:5])}…)")
    check("⑤ 不含 core_conflict/factions/characters",
          "core_conflict" not in wb and "factions" not in wb and "characters" not in rest)
    check("⑤ 含基调键", all(k in rest for k in ("tone", "target_audience", "pov", "era_language")))

    print(f"\n{'='*50}")
    print(f"  分阶段构建验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败项:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过！")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
