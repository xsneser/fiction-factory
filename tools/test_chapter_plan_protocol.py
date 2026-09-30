#!/usr/bin/env python3
"""运行时章计划协议（I5 漂移约束 / I6 三元组 freshness / I7 计划不占租约）。

章计划把「打几个段落凑一章」从按字数阈值机械断章，变成主 Agent 的**显式决定**；服务端只守
硬边界。本测试锁定这些边界，尤其是最容易被绕过的那几条：

  · 选段必须是叙事顺序上的**连续前缀**（章计划不是改故事线的后门）；
  · 覆写某段目标字数受「类型带 + ≤1200 + 相对原计划 0.7~1.5 倍」三重约束
    （否则 `300→1200` 的「拉长一段凑章」会重新变成可能——那正是最初要修的问题）；
  · 改计划会让**旧 commit_token 失效**（版本向量含计划摘要）；
  · 收章的 freshness 是**三元组**：revision + draft_digest + chapter_plan_digest；
  · 计划住在 PLANNED flow 里且**不占写租约**（只规划不写作不会锁书）。

用法：python tools/test_chapter_plan_protocol.py
"""
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

SUMMARY = ("顾衡把当夜记录逐条摊开比对，确认回声的时间戳与值班表对不上，"
           "决定先不惊动老赵，而是连夜返回旧工区取证。")


def _seed(agent_tools, bm, title, plot_count, plot_words=800):
    from libraries.storyline import BookStoryline
    cfg = bm.create(title=title, pen_name="测试", chapter_count=4)
    bid = cfg.book_id
    tl = BookStoryline(book_title=title, pen_name="测试", phase="ready", words_per_chapter=3000)
    tl.basic_info = {"characters": [
        {"name": "顾衡", "role": "主角", "importance": 1, "identity": "调查员", "speech_profile": {}}]}
    bm.save_storyline(bid, tl)
    plots = [{"id": f"p{i}", "name": f"段{i}", "outline_id": "a1", "words": plot_words,
              "roles": ["顾衡"], "primary_turn": f"顾衡在第 {i} 步推进调查并发现新的矛盾点"}
             for i in range(1, plot_count + 1)]
    res = agent_tools.save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0,
                        "end_word": plot_words * plot_count}],
        plots=plots, mode="replace", expected_revision=0,
        planning_patch={"committed_until_word": plot_words * plot_count})
    assert res.get("ok"), res
    return bid


def _expect_raise(fn, needle, label):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        assert needle in str(exc), f"{label}: 报错不含「{needle}」→ {exc}"
        return
    raise AssertionError(f"{label}: 应被拒绝但没有")


def _plan(bid, agent_tools, plot_ids, target=3100, overrides=None, break_code="natural_closure"):
    state = agent_tools.get_orchestration_state(bid)
    return agent_tools.set_chapter_plan(
        bid, state["storyline_revision"], state["draft_digest"], {
            "chapter_num": state["chapter"]["chapter_num"], "plot_ids": list(plot_ids),
            "target_words": target,
            "break_reason": {"code": break_code, "note": "测试用"},
            "plot_word_targets": [{"plot_id": k, "target_words": v}
                                  for k, v in (overrides or {}).items()]})


def _write(agent_tools, bid, body):
    run = agent_tools.prepare_plot_run(bid)
    saved = agent_tools.save_plot_draft(run["run"]["commit_token"], body, plot_summary=SUMMARY,
                                       outcome={"information_revealed": ["推进调查"]})
    gate = saved["quality_gate"]
    receipt = agent_tools.record_plot_review(bid, str(run["execution"]["id"]),
                                             gate["gate_digest"], "accept")
    agent_tools.accept_plot_draft(bid, str(run["execution"]["id"]),
                                  gate_digest=gate["gate_digest"],
                                  review_receipt=receipt["receipt_id"])
    return run


def scenario_validation(agent_tools, bm):
    """服务端硬边界：能拒的都拒掉，且每条拒绝都对应一种绕过手法。"""
    bid = _seed(agent_tools, bm, "章计划校验", 4)
    try:
        # 没有计划时不许开写，且 advisory 必须预告
        state = agent_tools.get_orchestration_state(bid)
        opts = state["advisory"]["decision_options"]
        assert opts["write_next_plot"]["allowed"] is False, opts
        assert "CHAPTER_PLAN_REQUIRED_MISSING" in opts["write_next_plot"]["reasons"], opts
        _expect_raise(lambda: agent_tools.prepare_plot_run(bid),
                      "CHAPTER_PLAN_REQUIRED_MISSING", "无计划就开写")

        # CAS：陈旧 revision / 陈旧 digest
        _expect_raise(lambda: agent_tools.set_chapter_plan(
            bid, state["storyline_revision"] - 1, state["draft_digest"],
            {"chapter_num": state["chapter"]["chapter_num"], "plot_ids": ["p1"],
             "target_words": 3100}), "storyline_revision 已变化", "陈旧 revision 提交计划")
        _expect_raise(lambda: agent_tools.set_chapter_plan(
            bid, state["storyline_revision"], "deadbeef",
            {"chapter_num": state["chapter"]["chapter_num"], "plot_ids": ["p1"],
             "target_words": 3100}), "draft_digest 不匹配", "陈旧 digest 提交计划")

        # 跳序 / 乱序 / 重复 / 未知段
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1", "p3"]),
                      "连续前缀", "跳过已承诺的情节段")
        _expect_raise(lambda: _plan(bid, agent_tools, ["p2", "p1"]),
                      "连续前缀", "乱序选段")
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1", "p1"]), "重复", "重复选段")
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1", "pX"]), "不存在", "未知情节段")

        # 字数带
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1"], target=1000),
                      "低于落盘下限", "目标低于落盘下限")
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1", "p2", "p3"], target=5000),
                      "超过硬上限", "目标超硬上限")
        # 覆写越界：p1 原计划 800 → 合法漂移带 560~1200（×1.5 正好 1200）
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1", "p2", "p3"], overrides={"p1": 1300}),
                      "越界", "覆写超过绝对硬上限")
        _expect_raise(lambda: _plan(bid, agent_tools, ["p1", "p2", "p3"], overrides={"p1": 400}),
                      "越界", "覆写低于 0.7×原计划")

        # 合法的计划
        ok = _plan(bid, agent_tools, ["p1", "p2", "p3"], overrides={"p1": 1000})
        assert ok.get("ok"), ok
        assert ok["plan_digest"], ok
        assert ok["effective_plot_budgets"]["p1"]["source"] == "chapter_plan", ok
        assert ok["effective_plot_budgets"]["p1"]["assigned"] == 1000, ok
        assert ok["effective_plot_budgets"]["p2"]["assigned"] == 800, ok
        view = ok["chapter_plan"]
        assert view["plot_ids"] == ["p1", "p2", "p3"] and view["state"] == "active", view
        assert view["next_planned_plot_id"] == "p1", view

        # I7：计划 flow **不占写租约**，但状态里能读到它
        from libraries.write_flow import active_flow_id, load_flow, resolve_flow
        assert active_flow_id(bid) == "", "只做计划不该持有写租约"
        assert resolve_flow(bid) == ok["flow_id"], "计划 flow 必须能被 resolve_flow 找到"
        assert (load_flow(bid, ok["flow_id"]) or {}).get("phase") == "PLANNED"
        state2 = agent_tools.get_orchestration_state(bid)
        assert state2["flow_id"] == ok["flow_id"], state2["flow_id"]
        assert state2["chapter_plan"]["plan_digest"] == ok["plan_digest"], state2["chapter_plan"]
        assert state2["advisory"]["decision_options"]["write_next_plot"]["allowed"] is True

        # 改计划取代旧计划（不留分叉）
        plan2 = _plan(bid, agent_tools, ["p1", "p2", "p3", "p4"], overrides={"p1": 900})
        assert plan2["flow_id"] != ok["flow_id"], "新计划应另起 flow"
        assert (load_flow(bid, ok["flow_id"]) or {}).get("phase") == "SUPERSEDED"
        assert resolve_flow(bid) == plan2["flow_id"]

        print("[OK] 无计划不许开写 / CAS 陈旧即拒 / 跳序·乱序·重复·未知段全拒")
        print("[OK] 字数带与覆写三重约束（含 1300 超硬上限、400 低于 0.7×原计划）")
        print("[OK] I7：PLANNED flow 不占租约，但 resolve_flow 能选中它、状态里可见")
        print("[OK] 新计划取代旧计划（旧 flow 标 SUPERSEDED，不留分叉）")
        return bid
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)


def scenario_token_invalidation(agent_tools, bm):
    """改章计划 → 旧 commit_token 失效（否则「按 800 备好、改成 1000、旧 token 照交」）。"""
    bid = _seed(agent_tools, bm, "章计划令牌失效", 3)
    try:
        plan = _plan(bid, agent_tools, ["p1", "p2", "p3"])
        run = agent_tools.prepare_plot_run(bid)
        assigned = run["execution"]["word_budget"]["assigned"]
        assert assigned == 800, run["execution"]["word_budget"]
        # 计划改掉本段目标 → 版本向量里 chapter_plan_revision 变化
        _plan(bid, agent_tools, ["p1", "p2", "p3"], overrides={"p1": 1000})
        _expect_raise(lambda: agent_tools.save_plot_draft(
            run["run"]["commit_token"], "顾衡继续追查。" * 60, plot_summary=SUMMARY),
            "权威上下文已变化", "计划变更后仍用旧 token 提交")

        # 重新 prepare 拿到新目标与新 token，才是通的
        run2 = agent_tools.prepare_plot_run(bid)
        assert run2["execution"]["word_budget"]["assigned"] == 1000, run2["execution"]["word_budget"]
        assert run2["execution"]["word_budget"]["source"] == "chapter_plan"
        assert run2["run"]["commit_token"] != run["run"]["commit_token"]
        print("[OK] I5+token：改计划后旧 token 失效；重 prepare 拿到新的 assigned 目标")
        print(f"[OK] 计划摘要进版本向量（plan {plan['plan_digest'][:8]}… → 新 token 才可用）")
        return bid
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)


def scenario_full_chapter(agent_tools, bm):
    """按计划跑完一章：计划完成才能收章；收章后计划作废；三元组 freshness 生效。"""
    bid = _seed(agent_tools, bm, "章计划整章", 4)
    try:
        plan = _plan(bid, agent_tools, ["p1", "p2", "p3"], overrides={"p2": 1000})
        body = "顾衡继续追查真相，把当夜的每一份记录都摊在桌上逐一比对。" * 40
        for _ in range(3):
            state = agent_tools.get_orchestration_state(bid)
            assert state["advisory"]["recommended_action"] == "WRITE_NEXT_PLOT", state["advisory"]
            _write(agent_tools, bid, body)

        state = agent_tools.get_orchestration_state(bid)
        assert state["chapter_plan"]["state"] == "complete", state["chapter_plan"]
        assert str(state["chapter_plan"]["plan_digest"]) == str(plan["plan_digest"]), \
            "计划内容未变时摘要必须稳定"
        assert state["advisory"]["decision_options"]["finalize_chapter"]["allowed"] is True, \
            state["advisory"]
        assert state["flow_id"] == plan["flow_id"], "首次 Plot 提交应**接管**计划 flow（此刻才取租约）"

        # I6：计划的第三项 freshness
        _expect_raise(lambda: agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"], expected_chapter_plan_digest="deadbeef"),
            "章计划已变化", "陈旧 plan digest 收章")

        result = agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"],
            expected_chapter_plan_digest=state["chapter_plan"]["plan_digest"])
        assert result.get("ok"), result
        assert (bm.load_chapter(bid, 1) or {}).get("content", "").strip()
        # 计划是章级运行态：收章即作废
        assert agent_tools.get_orchestration_state(bid)["chapter_plan"] is None, "收章后计划应作废"
        from libraries.write_flow import active_flow_id
        assert active_flow_id(bid) == "", "收章后应释放租约"
        print("[OK] 整章按计划完成：3 段 → 计划 state=complete → 收章；flow 由 PLANNED 接管后收尾")
        print("[OK] I6：chapter_plan_digest 进收章 CAS（陈旧即拒）")
        print("[OK] 计划不跨章复用（收章后 chapter_plan 为 None，租约已释放）")
        return bid
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)


def scenario_incomplete_plan(agent_tools, bm):
    """计划未完成时不能收章——必须先把计划**显式改小**，把断章变成被记录的决定。"""
    bid = _seed(agent_tools, bm, "章计划未完成", 4)
    try:
        _plan(bid, agent_tools, ["p1", "p2", "p3", "p4"], target=3400)
        body = "顾衡继续追查真相，把当夜的每一份记录都摊在桌上逐一比对。" * 40
        for _ in range(3):
            _write(agent_tools, bid, body)
        state = agent_tools.get_orchestration_state(bid)
        assert state["chapter_plan"]["state"] == "active", state["chapter_plan"]
        assert state["advisory"]["decision_options"]["finalize_chapter"]["allowed"] is False
        assert "CHAPTER_PLAN_INCOMPLETE" in \
            state["advisory"]["decision_options"]["finalize_chapter"]["reasons"]
        _expect_raise(lambda: agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"],
            expected_chapter_plan_digest=state["chapter_plan"]["plan_digest"]),
            "CHAPTER_PLAN_INCOMPLETE", "计划未完成就收章")
        # 显式改小计划（去掉 p4）后即可收章
        smaller = _plan(bid, agent_tools, ["p1", "p2", "p3"])
        state = agent_tools.get_orchestration_state(bid)
        assert state["chapter_plan"]["state"] == "complete", state["chapter_plan"]
        result = agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"],
            expected_chapter_plan_digest=smaller["plan_digest"])
        assert result.get("ok"), result
        print("[OK] 计划未完成时收章被拒；显式改小计划后才放行（断章成为被记录的决定）")
        return bid
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)


def scenario_bands_unit():
    """I5 的类型带 + 漂移带交集（纯函数，含「拉长一段凑章」的反例）。"""
    from libraries.chapter_plan import allowed_assigned_range
    from libraries.storyline import PLOT_HARD_MAX

    class P:
        def __init__(self, words, category=""):
            self.words, self.category = words, category

    # 300 字的一段：漂移带 210~450 —— 1200 在**两道**约束下都不合法
    lo, hi = allowed_assigned_range(P(300), 300)
    assert (lo, hi) == (210, 450), (lo, hi)
    assert not (lo <= 1200 <= hi), "300→1200 的「拉长凑章」必须被拒"
    # 类型带比漂移带更窄时取交集（normal 450~700 ∩ 800×0.7~1.5 = 560~1200 → 560~700）
    assert allowed_assigned_range(P(800, "normal"), 800) == (560, 700)
    # 类型带比漂移带更宽时漂移带说了算（climax 850~1100 ∩ 770~1650 → 850~1100）
    assert allowed_assigned_range(P(1100, "climax"), 1100) == (850, 1100)
    # 上限永远受 PLOT_HARD_MAX
    assert allowed_assigned_range(P(2000), 2000)[1] <= PLOT_HARD_MAX
    print("[OK] I5 覆写区间：类型带 ∩ 漂移带 ∩ ≤1200（300→1200 的反例被钉死）")


def main():
    from libraries.book_manager import BookManager
    import agent_tools

    bm = BookManager(os.path.join(_ROOT, "books"))
    prev = {k: os.environ.get(k) for k in ("NOVEL_REVIEW_GATE", "NOVEL_CHAPTER_PLAN_REQUIRED")}
    os.environ["NOVEL_REVIEW_GATE"] = "1"
    os.environ["NOVEL_CHAPTER_PLAN_REQUIRED"] = "1"
    try:
        scenario_bands_unit()
        scenario_validation(agent_tools, bm)
        scenario_token_invalidation(agent_tools, bm)
        scenario_full_chapter(agent_tools, bm)
        scenario_incomplete_plan(agent_tools, bm)
        print("\n  ✅ 运行时章计划协议验收通过")
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    main()
