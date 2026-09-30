#!/usr/bin/env python3
"""分阶段落盘 / 下游失效 / 版本轴：端到端（不起 dsh、不调 LLM、直接打 canonical）。

钉死五类不变量：

1. **phase-aware 校验**：立命题阶段只写 `core_conflict`/`differentiation` 就能落盘，
   而同一份草稿走公开的全量校验会被拒（证明是"按阶段放宽尚未轮到的 path"，
   不是把校验放松了）；
2. **版本轴分开**：只写流程元数据（阶段确认）不动 `content_revision`，
   刚通过的校验回执因此**不会莫名失效**——这是旧设计里最常见的"莫名 bug"；
3. **失效只对做过的阶段生效** + **游标回退**：新书立完命题不该显示"4 个阶段要重查"；
   真正改上游后，游标回到最早的失效阶段，重做一个只清一个；
4. **CAS 在锁内**：陈旧 `expected_revision` → `revision_conflict`，且一个字不写；
5. **锁定字段**：用户保留的字段不被 agent 覆盖；结构身份字段（id/order/start_word）
   服务端拒绝锁定。

用法：python tools/test_build_stale.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_tools as AT  # noqa: E402
from libraries import build_draft as BD  # noqa: E402
from libraries import build_phases as BP  # noqa: E402
from libraries import plan_paths as PP  # noqa: E402
from tools.test_build_step3_tools import CHARACTERS, STORYLINE, WORLD  # noqa: E402

SID = "selftest-build-stale"
PASS, FAIL = [], []

THESIS_WORLD = {"world_building": {
    "core_conflict": "用不断折损的记忆，把母国随时准备回收的流放地建成谁也送不走的要塞",
    "differentiation": "同类靠金手指，本书靠记忆贴现的制度成本"}}
FUTURE_PATCH = {"future_intents": [{"id": "fi1", "kind": "arc_intent", "intent": "星门清算"}]}


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _save(**kw):
    return AT.save_build_draft(build_session_id=SID, **kw)


def _meta():
    return BD.load(SID)["plan_meta"]


def _ack():
    """模拟用户在停点点「继续下一阶段」。"""
    BD.update(SID, on_meta=lambda m: BP.advance(m, "user_ack") or m)


def main():
    print("=" * 60)
    print("  分阶段落盘 / 下游失效 / 版本轴")
    print("=" * 60)
    AT._current_build_session = lambda explicit="": str(explicit or SID)

    try:
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))
        BD.transition(SID, step=3, idea="记忆贴现的基建流放地", tags=["科幻", "基建"],
                      pen_name="星烬")

        # ── 1) 分阶段落盘：立命题即可落，全量校验会拒 ────────────────────────
        r = _save(world=THESIS_WORLD)
        check("立命题阶段只写命题 → 落盘成功", r.get("saved") is True, str(r.get("message")))
        check("本次校验只跑了 thesis 档 + world/characters",
              set(r.get("checked") or []) <= {"thesis", "world", "characters"},
              str(r.get("checked")))
        check("phase: planning_thesis → mirror", _meta()["phase"] == BP.P_MIRROR,
              _meta()["phase"])
        full = AT.validate_build(world=THESIS_WORLD, storyline=None, characters=None,
                                 build_session_id=SID)
        check("同一份草稿走**全量**校验会被拒（放宽是分阶段的，不是放松校验）",
              full["passed"] is False, str(full["issues"])[:80])

        # ── 2) 新书不该立刻显示"下游要重查" ──────────────────────────────────
        check("刚立完命题：stale 为空（没做过的阶段谈不上失效）",
              _meta()["stale_phases"] == [], str(_meta()["stale_phases"]))
        check("completed 记下 planning_thesis",
              "planning_thesis" in _meta()["completed_phases"])

        # ── 3) 对镜证据 → 自动离开 mirror ────────────────────────────────────
        from libraries import dsh_bridge as DB
        DB._note_mirror_evidence(
            {"type": "tool_call", "name": "mcp__novelengine__query_arc_library"}, SID, set())
        check("只查了弧库 → 仍停在 mirror（不放行）", _meta()["phase"] == BP.P_MIRROR,
              _meta()["phase"])
        DB._note_mirror_evidence(
            {"type": "tool_call", "name": "mcp__novelengine__query_plots"}, SID, set())
        check("两条查库齐 → mirror → scaffold", _meta()["phase"] == BP.P_SCAFFOLD,
              _meta()["phase"])

        # ── 4) 骨架落盘 → 停点 A ─────────────────────────────────────────────
        r = _save(world=WORLD, characters=CHARACTERS)
        check("骨架落盘成功", r.get("saved") is True, str(r.get("validation", {}).get("issues")))
        check("scaffold → stop_A（停点）", _meta()["phase"] == BP.P_STOP_A, _meta()["phase"])
        _ack()
        check("用户确认 → executable_horizon", _meta()["phase"] == BP.P_H0, _meta()["phase"])

        # ── 5) 走完后半程：H0 → forecast → promise → validate → stop_B ───────
        _save(storyline=STORYLINE)
        check("H0 落盘 → forecast_horizon", _meta()["phase"] == BP.P_FORECAST, _meta()["phase"])
        _save(storyline={**STORYLINE, "planning": FUTURE_PATCH})
        check("远期落盘 → promise_reconciliation", _meta()["phase"] == BP.P_PROMISE,
              _meta()["phase"])
        _save(storyline={**STORYLINE, "planning": FUTURE_PATCH, "promises": [
            {"id": "pr1", "setup_plot_id": "p1", "status": "planned", "desc": "刑台旧令"}]})
        check("伏笔对账落盘 → validate", _meta()["phase"] == BP.P_VALIDATE, _meta()["phase"])
        r = _save()
        check("validate 阶段跑通 → stop_B", _meta()["phase"] == BP.P_STOP_B, _meta()["phase"])
        rec = BD.load(SID)
        check("校验回执绑定**写入后**的 content_revision",
              rec["plan_meta"]["validated"]["content_revision"] == rec["content_revision"],
              f"{rec['plan_meta']['validated']['content_revision']} vs {rec['content_revision']}")

        # ── 6) 版本轴：只写流程元数据不动 content_revision / 不回执失效 ───────
        cr_before, rev_before = rec["content_revision"], rec["revision"]
        BD.update(SID, on_meta=lambda m: {**m, "phase_ack": {"phase": "x"}})
        rec = BD.load(SID)
        check("纯流程写：revision 涨、content_revision 不涨",
              rec["revision"] > rev_before and rec["content_revision"] == cr_before,
              f"rev {rev_before}→{rec['revision']}, cr {cr_before}→{rec['content_revision']}")
        check("纯流程写不让刚通过的校验失效",
              rec["plan_meta"]["validated"]["content_revision"] == rec["content_revision"])

        # ── 7) 失效链：改核心矛盾 → 已做过的下游标记 + 游标回退 ──────────────
        r = _save(world={**WORLD, "world_building": {
            **WORLD["world_building"], "core_conflict": "换一个全新的核心矛盾说法，重打全套推论"}})
        meta = _meta()
        check("改核心矛盾 → 已做过的下游全线失效",
              meta["stale_phases"] == ["scaffold", "executable_horizon",
                                       "forecast_horizon", "validate"],
              str(meta["stale_phases"]))
        check("游标回退到最早的失效阶段", meta["phase"] == BP.P_SCAFFOLD, meta["phase"])
        check("completed_phases 不回退（审计：曾做过）",
              "validate" in meta["completed_phases"], str(meta["completed_phases"]))
        check("有效完成 = completed − stale",
              "scaffold" not in BP.effective_completed(meta)
              and "planning_thesis" in BP.effective_completed(meta),
              str(BP.effective_completed(meta)))

        # ── 8) frontier 逐项推进：重做 scaffold 只清 scaffold ────────────────
        # 重做必须改**骨架级**字段：回退 core_conflict 是命题级改动，按设计就不该
        # 清掉 scaffold 的失效标记（那正是"改上游 ⇒ 下游仍失效"的意思）。
        cur_wb = BD.load(SID)["draft"]["world"]["world_building"]
        r = _save(world={"world_building": {
            **cur_wb, "geography": "断脊隘口，其下三层地下城"}})
        meta = _meta()
        check("重做 scaffold → 只清掉 scaffold，其余仍失效",
              meta["stale_phases"] == ["executable_horizon", "forecast_horizon", "validate"],
              str(meta["stale_phases"]))
        check("落盘回执如实报告 stale（agent 据此继续重做）",
              r.get("stale_phases") == meta["stale_phases"], str(r.get("stale_phases")))

        # ── 9) CAS 在锁内 + 陈旧 revision 拒收且一字不写 ─────────────────────
        stale_rev = BD.load(SID)["revision"] - 1
        before = BD.load(SID)
        r = _save(world=THESIS_WORLD, expected_revision=stale_rev)
        check("陈旧 expected_revision → revision_conflict",
              r.get("error") == "revision_conflict", str(r.get("error")))
        check("CAS 拒收时一个字不写", BD.load(SID)["revision"] == before["revision"])

        # ── 10) 锁定字段：用户版本优先 + 结构身份字段禁锁 ────────────────────
        locked_path = "world.world_building.core_conflict"
        BD.update(SID, on_meta=lambda m: {**m, "locked_fields": [locked_path]})
        kept = BD.load(SID)["draft"]["world"]["world_building"]["core_conflict"]
        r = _save(world={**WORLD, "world_building": {
            **WORLD["world_building"], "core_conflict": "agent 想改成这句"}})
        now = BD.load(SID)["draft"]["world"]["world_building"]["core_conflict"]
        check("被锁字段仍是用户版本", now == kept, now[:40])
        check("冲突如实回报 protected_field_conflicts / ignored_agent_changes",
              any(c["path"] == locked_path for c in (r.get("protected_field_conflicts") or []))
              and r.get("ignored_agent_changes") == r.get("protected_field_conflicts"),
              str(r.get("protected_field_conflicts"))[:100])
        problems = PP.lockable_problems(
            ["storyline.plots[id=p1].start_word", "storyline.outlines[id=A].order",
             "characters[id=陆铮].id", locked_path],
            BD.load(SID)["draft"])
        check("结构身份字段一律拒绝锁定（id / order / start_word）",
              len(problems) == 3 and not any(locked_path in p for p in problems),
              str(problems))
    finally:
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))

    print("\n" + "=" * 60)
    print(f"  分阶段落盘验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
