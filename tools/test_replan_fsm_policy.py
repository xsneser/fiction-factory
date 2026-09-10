#!/usr/bin/env python3
"""批次2 回归：续规划的策略分支、重试上限与提交判据。

覆盖：
  A. P3 `set_replan_preview` 必须基于当前故事线版本（陈旧预览早失败，不拖到 commit）。
  B. P4/P5/P1 `_commit_pending_replan`：revision=0 也能提交（旧代码 `0 or -1` → 必失败）；
     commit 成功判据是显式 `commit_ok`；提交后服务端写入 `planning_state.last_replan`。
  C. W4 confirm 策略：写作 run 撞边界时**先让计划器生成预览**再停 WAIT_CONFIRM，
     且不提交；再次触发时复用同一预览、不重复起计划器。
  D. W5 续规划连败有上限（不再无限起计划器会话），失败统一释放租约。
  E. auto 策略端到端：撞边界 → 计划器产出预览 → 自动原子提交 → 继续写 → 收章 DONE。

用法：python tools/test_replan_fsm_policy.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PEN = "测试"


def _new_book(bm, title):
    cfg = bm.create(title=title, pen_name=PEN, chapter_count=8)
    return cfg.book_id


def _bootstrap_storyline(bid):
    from libraries.storyline import BookStoryline
    from ui.web_blueprints.ctx import book_mgr
    book = book_mgr.get(bid)
    tl = BookStoryline(book_title=book.title, pen_name=book.pen_name,
                       phase="ready", words_per_chapter=3000)
    # 协议 v2 的角色严格契约：情节段 roles 必须能在角色 bible 里找到
    tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                     "identity": "调查员", "speech_profile": {}}]}
    book_mgr.save_storyline(bid, tl)


def _seed_first_arc(bid):
    """建一个已承诺到 3000 字的弧 + 1 个情节段，并把它标成已写（→ 无剩余可写 Plot）。"""
    import agent_tools
    from ui.web_blueprints.ctx import book_mgr
    _bootstrap_storyline(bid)
    res = agent_tools.save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0, "end_word": 3000}],
        plots=[{"id": "p1", "name": "段1", "outline_id": "a1", "words": 1000, "roles": ["顾衡"]}],
        mode="replace", expected_revision=0,
        planning_patch={"committed_until_word": 3000})
    assert res.get("ok"), res
    tl = book_mgr.load_storyline(bid)
    for p in tl.plots:
        if p.id == "p1":
            p.written_chapter = 1
    book_mgr.save_storyline(bid, tl)          # 不 bump revision（模拟已收章的历史）
    return int(tl.storyline_revision)


def _preview_args(bid, revision, next_plot_id=2):
    """一份能通过 set_replan_preview 全部校验的预览参数（3 个情节段 + 新叶弧）。"""
    return {
        "book_id": bid, "expected_revision": revision,
        "diagnosis": {"current_pressure": "承诺区将尽", "reader_question": "他能否守住？"},
        "directions": [{"id": "d1", "title": "迎战"}, {"id": "d2", "title": "撤退"}],
        "selected_direction_id": "d1",
        "outlines": [{"id": "a2", "name": "第二弧", "start_word": 3000, "end_word": 6000}],
        "plots": [{"id": f"p{i}", "name": f"段{i}", "outline_id": "a2", "words": 1000,
                   "roles": ["顾衡"]}
                  for i in range(next_plot_id, next_plot_id + 3)],
        # 故意塞一个假的 last_replan：服务端必须以自己算的为准覆盖它
        "planning_patch": {"committed_until_word": 6000,
                           "last_replan": {"from_revision": 999, "reason_codes": ["BOGUS"]}},
    }


def main():
    import agent_tools
    import libraries.dsh_bridge as bridge
    from libraries.book_manager import BookManager
    from libraries.planning_state import load_planning_state, load_replan_preview
    from libraries.write_flow import active_flow_id, load_flow, start_flow

    bm = BookManager(os.path.join(_ROOT, "books"))
    original_run = bridge.run_dsh_task
    env_keys = ("NOVEL_WRITE_FLOW_ID", "NOVEL_WRITE_CHILD_RUN_ID")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    bid = None
    try:
        bid = _new_book(bm, "续规划策略验收")
        revision = _seed_first_arc(bid)
        assert revision == 1, revision

        # ── A. 陈旧 expected_revision 必须在暂存预览阶段就被拒绝 ──
        try:
            agent_tools.drive_ui("set_replan_preview", _preview_args(bid, revision + 5))
            raise AssertionError("陈旧 revision 的预览竟然被接受")
        except RuntimeError as e:
            assert "版本" in str(e), e
        assert not load_replan_preview(bid), "被拒绝的预览不应落盘"

        # ── B. 预览落盘 + 元数据提交（revision 0 的边界由独立书验证，见 B2）──
        agent_tools.drive_ui("set_replan_preview", _preview_args(bid, revision))
        preview = load_replan_preview(bid)
        assert preview and preview["expected_revision"] == revision, preview
        committed = bridge._commit_pending_replan(bid)
        assert committed.get("commit_ok") is True, committed
        assert not load_replan_preview(bid), "提交成功后应删除预览"
        tl_after = bm.load_storyline(bid)
        assert int(tl_after.storyline_revision) == revision + 1, tl_after.storyline_revision
        assert [p.id for p in tl_after.plots] == ["p1", "p2", "p3", "p4"], [p.id for p in tl_after.plots]
        state = load_planning_state(bid, tl_after, bm.get(bid), persist=False)
        last = state.get("last_replan") or {}
        assert last.get("from_revision") == revision, last
        assert last.get("reason_codes") and last["reason_codes"] != ["BOGUS"], last
        assert last.get("preview_id") == preview["preview_id"], last
        print("[OK] A/B 预览版本早校验 + 元数据提交（commit_ok + 服务端 last_replan）")

        # ── B2. revision=0 的自动提交（旧代码 `0 or -1` 必失败）──
        zero_bid = _new_book(bm, "revision0 提交验收")
        try:
            _bootstrap_storyline(zero_bid)     # 只要故事线，不建弧 → revision 保持 0
            zero = bridge._commit_pending_replan(zero_bid)
            assert zero.get("commit_ok") is False and zero.get("error") == "no_pending_preview", zero
            agent_tools.drive_ui("set_replan_preview", {
                "book_id": zero_bid, "expected_revision": 0,
                "diagnosis": {"current_pressure": "开篇"},
                "directions": [{"id": "d1", "title": "进"}, {"id": "d2", "title": "退"}],
                "selected_direction_id": "d1",
                "outlines": [{"id": "z1", "name": "开局弧", "start_word": 0, "end_word": 3000}],
                "plots": [{"id": f"z{i}", "name": f"开篇{i}", "outline_id": "z1", "words": 1000,
                           "roles": ["顾衡"]}
                          for i in range(1, 4)],
                "planning_patch": {"committed_until_word": 3000},
            })
            zero_commit = bridge._commit_pending_replan(zero_bid)
            assert zero_commit.get("commit_ok") is True, zero_commit
            assert int(bm.load_storyline(zero_bid).storyline_revision) == 1
            print("[OK] B2 revision=0 的自动提交成功（0 不再被当成缺省值）")
        finally:
            bm.delete(zero_bid)

        # ── C. confirm 策略：先出预览再停，不提交；复用时不再起计划器 ──
        confirm_bid = _new_book(bm, "confirm 策略验收")
        try:
            crevision = _seed_first_arc(confirm_bid)
            cflow = start_flow(confirm_bid, 1)
            planner_calls = []

            def fake_run_confirm(task, history=None, debug=False, **kwargs):
                if "续规划" in (task or ""):
                    planner_calls.append(task)
                    assert "请勿自行提交" in task, task      # confirm 文案必须禁止自提交
                    agent_tools.drive_ui("set_replan_preview", _preview_args(confirm_bid, crevision))
                    yield {"type": "reply", "content": "预览已暂存，等待确认。"}
                    yield {"type": "done"}
                    return
                raise AssertionError(f"confirm 策略不应启动 Writer 子 run：{task}")

            bridge.run_dsh_task = fake_run_confirm
            events = list(bridge._writer_fsm(f"继续写 {confirm_bid}", None, False, "confirm"))
            assert "error" not in [e.get("type") for e in events], events
            assert "chapter_changed" not in [e.get("name") for e in events if e.get("type") == "domain"]
            assert len(planner_calls) == 1, planner_calls
            flow = load_flow(confirm_bid, cflow["flow_id"])
            assert flow["phase"] == "WAIT_CONFIRM", flow
            cpreview = load_replan_preview(confirm_bid)
            assert cpreview and flow["replan_state"].get("preview_id") == cpreview["preview_id"], flow
            assert int(bm.load_storyline(confirm_bid).storyline_revision) == crevision, "confirm 不应提交"
            # 再次触发：复用在途预览，不重复起计划器
            events2 = list(bridge._writer_fsm(f"继续写 {confirm_bid}", None, False, "confirm"))
            assert len(planner_calls) == 1, planner_calls
            assert load_flow(confirm_bid, cflow["flow_id"])["phase"] == "WAIT_CONFIRM"
            assert not [e for e in events2 if e.get("type") == "error"], events2
            print("[OK] C confirm 策略先生成预览再停 + 复用不重复起计划器")
        finally:
            bm.delete(confirm_bid)

        # ── D. 连败上限：计划器始终不产出预览 → 有限重试后 FAILED 且释放租约 ──
        cap_bid = _new_book(bm, "续规划上限验收")
        try:
            _seed_first_arc(cap_bid)
            cap_flow = start_flow(cap_bid, 1)
            cap_calls = []

            def fake_run_no_preview(task, history=None, debug=False, **kwargs):
                cap_calls.append(task)
                yield {"type": "reply", "content": "什么都没产出。"}
                yield {"type": "done"}

            bridge.run_dsh_task = fake_run_no_preview
            events = list(bridge._writer_fsm(f"继续写 {cap_bid}", None, False, "auto"))
            errors = [e for e in events if e.get("type") == "error"]
            assert errors and "续规划" in errors[0]["message"], errors
            assert len(cap_calls) == bridge.MAX_REPLAN_ATTEMPTS, (len(cap_calls), bridge.MAX_REPLAN_ATTEMPTS)
            capped = load_flow(cap_bid, cap_flow["flow_id"])
            assert capped["phase"] == "FAILED" and capped["error"] == "replan_attempts_exhausted", capped
            assert active_flow_id(cap_bid) == "", "失败后必须释放写作租约"
            print(f"[OK] D 续规划连败上限 = {bridge.MAX_REPLAN_ATTEMPTS} 轮，失败释放租约")
        finally:
            bm.delete(cap_bid)

        # ── E. auto 端到端：撞边界 → 计划器 → 自动提交 → 继续写 → 收章 DONE ──
        e2e_bid = _new_book(bm, "auto 续写端到端验收")
        try:
            erevision = _seed_first_arc(e2e_bid)
            eflow = start_flow(e2e_bid, 1)
            calls = []

            def fake_run(task, history=None, debug=False, **kwargs):
                if "续规划" in (task or ""):
                    calls.append("replan")
                    agent_tools.drive_ui("set_replan_preview", _preview_args(e2e_bid, erevision))
                    yield {"type": "reply", "content": "预览已暂存。"}
                    yield {"type": "done"}
                    return
                calls.append("write")
                flow_id = kwargs.get("flow_id") or eflow["flow_id"]
                child_id = kwargs.get("child_run_id") or "writer:test"
                old = {k: os.environ.get(k) for k in env_keys}
                os.environ["NOVEL_WRITE_FLOW_ID"] = flow_id
                os.environ["NOVEL_WRITE_CHILD_RUN_ID"] = child_id
                try:
                    run = agent_tools.prepare_plot_run(e2e_bid)
                    assert run["execution"]["id"] == "p2", run["execution"]["id"]
                    saved = agent_tools.save_plot_draft(
                        run["run"]["commit_token"], "顾衡继续追查。" * 620,
                        outcome={"information_revealed": ["第二弧开启"]})
                finally:
                    for k, v in old.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                yield {"type": "domain", "name": "plot_run_changed", "book_id": e2e_bid,
                       "flow_id": saved.get("flow_id") or flow_id, "plot_id": "p2"}
                yield {"type": "reply", "content": "Plot 已完成。"}
                yield {"type": "done"}

            bridge.run_dsh_task = fake_run
            events = list(bridge._writer_fsm(f"继续写 {e2e_bid}", None, False, "auto"))
            kinds = [e.get("type") for e in events]
            assert "error" not in kinds, events
            assert "chapter_changed" in [e.get("name") for e in events if e.get("type") == "domain"], events
            assert calls.count("replan") == 1 and "write" in calls, calls
            assert int(bm.load_storyline(e2e_bid).storyline_revision) == erevision + 2, \
                "续规划提交 + 收章各 bump 一次"
            assert active_flow_id(e2e_bid) == "", "收章后应释放租约"
            print("[OK] E auto 撞边界 → 续规划 → 提交 → 继续写 → 收章 DONE")
        finally:
            bm.delete(e2e_bid)
    finally:
        bridge.run_dsh_task = original_run
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if bid:
            bm.delete(bid)


if __name__ == "__main__":
    main()
