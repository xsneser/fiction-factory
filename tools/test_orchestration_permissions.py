#!/usr/bin/env python3
"""编排授权真源回归（不变量 I2）—— advisory 与真实守卫**不许漂移**。

主 Agent 的调度自由建立在「服务端说能做、就真能做；说不能做、调用必被拒」之上。
一旦 `get_orchestration_state.advisory.decision_options` 与各 mutation 工具的守卫
各写一套判断，就会出现两种极难排查的失败：

  · 界面说 allowed，调用被拒 → 模型把整轮预算花在重试上（2026-09-10 的
    「可见命令 >> 可执行命令」事故同型）；
  · 界面说拒绝，实际放行 → 真正的护栏失效，且没人会去查。

本测试把「同一 facts 下两者结论必须一致」钉死：对每个动作、每种事实场景，
`compute_orchestration_permissions(facts)[a]["allowed"]` 必须与
`require_permission(a, facts)` 是否抛错完全一致。

用法：python tools/test_orchestration_permissions.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries import orchestration_policy as OP  # noqa: E402


def _facts(**over) -> dict:
    """一份「什么都没发生」的最小事实，再按场景覆盖。"""
    base = {
        "book_id": "book_x", "phase": "ready", "storyline_revision": 3, "review_gate": True,
        "chapter_num": 2, "draft_has_bridges": False, "draft_digest": "d0",
        "draft_words": 0, "bridge_plot_ids": [], "all_draft_plots_exist": True,
        "pending_review_plot": "", "last_bridge": None, "next_plot": None,
        "chapter_status": {"written_words": 0, "commit_floor": 1800, "chapter_ready": False},
        "boundary": {"needs_replan": False, "reason_codes": []},
        "receipt": None, "plan": None, "plan_enabled": False, "plan_required": False,
        "budget": {"actions_used": 0, "actions_max": 24, "revise_used": 0, "revise_max": 2,
                   "replan_used": 0, "replan_max": 2},
    }
    base.update(over)
    return base


def _bridge(gate="g1", blocking=0, state="pending"):
    return {"plot_id": "p1", "is_last": True, "review_state": state, "text_digest": "t1",
            "gate_digest": gate, "gate_passed": True, "blocking_hard_issue_count": blocking}


def _receipt(verdict="accept", valid=True):
    return {"receipt_id": "rr_1", "verdict": verdict, "valid": valid, "rewrite_brief": {}}


def _preview(exists=True, passed=True, expected_revision=3):
    return {"exists": exists, "preview_id": "pv_1" if exists else "",
            "expected_revision": expected_revision, "validation_passed": passed}


SCENARIOS = {
    # 空草稿但没有 replan 预览 → 续规划当然还不能提交（缺 preview 是独立原因）
    "空草稿（无预览）": (_facts(next_plot={"id": "p1", "planned_words": 700,
                                      "chapter_break_after": "allowed"}),
                   {"write_next_plot": True, "review_plot": False, "record_review": False,
                    "accept_plot": False, "revise_plot": False, "finalize_chapter": False,
                    "replan": False}),
    # 空草稿 + 有效预览 → 续规划可以提交
    "空草稿（有有效预览）": (_facts(
        next_plot={"id": "p1", "planned_words": 700, "chapter_break_after": "allowed"},
        preview=_preview()),
        {"replan": True, "write_next_plot": True}),
    # 预览校验未过 / 预览陈旧 → 都不许提交（advisory 提前说清差什么）
    "预览校验未过": (_facts(preview=_preview(passed=False)), {"replan": False}),
    "预览陈旧": (_facts(preview=_preview(expected_revision=2)), {"replan": False}),
    "末段待评审（无凭据）": (_facts(
        draft_has_bridges=True, draft_words=900, bridge_plot_ids=["p1"],
        pending_review_plot="p1", last_bridge=_bridge(),
        chapter_status={"written_words": 900, "commit_floor": 1800, "chapter_ready": False}),
        {"write_next_plot": False, "review_plot": True, "record_review": True,
         "accept_plot": False, "revise_plot": True, "finalize_chapter": False, "replan": False}),
    "末段待评审（有 accept 凭据）": (_facts(
        draft_has_bridges=True, draft_words=1900, bridge_plot_ids=["p1"],
        pending_review_plot="p1", last_bridge=_bridge(), receipt=_receipt("accept"),
        chapter_status={"written_words": 1900, "commit_floor": 1800, "chapter_ready": False}),
        {"accept_plot": True}),
    "凭据要求改稿": (_facts(
        draft_has_bridges=True, draft_words=1900, bridge_plot_ids=["p1"], pending_review_plot="p1",
        last_bridge=_bridge(), receipt=_receipt("revise_text")),
        {"accept_plot": False, "revise_plot": True}),
    "硬门禁未过": (_facts(
        draft_has_bridges=True, draft_words=1900, bridge_plot_ids=["p1"], pending_review_plot="p1",
        last_bridge=_bridge(blocking=2), receipt=_receipt("accept")),
        {"accept_plot": False}),
    "低于落盘下限": (_facts(
        draft_has_bridges=True, draft_words=400, bridge_plot_ids=["p1"],
        last_bridge=_bridge(state="accepted"),
        chapter_status={"written_words": 400, "commit_floor": 1800, "chapter_ready": False}),
        {"finalize_chapter": False, "write_next_plot": False}),
    "可以收章": (_facts(
        draft_has_bridges=True, draft_words=2800, bridge_plot_ids=["p1"],
        last_bridge=_bridge(state="accepted"),
        chapter_status={"written_words": 2800, "commit_floor": 1800, "chapter_ready": True}),
        {"finalize_chapter": True, "write_next_plot": False}),
    "草稿里有过期 Plot": (_facts(
        draft_has_bridges=True, draft_words=2800, bridge_plot_ids=["gone"],
        last_bridge=_bridge(state="accepted"), all_draft_plots_exist=False,
        chapter_status={"written_words": 2800, "commit_floor": 1800, "chapter_ready": True}),
        {"finalize_chapter": False}),
    "预算耗尽": (_facts(
        next_plot={"id": "p1", "planned_words": 700, "chapter_break_after": "allowed"},
        last_bridge=_bridge(), preview=_preview(),
        budget={"actions_used": 24, "actions_max": 24, "revise_used": 2, "revise_max": 2,
                "replan_used": 2, "replan_max": 2}),
        {"write_next_plot": False, "revise_plot": False, "replan": False}),
    "章计划未启用": (_facts(preview=_preview()), {"plan_chapter": False, "replan": True}),
}


def main():
    # ① 每个动作键都必须出现在授权输出里（漏一个键 = 某条通路没有真源）
    sample = OP.compute_orchestration_permissions(_facts())
    missing = sorted(set(OP.ORCHESTRATION_ACTIONS) - set(sample))
    assert not missing, f"授权输出缺动作键: {missing}"

    # ② 场景矩阵：期望的 allowed 必须一致，且 require_permission 与它结论相同
    for label, (facts, expect) in SCENARIOS.items():
        perms = OP.compute_orchestration_permissions(facts)
        for action, want in expect.items():
            got = perms[action]["allowed"]
            assert got is want, f"{label} / {action}: 期望 allowed={want}，实际 {got}（{perms[action]}）"
        # ③ 漂移护栏：逐动作比对「advisory 的结论」与「守卫是否抛错」
        for action in OP.ORCHESTRATION_ACTIONS:
            allowed = perms[action]["allowed"]
            raised, msg = False, ""
            try:
                OP.require_permission(action, facts)
            except RuntimeError as exc:
                raised, msg = True, str(exc)
            assert raised is (not allowed), (
                f"{label} / {action}: advisory.allowed={allowed} 但 require_permission "
                f"{'抛错' if raised else '放行'} —— 告知与守卫已漂移")
            if not allowed:
                for reason in perms[action]["reasons"]:
                    assert reason in msg, f"{label} / {action}: 拒绝消息缺 reason 码 {reason}：{msg}"

    # ④ 拒绝对非法的动作名（防拼写错误静默放过）
    try:
        OP.require_permission("write_plot", _facts())
        raise AssertionError("未知动作应被拒")
    except ValueError:
        pass

    # ⑤ 排序/字数助手只有一份实现：agent_tools 的别名必须指向策略模块
    import agent_tools as AT
    from libraries.storyline import BookStoryline
    tl = BookStoryline(book_title="排序口径", pen_name="测试", phase="ready")
    assert AT._ordered_plots(tl) == OP.ordered_plots(tl)
    assert AT._next_plot(tl, {}) is OP.next_plot(tl, {})
    assert AT._draft_plot_ids({"bridges": [{"plot_id": "a"}]}) == {"a"}

    # ⑥ 推荐动作：纯建议，且必须落在「允许的动作」里（建议不能诱导非法动作）
    for label, (facts, _expect) in SCENARIOS.items():
        perms = OP.compute_orchestration_permissions(facts)
        rec = OP.recommend_action(facts, perms)
        assert rec["action"], label
        if rec["action"] == "WRITE_NEXT_PLOT":
            assert perms["write_next_plot"]["allowed"], f"{label}: 推荐写下一段但实际不被允许"
        if rec["action"] == "FINALIZE_CHAPTER":
            assert perms["finalize_chapter"]["allowed"], f"{label}: 推荐收章但实际不被允许"
        if rec["action"] == "REPLAN":
            assert perms["replan"]["allowed"], f"{label}: 推荐续规划但实际不被允许"

    print("[OK] 授权真源：%d 个动作键齐备，%d 个场景的 allowed 与期望一致"
          % (len(OP.ORCHESTRATION_ACTIONS), len(SCENARIOS)))
    print("[OK] 漂移护栏：每个场景的 advisory 结论 == require_permission 的实际裁决")
    print("[OK] 拒绝消息携带与 advisory 完全相同的 reason 码")
    print("[OK] 排序/字数助手单一份实现（agent_tools 别名 == orchestration_policy）")
    print("[OK] 推荐动作永远落在被允许的动作集合内")


if __name__ == "__main__":
    main()
