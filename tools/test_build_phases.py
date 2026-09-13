#!/usr/bin/env python3
"""规划阶段状态机 + 语义摘要测试。

钉死四类不变量：

1. **转移表逐行**：每个 phase 只认自己的唯一事件；`agent_save` 只在该 phase 的目标
   路径被写入时才推进（不是"泛化地推进到下一阶段"）。
2. **防双推进**：`user_ack` 只在 `stop_A` 有效；`validation_pass` 只在 `validate` 有效。
   否则会出现"agent save 推一级 + 用户点继续再推一级"跳过 H0。
3. **对镜证据**：build 入口 `mirror → scaffold` 必须由**服务器观测到的两条查库**驱动。
4. **顺序语义**：`plots`/`outlines` 的排列是叙事顺序，交换顺序必须改变 digest 并使
   下游失效；而空白差异、集合性集合（characters）的重排不得制造假变更。

用法：python tools/test_build_phases.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries import build_phases as bp  # noqa: E402
from libraries import plan_diff as pd  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _plot(pid, **kw):
    d = {"id": pid, "name": pid}
    d.update(kw)
    return d


def main():
    print("=" * 60)
    print("  规划阶段状态机 / 语义摘要")
    print("=" * 60)

    # ── 1) build 链的转移表逐行 ──────────────────────────────────────────────
    m = bp.default_meta(bp.ENTRY_BUILD)
    check("初始持久 phase = planning_thesis", m["phase"] == bp.P_THESIS, m["phase"])
    check("初始 chain 含两个停点",
          bp.chain_for(bp.ENTRY_BUILD) == ["planning_thesis", "mirror", "scaffold", "stop_A",
                                           "executable_horizon", "forecast_horizon",
                                           "promise_reconciliation", "validate", "stop_B", "done"],
          str(bp.chain_for(bp.ENTRY_BUILD)))

    # agent_save 写非目标路径 → 不推进
    check("thesis 阶段写 storyline 路径 → 不推进",
          bp.advance(m, "agent_save", wrote_paths=["storyline.plots[id=pl1].words"]) is None)
    m = bp.advance(m, "agent_save", wrote_paths=["world.world_building.core_conflict"])
    check("thesis --agent_save--> mirror", m and m["phase"] == bp.P_MIRROR,
          m and m["phase"])
    check("planning_thesis 进 completed", "planning_thesis" in m["completed_phases"])

    # mirror 需要服务器观测的两条查库证据
    check("mirror 证据不全 → 不推进",
          bp.advance(m, "mirror_done",
                     mirror_evidence={"arc_query": True, "plot_query": False}) is None)
    m = bp.advance(m, "mirror_done",
                   mirror_evidence={"arc_query": True, "plot_query": True})
    check("mirror --mirror_done(证据齐)--> scaffold", m and m["phase"] == bp.P_SCAFFOLD,
          m and m["phase"])

    m = bp.advance(m, "agent_save", wrote_paths=["characters[id=char_夏明鸢].personality"])
    check("scaffold --agent_save--> stop_A", m and m["phase"] == bp.P_STOP_A, m and m["phase"])

    # 停点只认 user_ack（防双推进）
    check("stop_A 收到 agent_save → no-op",
          bp.advance(m, "agent_save", wrote_paths=["world"]) is None)
    check("stop_A 收到 validation_pass → no-op", bp.advance(m, "validation_pass") is None)
    m = bp.advance(m, "user_ack")
    check("stop_A --user_ack--> executable_horizon", m and m["phase"] == bp.P_H0, m and m["phase"])

    m = bp.advance(m, "agent_save", wrote_paths=["storyline.plots[id=pl1].words"])
    check("H0 --agent_save--> forecast_horizon", m and m["phase"] == bp.P_FORECAST, m and m["phase"])
    m = bp.advance(m, "agent_save", wrote_paths=["storyline.planning.future_intents"])
    check("forecast --agent_save--> promise_reconciliation",
          m and m["phase"] == bp.P_PROMISE, m and m["phase"])
    m = bp.advance(m, "agent_save", wrote_paths=["storyline.plots[id=pl1].foreshadow"])
    check("promise --agent_save--> validate", m and m["phase"] == bp.P_VALIDATE, m and m["phase"])

    check("validate 收到 agent_save → no-op（只认 validation_pass）",
          bp.advance(m, "agent_save", wrote_paths=["world"]) is None)
    m = bp.advance(m, "validation_pass")
    check("validate --validation_pass--> stop_B", m and m["phase"] == bp.P_STOP_B, m and m["phase"])
    check("stop_B 收到 user_ack → no-op（防双推进）", bp.advance(m, "user_ack") is None)
    m = bp.advance(m, "user_submit")
    check("stop_B --user_submit--> done", m and m["phase"] == bp.P_DONE, m and m["phase"])
    check("done 之后无转移", bp.advance(m, "user_submit") is None)

    # ── 2) replan 链：无 scaffold / 无 stop_A / 末尾 preview_confirm ──────────
    r = bp.default_meta(bp.ENTRY_REPLAN)
    check("replan chain 无 scaffold/stop_A",
          bp.P_SCAFFOLD not in bp.chain_for(bp.ENTRY_REPLAN)
          and bp.P_STOP_A not in bp.chain_for(bp.ENTRY_REPLAN),
          str(bp.chain_for(bp.ENTRY_REPLAN)))
    r = bp.advance(r, "agent_save", wrote_paths=["world.world_building.core_conflict"])
    check("replan thesis --agent_save--> executable_horizon（跳过 mirror/scaffold）",
          r and r["phase"] == bp.P_H0, r and r["phase"])
    for ev, paths in (("agent_save", ["storyline.plots[id=p].words"]),
                      ("agent_save", ["storyline.planning.future_intents"]),
                      ("agent_save", ["storyline.promises"]),
                      ("validation_pass", None)):
        r = bp.advance(r, ev, wrote_paths=paths)
    check("replan 末尾 = preview_confirm", r and r["phase"] == bp.P_PREVIEW_CONFIRM,
          r and r["phase"])
    r = bp.advance(r, "user_confirm")
    check("preview_confirm --user_confirm--> done", r and r["phase"] == bp.P_DONE, r and r["phase"])

    # ── 3) 顺序语义：plots 交换必须改变 digest 并让下游失效 ─────────────────
    a = {"storyline": {"plots": [_plot("pl21"), _plot("pl22"), _plot("pl23")]}}
    b = {"storyline": {"plots": [_plot("pl21"), _plot("pl23"), _plot("pl22")]}}
    check("仅交换 pl22/pl23 顺序 → digest 必须变化",
          pd.semantic_digest(a) != pd.semantic_digest(b))
    diff = pd.semantic_diff(a, b)
    check("顺序变更被记为 storyline.plots.order",
          any(c["path"] == "storyline.plots.order" for c in diff["changed"]),
          str([c["path"] for c in diff["changed"]]))
    stale = bp.stale_for_paths(pd.changed_paths(diff), entry=bp.ENTRY_BUILD)
    check("顺序变更 → H1/H2 与 validate 失效", stale == ["forecast_horizon", "validate"], str(stale))

    # outlines 同理
    oa = {"storyline": {"outlines": [{"id": "a1"}, {"id": "a2"}]}}
    ob = {"storyline": {"outlines": [{"id": "a2"}, {"id": "a1"}]}}
    check("outlines 顺序变更 → digest 变化", pd.semantic_digest(oa) != pd.semantic_digest(ob))

    # ── 4) 不得制造假变更 ────────────────────────────────────────────────────
    check("首尾空白不算变更",
          pd.semantic_digest({"world": {"core_conflict": "abc"}})
          == pd.semantic_digest({"world": {"core_conflict": "  abc  "}}))
    check("None vs \"\" 不算变更",
          pd.semantic_digest({"world": {"world_summary": None}})
          == pd.semantic_digest({"world": {"world_summary": ""}}))
    ca = {"characters": [{"id": "c1", "name": "甲"}, {"id": "c2", "name": "乙"}]}
    cb = {"characters": [{"id": "c2", "name": "乙"}, {"id": "c1", "name": "甲"}]}
    check("人物重排不算变更（集合性）", pd.semantic_digest(ca) == pd.semantic_digest(cb))

    # ── 5) 路径 → 级别 + 失效依赖图 ─────────────────────────────────────────
    check("thesis 路径级别", bp.phase_for_path("world.world_building.core_conflict") == bp.P_THESIS)
    check("骨架路径级别", bp.phase_for_path("world.world_building.geography") == bp.P_SCAFFOLD)
    check("人物路径级别", bp.phase_for_path("characters[id=c1].goal") == bp.P_SCAFFOLD)
    check("情节段路径级别", bp.phase_for_path("storyline.plots[id=p1].words") == bp.P_H0)
    check("伏笔路径级别", bp.phase_for_path("storyline.plots[id=p1].foreshadow") == bp.P_PROMISE)
    thesis_diff = pd.semantic_diff(
        {"world": {"core_conflict": "旧"}}, {"world": {"core_conflict": "新"}})
    check("thesis 改动 → scaffold/H0/H1H2/validate 全失效",
          bp.stale_for_paths(pd.changed_paths(thesis_diff)) ==
          ["scaffold", "executable_horizon", "forecast_horizon", "validate"],
          str(bp.stale_for_paths(pd.changed_paths(thesis_diff))))

    # ── 6) stale frontier 逐项推进（不能一刀切清空）─────────────────────────
    meta = bp.default_meta(bp.ENTRY_BUILD)
    # 全链都做过（否则"没做过的阶段"不该被标失效——见 mark_stale 的说明）
    meta["completed_phases"] = ["planning_thesis", "mirror", "scaffold",
                                "executable_horizon", "forecast_horizon", "validate"]
    meta = bp.mark_stale(meta, ["scaffold", "executable_horizon", "forecast_horizon", "validate"])
    check("stale 记入 4 项", meta["stale_phases"] ==
          ["scaffold", "executable_horizon", "forecast_horizon", "validate"],
          str(meta["stale_phases"]))
    meta["phase"] = bp.P_SCAFFOLD
    meta = bp.advance(meta, "agent_save", wrote_paths=["world"])
    check("重做 scaffold → 只移除 scaffold，其余仍失效",
          meta["stale_phases"] == ["executable_horizon", "forecast_horizon", "validate"],
          str(meta["stale_phases"]))
    check("重做 scaffold → 下游仍留在 stale（不是清空）", meta["stale_phases"], "非空")
    check("completed_phases 只增不减（audit）",
          meta["completed_phases"][:3] == ["planning_thesis", "mirror", "scaffold"],
          str(meta["completed_phases"]))
    check("有效完成 = completed − stale（重做过的 scaffold 已重新有效）",
          bp.effective_completed(meta) == ["planning_thesis", "mirror", "scaffold"]
          and bp.is_phase_effective(meta, bp.P_SCAFFOLD)
          and not bp.is_phase_effective(meta, bp.P_H0),
          str(bp.effective_completed(meta)))

    # ── 7) 对镜证据由**服务器按实际 tool call** 记录，且不动 revision ────────
    from libraries import build_draft as BD  # noqa: E402
    from libraries import dsh_bridge as DB  # noqa: E402
    sid = "selftest-mirror-evidence"
    try:
        if os.path.exists(BD.path_for(sid)):
            os.remove(BD.path_for(sid))
        BD.transition(sid, step=3, idea="对镜证据", tags=["测试"], pen_name="枫落")
        # 先立命题把 phase 推到 mirror（对镜证据是**离开 mirror** 的条件）
        BD.update(sid, on_meta=lambda m: bp.advance(
            m, "agent_save", wrote_paths=["world.world_building.core_conflict"]))
        check("推进到 mirror 待对镜", BD.load(sid)["plan_meta"]["phase"] == bp.P_MIRROR)
        rev_before = BD.load(sid)["revision"]
        DB._note_mirror_evidence(
            {"type": "tool_call", "name": "mcp__novelengine__query_arc_library"}, sid, set())
        rec = BD.load(sid)
        check("桥层记下 arc_query", rec["plan_meta"]["mirror_evidence"]["arc_query"] is True)
        check("记证据不动 revision（否则 agent 自己查询会把自己的 CAS 判过期）",
              rec["revision"] == rev_before, f"{rev_before} → {rec['revision']}")
        check("只记了一条时 mirror → scaffold 仍不放行",
              bp.advance(rec["plan_meta"], "mirror_done") is None)
        # 无关的工具调用不产生证据
        DB._note_mirror_evidence(
            {"type": "tool_call", "name": "mcp__novelengine__validate_build"}, sid, set())
        check("无关工具不记证据",
              BD.load(sid)["plan_meta"]["mirror_evidence"]["plot_query"] is False)
        DB._note_mirror_evidence(
            {"type": "tool_call", "name": "mcp__novelengine__query_plots"}, sid, set())
        rec = BD.load(sid)
        # 证据齐了**当场**推进（mirror_done 的触发点就是观测动作本身），不必等下一次落盘
        check("两条齐后 mirror → scaffold 放行",
              rec["plan_meta"]["phase"] == bp.P_SCAFFOLD, rec["plan_meta"]["phase"])
    finally:
        if os.path.exists(BD.path_for(sid)):
            os.remove(BD.path_for(sid))

    print("\n" + "=" * 60)
    print(f"  规划阶段状态机验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
