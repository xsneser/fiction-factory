#!/usr/bin/env python3
"""主 Agent 单章编排闭环（**无 LLM**，按协议逐动作模拟 root Agent）。

这是阶段一的里程碑测试：证明主 Agent 在**不进入服务端 `_legacy_writer_fsm` 循环**的前提下，
能完整跑完一章：

    读状态 → Writer 写一段 → 体检 → Critic 记录判决（拿 receipt）→ accept
    →（重复）→ 收章（带 CAS 三元组）

同时锁定三件调度期新出现的硬约束：
  · 收章必须带 `storyline_revision` + `draft_digest`，任一陈旧即拒（I6 的 CAS 前提）；
  · 改稿次数由服务端硬拒，不再只是 skill 里的一句话（I8）；
  · 未调 `record_plot_review` 就没有凭据，accept 无法完成（I1）。

用法：python tools/test_orchestrator_chapter_loop.py
"""
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PLOT_WORDS = 800
BODY = "顾衡继续追查真相，把当夜的每一份记录都摊在桌上逐一比对。" * 40   # ≈1000 字
SUMMARY = ("顾衡把当夜记录逐条摊开比对，确认回声的时间戳与值班表对不上，"
           "决定先不惊动老赵，而是连夜返回旧工区取证。")


def _seed(agent_tools, bm, title, plot_count):
    """建一本 phase=ready 的书：1 弧 + N 个连续情节段。"""
    from libraries.storyline import BookStoryline
    cfg = bm.create(title=title, pen_name="测试", chapter_count=4)
    bid = cfg.book_id
    tl = BookStoryline(book_title=title, pen_name="测试", phase="ready", words_per_chapter=3000)
    tl.basic_info = {"characters": [
        {"name": "顾衡", "role": "主角", "importance": 1, "identity": "调查员", "speech_profile": {}},
        {"name": "老赵", "role": "配角", "importance": 2, "identity": "线人", "speech_profile": {}}]}
    bm.save_storyline(bid, tl)
    plots = [{"id": f"p{i}", "name": f"段{i}", "outline_id": "a1", "words": PLOT_WORDS,
              "roles": ["顾衡"], "primary_turn": f"顾衡在第 {i} 步推进调查并发现新的矛盾点"}
             for i in range(1, plot_count + 1)]
    res = agent_tools.save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0,
                        "end_word": PLOT_WORDS * plot_count}],
        plots=plots, mode="replace", expected_revision=0,
        planning_patch={"committed_until_word": PLOT_WORDS * plot_count})
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
    """Writer 写一段（模拟）→ 体检 → Critic 记录判决 → root 接受。返回 plot_id。"""
    run = agent_tools.prepare_plot_run(bid)
    assert run.get("ok") and run.get("run"), run
    plot_id = str(run["execution"]["id"])
    saved = agent_tools.save_plot_draft(
        run["run"]["commit_token"], BODY, plot_summary=SUMMARY,
        outcome={"information_revealed": [f"{plot_id} 推进了调查"]})
    assert saved.get("ok"), saved
    gate = saved["quality_gate"]
    assert gate.get("gate_digest"), gate
    # 没有凭据就接受不了（I1）——这是每条通路都必须经过的一步
    _expect_raise(lambda: agent_tools.accept_plot_draft(
        bid, plot_id, gate_digest=gate["gate_digest"]),
        "必须凭 review_receipt", f"{plot_id} 无凭据 accept")
    receipt = agent_tools.record_plot_review(bid, plot_id, gate["gate_digest"], "accept",
                                             issues=[{"code": "PACE", "severity": "warning",
                                                      "summary": "节奏可再紧一点"}])
    accepted = agent_tools.accept_plot_draft(bid, plot_id, gate_digest=gate["gate_digest"],
                                             review_receipt=receipt["receipt_id"])
    assert accepted["review"]["state"] == "accepted", accepted
    return plot_id


def scenario_single_chapter(agent_tools, bm):
    """主线：root **跟随 advisory 的推荐动作**逐段推进，直到在合法章边界收章。

    刻意跟随 `recommended_action`（而不是「一看到能收章就收」）：收章的下限只是
    `commit_floor`（0.6×目标），2 段就够格落盘——但那一章的篇幅明显偏短。合法动作的
    选择权正是主 Agent 的自由所在，这里模拟一个「守规矩且不偷懒」的 root。
    """
    bid = _seed(agent_tools, bm, "编排闭环验收", 4)
    try:
        written, last_status = [], {}
        for _ in range(8):        # 上限 8 次迭代（防死循环把测试挂住）
            state = agent_tools.get_orchestration_state(bid)
            opts = state["advisory"]["decision_options"]
            action = state["advisory"]["recommended_action"]
            last_status = state["advisory"]["legacy_fsm"]["chapter_status"]
            if action == "FINALIZE_CHAPTER":
                assert opts["finalize_chapter"]["allowed"], state["advisory"]
                break
            if action != "WRITE_NEXT_PLOT":
                raise AssertionError(f"未预期的推荐动作 {action}：{state['advisory']}")
            assert opts["write_next_plot"]["allowed"], state["advisory"]
            written.append(_write_and_accept(agent_tools, bid))
        else:
            raise AssertionError("循环未收敛到可收章状态")

        state = agent_tools.get_orchestration_state(bid)
        assert state["advisory"]["decision_options"]["finalize_chapter"]["allowed"], state["advisory"]
        assert state["chapter"]["written_words"] >= state["chapter"]["commit_floor"], state["chapter"]
        # 收章发生在**合法章边界**上（不是「刚够落盘下限就草草断章」）
        assert last_status.get("chapter_ready") is True, last_status
        assert len(written) >= 2, written

        # ① CAS：陈旧 revision / 陈旧 digest 都不许收章
        _expect_raise(lambda: agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"] - 1,
            expected_draft_digest=state["draft_digest"]),
            "storyline_revision 已变化", "陈旧 revision 收章")
        _expect_raise(lambda: agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest="deadbeef"),
            "草稿已变化", "陈旧 digest 收章")
        # ② 编排路径必须带 CAS（缺参即拒），不许「不带参数就放行」
        _expect_raise(lambda: agent_tools.finalize_draft_chapter(bid, state["flow_id"]),
                      "必须带 expected_revision", "无 CAS 收章")

        rev_before = state["storyline_revision"]
        result = agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"])
        assert result.get("ok"), result
        assert result.get("chapter") == 1, result

        # ③ 落盘后的事实
        chapter = bm.load_chapter(bid, 1) or {}
        assert chapter.get("content", "").strip(), "章节正文必须落盘"
        assert agent_tools._draft_read(bid) in (None, {}), "收章后草稿必须被清空"
        tl = agent_tools.load_tl(bid)
        for pid in written:
            plot = next(p for p in tl.plots if p.id == pid)
            assert int(plot.written_chapter) == 1, f"{pid} 的 written_chapter 应为 1"
        assert int(tl.storyline_revision) == rev_before + 1, "收章只应 bump 一次 revision"

        # ④ flow 收尾：DONE + 租约释放
        from libraries.write_flow import active_flow_id, load_flow
        flow = load_flow(bid, state["flow_id"]) or {}
        assert flow.get("phase") == "DONE", flow.get("phase")
        assert active_flow_id(bid) == "", "收章后必须释放写作租约"

        print(f"[OK] 单章闭环：{len(written)} 段全部 root 调度完成（{written}），"
              f"章边界原因={last_status.get('reason')}，章内 {state['chapter']['written_words']} 字")
        print("[OK] 收章 CAS 三元组：陈旧 revision / 陈旧 digest / 缺参都被拒")
        print("[OK] 落盘事实：章节存在、草稿清空、written_chapter 正确、revision 只 bump 一次")
        print("[OK] flow DONE 且租约已释放")
        return bid
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)


def scenario_revise_budget(agent_tools, bm):
    """改稿预算硬拒（I8）：上限 2 次，第 3 次必须被服务端拒。"""
    bid = _seed(agent_tools, bm, "改稿预算验收", 1)
    try:
        _write_and_accept(agent_tools, bid)
        for i in (1, 2):
            rev = agent_tools.prepare_plot_revision(
                bid, "p1", rewrite_brief={"goals": [f"第 {i} 轮收紧节奏"]})
            saved = agent_tools.save_plot_revision(
                rev["revision_token"], BODY + f"（第 {i} 轮修订）", plot_summary=SUMMARY)
            assert saved.get("ok"), saved
            gate = saved["quality_gate"]
            receipt = agent_tools.record_plot_review(bid, "p1", gate["gate_digest"], "accept")
            agent_tools.accept_plot_draft(bid, "p1", gate_digest=gate["gate_digest"],
                                          review_receipt=receipt["receipt_id"])
        state = agent_tools.get_orchestration_state(bid)
        budget = state["budget"]
        assert budget["revise_used"] == 2 and budget["revise_remaining"] == 0, budget
        _expect_raise(lambda: agent_tools.prepare_plot_revision(
            bid, "p1", rewrite_brief={"goals": ["再改一轮"]}),
            "REVISE_BUDGET_EXHAUSTED", "超出改稿预算")
        # 授权真源必须已经预告了这一点（advisory 与守卫同源）
        assert state["advisory"]["decision_options"]["revise_plot"]["allowed"] is False, state["advisory"]
        print("[OK] 改稿预算：服务端硬拒第 3 次改稿，且 advisory 已提前预告")
        return bid
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)


def main():
    from libraries.book_manager import BookManager
    import agent_tools

    bm = BookManager(os.path.join(_ROOT, "books"))
    prev_gate = os.environ.get("NOVEL_REVIEW_GATE")
    os.environ["NOVEL_REVIEW_GATE"] = "1"     # 编排路径注入的门禁
    try:
        scenario_single_chapter(agent_tools, bm)
        scenario_revise_budget(agent_tools, bm)
        print("\n  ✅ 主 Agent 单章编排闭环通过")
    finally:
        if prev_gate is None:
            os.environ.pop("NOVEL_REVIEW_GATE", None)
        else:
            os.environ["NOVEL_REVIEW_GATE"] = prev_gate


if __name__ == "__main__":
    main()
