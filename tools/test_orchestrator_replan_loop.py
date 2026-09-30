#!/usr/bin/env python3
"""编排路径的续规划闭环（I4 与未收章草稿互斥 / I8 续规划预算）——无 LLM。

主 Agent 在边界处委派 Planner 生成 preview，再由自己**按策略提交**（`commit_replan_preview`
是编排面的提交口，与书详情页的 commit-plan 端点是同一个 service）。本测试锁定两件
最容易出事的语义：

  I4：草稿里还有没结算的段落时，**禁止**提交 replan。完整 replan 可能删改当前情节段，
      已写的正文会变成孤儿。此时 Critic 判「结构问题」只能让主 Agent 停下报告。
  I8：续规划尝试次数**跨章累计**（续规划是书级动作，按章重置等于没有上限）。

用法：python tools/test_orchestrator_replan_loop.py
"""
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

SUMMARY = ("顾衡把当夜记录逐条摊开比对，确认回声的时间戳与值班表对不上，"
           "决定先不惊动老赵，而是连夜返回旧工区取证。")
BODY = "顾衡继续追查真相，把当夜的每一份记录都摊在桌上逐一比对。" * 40


def _seed(agent_tools, bm, title):
    from libraries.storyline import BookStoryline
    cfg = bm.create(title=title, pen_name="测试", chapter_count=8)
    bid = cfg.book_id
    tl = BookStoryline(book_title=title, pen_name="测试", phase="ready", words_per_chapter=3000)
    tl.basic_info = {"characters": [
        {"name": "顾衡", "role": "主角", "importance": 1, "identity": "调查员", "speech_profile": {}}]}
    bm.save_storyline(bid, tl)
    res = agent_tools.save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0, "end_word": 1600}],
        plots=[{"id": "p1", "name": "段1", "outline_id": "a1", "words": 800, "roles": ["顾衡"],
                "primary_turn": "顾衡发现时间戳的矛盾"},
               {"id": "p2", "name": "段2", "outline_id": "a1", "words": 800, "roles": ["顾衡"],
                "primary_turn": "顾衡确认装置仍在运转"}],
        mode="replace", expected_revision=0, planning_patch={"committed_until_word": 1600})
    assert res.get("ok"), res
    return bid


def _expect_raise(fn, needle, label):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        assert needle in str(exc), f"{label}: 报错不含「{needle}」→ {exc}"
        return
    raise AssertionError(f"{label}: 应被拒绝但没有")


def _write_and_accept(agent_tools, bid):
    run = agent_tools.prepare_plot_run(bid)
    pid = str(run["execution"]["id"])
    saved = agent_tools.save_plot_draft(run["run"]["commit_token"], BODY, plot_summary=SUMMARY,
                                       outcome={"information_revealed": ["推进调查"]})
    gate = saved["quality_gate"]
    receipt = agent_tools.record_plot_review(bid, pid, gate["gate_digest"], "accept")
    return agent_tools.accept_plot_draft(bid, pid, gate_digest=gate["gate_digest"],
                                         review_receipt=receipt["receipt_id"])


def _make_preview(bm, agent_tools, bid, preview_id, arc_id, plot_ids, base_word):
    """造一个能通过**动态复核**的 replan preview（等价于 Planner 产出 + set_replan_preview 暂存）。

    注意 `validation.passed=True` 只是预览写入时的快照：提交端还会按当前模型**重新**校验
    （H1 形状、新段 roles、弧跨度…）。这里必须真的给出合法载荷，不能靠伪造快照蒙过去。
    """
    from libraries.planning_state import save_replan_preview
    rev = agent_tools.get_orchestration_state(bid)["storyline_revision"]
    return save_replan_preview(bid, {
        "preview_id": preview_id, "expected_revision": rev,
        "outlines": [{"id": arc_id, "name": "第二弧", "start_word": base_word,
                      "end_word": base_word + 800 * len(plot_ids)}],
        "plots": [{"id": pid, "name": f"新段{pid}", "outline_id": arc_id, "words": 800,
                   "roles": ["顾衡"],
                   "primary_turn": f"{pid} 的主要戏剧变化"} for pid in plot_ids],
        "planning_patch": {"committed_until_word": base_word + 800 * len(plot_ids),
                           "horizon": {"h1": [{"title": "近期方向",
                                               "arc_intent": "承接当前压力继续推进"}]},
                           "future_intents": [{"title": "更远的威胁", "intent": "更远的威胁"}]},
        "validation": {"passed": True, "problems": []}})


def main():
    from libraries.book_manager import BookManager
    import agent_tools
    from libraries import orchestration_budget as BUD

    bm = BookManager(os.path.join(_ROOT, "books"))
    prev = {k: os.environ.get(k) for k in
            ("NOVEL_REVIEW_GATE", "NOVEL_CHAPTER_PLAN_REQUIRED", "MAX_REPLAN_ATTEMPTS_PER_RUN")}
    os.environ["NOVEL_REVIEW_GATE"] = "1"
    os.environ.pop("NOVEL_CHAPTER_PLAN_REQUIRED", None)   # 本测试聚焦 replan，不叠章计划
    os.environ["MAX_REPLAN_ATTEMPTS_PER_RUN"] = "2"       # 一次给单元检查、一次给真实提交
    bid = _seed(agent_tools, bm, "续规划闭环验收")
    try:
        # ── I4：草稿未结算时禁止提交 replan ──
        _write_and_accept(agent_tools, bid)          # 写 p1 并接受（草稿里仍有未收章的 p1）
        from libraries.planning_state import delete_replan_preview, load_replan_preview
        _make_preview(bm, agent_tools, bid, "pv_mid", "a2", ["p3"], 1600)
        state = agent_tools.get_orchestration_state(bid)
        opts = state["advisory"]["decision_options"]["replan"]
        assert opts["allowed"] is False, opts
        assert "UNACCEPTED_DRAFT_PRESENT" in opts["reasons"], opts
        _expect_raise(lambda: agent_tools.commit_replan_preview(
            bid, "pv_mid", state["storyline_revision"]),
            "UNACCEPTED_DRAFT_PRESENT", "带未收章草稿提交 replan")
        assert load_replan_preview(bid), "被拒的提交不该吞掉 preview"
        assert agent_tools.load_tl(bid).storyline_revision == state["storyline_revision"], \
            "被拒的提交不该改动正式故事线"
        print("[OK] I4：草稿未收章时 replan 提交被拒（preview 与正式故事线均不变）")

        # ── 预算单元语义：actions/revise 换章归零，replan 跨章累计 ──
        BUD.bump(bid, 1, "actions"); BUD.bump(bid, 1, "revise", "p1")
        BUD.bump(bid, 1, "replan")
        snap_after = BUD.snapshot(bid, 2, "p1")
        assert snap_after["actions_used"] == 0 and snap_after["revise_used"] == 0, snap_after
        assert snap_after["replan_used"] == 1, snap_after
        assert snap_after["replan_remaining"] == 1, snap_after
        print("[OK] I8 计数语义：actions/revise 换章归零，replan 跨章累计（书级动作）")

        # ── 收章清空草稿后，replan 才可以提交 ──
        delete_replan_preview(bid, "pv_mid")
        state = agent_tools.get_orchestration_state(bid)
        _write_and_accept(agent_tools, bid)          # 写 p2，让本章够字数
        state = agent_tools.get_orchestration_state(bid)
        assert state["advisory"]["decision_options"]["finalize_chapter"]["allowed"] is True, \
            state["advisory"]
        agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"])
        rev_before = agent_tools.load_tl(bid).storyline_revision
        assert agent_tools._draft_read(bid) in (None, {}), "收章后草稿应已清空"

        _make_preview(bm, agent_tools, bid, "pv_boundary", "a2", ["p3", "p4"], 1600)
        state = agent_tools.get_orchestration_state(bid)
        opts = state["advisory"]["decision_options"]["replan"]
        assert opts["allowed"] is True, opts
        assert state["boundary"]["needs_replan"] is True, state["boundary"]
        result = agent_tools.commit_replan_preview(bid, "pv_boundary",
                                                   state["storyline_revision"])
        assert result.get("commit_ok"), result
        tl_now = agent_tools.load_tl(bid)
        assert tl_now.storyline_revision == rev_before + 1, "提交应 bump 一次 revision"
        assert any(p.id == "p3" for p in tl_now.plots), "新情节段应已进入正式故事线"
        assert load_replan_preview(bid) is None, "提交成功后 preview 应被删除"
        # 新的可写段已经出现（旧边界被推远）
        state = agent_tools.get_orchestration_state(bid)
        assert state["current_plot"]["id"] == "p3", state["current_plot"]
        print("[OK] 边界续规划：preview 原子提交 → revision+1 → 新情节段可写 → preview 已删除")

        # ── I8：预算耗尽后（跨章累计）第二次提交被拒 ──
        assert state["limits"]["replan_remaining"] == 0, state["limits"]
        _make_preview(bm, agent_tools, bid, "pv_again", "a3", ["p5"], 3200)
        state = agent_tools.get_orchestration_state(bid)
        opts = state["advisory"]["decision_options"]["replan"]
        assert opts["allowed"] is False, opts
        assert "REPLAN_BUDGET_EXHAUSTED" in opts["reasons"], opts
        _expect_raise(lambda: agent_tools.commit_replan_preview(
            bid, "pv_again", state["storyline_revision"]),
            "REPLAN_BUDGET_EXHAUSTED", "超出续规划预算")
        print("[OK] I8：超出续规划预算后服务端硬拒（advisory 提前预告同一 reason 码）")
        print("\n  ✅ 编排续规划闭环验收通过")
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    main()
