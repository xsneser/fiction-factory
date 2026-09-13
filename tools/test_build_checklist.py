#!/usr/bin/env python3
"""待填清单 / 硬门禁 / 校验强度 测试。

钉死三件事：

1. **清单回答 readiness，不回答 phase**：空 / 半 / 全三份草稿的档位；
2. **硬门禁是现有 issues 的真子集**——只能断言单向蕴含
   `gates.passed is False ⇒ validation.passed is False`。
   **不能写 iff**：`issues` 里完全可以同时有非门禁错误，"门禁过、整体不过"是正常情形。
   反向断言（`passed is False ⇒ gates fail`）会立刻失败，这里也顺带钉住它不成立。
3. **校验强度按 path 取 validator 闭包**，不是"最高一档"：
   `plots[*].foreshadow` 要的是 `{h0, promise}` 两个，而不是只跑 promise 档。

外加：`get_build_context` **读工具零副作用**（调用前后 canonical 一字不动）。

用法：python tools/test_build_checklist.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_tools as AT  # noqa: E402
from libraries import build_checklist as BC  # noqa: E402
from libraries import build_draft as BD  # noqa: E402
from tools.test_build_step3_tools import CHARACTERS, STORYLINE, WORLD  # noqa: E402

SID = "selftest-build-checklist"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _status(cl, iid):
    return next((it["status"] for it in cl["items"] if it["id"] == iid), "?")


def main():
    print("=" * 60)
    print("  待填清单 / 硬门禁 / 校验强度")
    print("=" * 60)

    # ── 1) 空草稿：三项硬门禁全部 blocking ──────────────────────────────────
    empty = BC.build_checklist(None)
    check("空草稿：premise missing", _status(empty, "premise") == BC.MISSING)
    check("空草稿：h0 missing", _status(empty, "h0") == BC.MISSING)
    check("空草稿：cast missing", _status(empty, "cast") == BC.MISSING)
    check("空草稿：硬门禁 blocking = premise/h0/cast",
          sorted(empty["gates"]["blocking"]) == ["cast", "h0", "premise"],
          str(empty["gates"]["blocking"]))
    check("空草稿：gates.passed = False", empty["gates"]["passed"] is False)
    check("summary 明说硬门禁未过", "硬门禁未过" in empty["summary"], empty["summary"])

    # ── 2) 半份（只立了命题）：premise 过、骨架/H0 未过 ──────────────────────
    half = BC.build_checklist({"world": {"world_building": {
        "core_conflict": "用不断折损的记忆，把流放地建成谁也送不走的要塞",
        "differentiation": "同类靠金手指，本书靠记忆贴现的制度成本"}}})
    check("半份：premise 不算 missing（世界观段非空）",
          _status(half, "premise") in (BC.OK, BC.THIN), _status(half, "premise"))
    check("半份：world_dims missing（维度没填）",
          _status(half, "world_dims") == BC.MISSING, _status(half, "world_dims"))
    check("半份：h0 / cast 仍 blocking",
          set(half["gates"]["blocking"]) == {"h0", "cast"}, str(half["gates"]["blocking"]))

    # ── 3) 全份：门禁全过 ────────────────────────────────────────────────────
    full = BC.build_checklist({"world": WORLD, "characters": CHARACTERS, "storyline": STORYLINE})
    check("全份：gates.passed = True", full["gates"]["passed"] is True,
          str(full["gates"]["blocking"]))
    check("全份：factions ok", _status(full, "factions") == BC.OK, _status(full, "factions"))
    check("全份：cast ok", _status(full, "cast") == BC.OK, _status(full, "cast"))
    check("全份：h0 ok", _status(full, "h0") == BC.OK, _status(full, "h0"))
    check("全份：远期伏笔 thin（还没登记）",
          _status(full, "far_foreshadow") == BC.THIN, _status(full, "far_foreshadow"))
    check("全份：弧设计意图 missing（老 payload 没写 design_intent）",
          _status(full, "arc_intent") == BC.MISSING, _status(full, "arc_intent"))

    # ── 4) 单向蕴含：gate fail ⇒ validation fail（**不是** iff）─────────────
    AT._current_build_session = lambda explicit="": str(explicit or SID)
    real_exists = BD.exists(SID)
    try:
        cases = [
            ("空草稿", None, None, None),
            ("只有命题", {"world_building": {"core_conflict": "x" * 20}}, None, None),
            ("全份", WORLD, STORYLINE, CHARACTERS),
        ]
        for label, world, sl, chars in cases:
            r = AT.validate_build(world=world, storyline=sl, characters=chars,
                                  build_session_id=SID)
            g, p = r["gates"]["passed"], r["passed"]
            check(f"蕴含成立（{label}）：gate 不过 ⇒ 整体不过", (not g) <= (not p),
                  f"gates={g} passed={p}")
            if not g:
                # 门禁项固定是三选（世界观为空 / 弧或情节段为空 / 没有主角）
                check(f"{label} 确实触发了门禁",
                      bool(r["gates"]["blocking"])
                      and set(r["gates"]["blocking"]) <= {"premise", "h0", "cast"},
                      str(r["gates"]["blocking"]))
        # 反向不成立：造一份"门禁过但有非门禁 issue"的草稿（plot 挂非叶弧）
        bad_sl = {"outlines": [{"id": "A", "name": "甲", "start_word": 0, "end_word": 3000},
                               {"id": "A1", "name": "甲子", "start_word": 0, "end_word": 3000,
                                "parent_arc_id": "A"}],
                  "plots": [{"id": "p1", "name": "越级", "outline_id": "A",
                             "primary_turn": "挂到了非叶弧", "words": 800}]}
        r2 = AT.validate_build(world=WORLD, storyline=bad_sl, characters=CHARACTERS,
                               build_session_id=SID)
        check("门禁可过而整体不过（非门禁 issue）⇒ 只能单向断言",
              r2["gates"]["passed"] is True and r2["passed"] is False,
              f"gates={r2['gates']['passed']} passed={r2['passed']} issues={r2['issues'][:2]}")
    finally:
        if not real_exists and os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))

    # ── 5) 校验强度：按 path 取 validator 闭包（不是线性最大档）──────────────
    v = BC.validators_for_paths(["storyline.plots[id=pl1].foreshadow"])
    check("伏笔路径 → {h0, promise}（含前置闭包）",
          v == {BC.V_H0, BC.V_PROMISE}, str(sorted(v)))
    v = BC.validators_for_paths(["world.world_building.core_conflict"])
    check("命题路径 → 只跑 thesis 档", v == {BC.V_THESIS}, str(sorted(v)))
    v = BC.validators_for_paths(["characters[id=c1].personality"])
    check("人物路径 → {world, characters}", v == {BC.V_WORLD, BC.V_CHARACTERS}, str(sorted(v)))
    v = BC.validators_for_paths(["storyline.planning.future_intents"])
    check("远期路径 → {h0, forecast}", v == {BC.V_H0, BC.V_FORECAST}, str(sorted(v)))
    check("无路径时退化为按 phase 取默认集",
          BC.validators_for_paths([], phase="validate") ==
          {BC.V_THESIS, BC.V_WORLD, BC.V_CHARACTERS, BC.V_H0, BC.V_FORECAST, BC.V_PROMISE})

    # ── 6) 读工具零副作用 ────────────────────────────────────────────────────
    try:
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))
        BD.transition(SID, step=3, idea="零副作用检查", tags=["测试"], pen_name="枫落")
        before = BD.load(SID)
        with open(BD.path_for(SID), "rb") as f:
            raw_before = f.read()
        ctx = AT.get_build_context()
        after = BD.load(SID)
        with open(BD.path_for(SID), "rb") as f:
            raw_after = f.read()
        check("get_build_context 不改 canonical revision",
              before["revision"] == after["revision"],
              f"{before['revision']} → {after['revision']}")
        check("get_build_context 字节级零写入", raw_before == raw_after)
        check("没有草稿时不返回 checklist（不误导）", ctx.get("checklist") is None,
              str(ctx.get("checklist"))[:60])
        check("返回 plan_meta 与 next_phase",
              ctx["plan_meta"]["phase"] == "planning_thesis"
              and ctx["next_phase"] == "mirror",
              f"{ctx['plan_meta']['phase']} → {ctx['next_phase']}")
    finally:
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))

    print("\n" + "=" * 60)
    print(f"  待填清单验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
