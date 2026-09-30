#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中章失败后的**可恢复性**回归（无 LLM，按协议逐动作模拟）。

背景（第 16 章那次）：Writer 子 run 因上游空回复（EMPTY_RESPONSE）失败时，旧代码只
yield 一句 error + done 就 return——flow 停在中途、租约白占 15 分钟，用户还被告知
「Writer 未完成有效 Plot 提交」。改成走 `_fail_flow` 之后必须证明两件事**同时**成立：

  1. 失败是干净的：flow=FAILED、租约已释放、失败原因写在 flow.error 上；
  2. 失败是**可恢复的**：已 accepted 的段落原样保留、能重新拿到租约、从下一个未完成
     段落继续，最后仍能正常收章——正文里必须有失败前写的那两段。

第 2 条是这次改造最要紧的持久化保证：把 failure 处理做严，很容易顺手把「章节恢复」
一起做死（flow 已 FAILED 就再也推不动，或者重新建 flow 时把草稿清掉）。

用法：python tools/test_writer_failure_recovery.py
"""
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLOT_WORDS = 800
BODY = "顾衡继续追查真相，把当夜的每一份记录都摊在桌上逐一比对。" * 40   # ≈1000 字
SUMMARY = ("顾衡把当夜记录逐条摊开比对，确认回声的时间戳与值班表对不上，"
           "决定先不惊动老赵，而是连夜返回旧工区取证。")
FAIL_RAW = "EMPTY_RESPONSE: model returned a completed response with no content"


def _seed(agent_tools, bm, title, plot_count):
    """建一本 phase=ready 的书：1 弧 + N 个连续情节段。"""
    from libraries.storyline import BookStoryline
    cfg = bm.create(title=title, pen_name="测试", chapter_count=4)
    bid = cfg.book_id
    tl = BookStoryline(book_title=title, pen_name="测试", phase="ready", words_per_chapter=3000)
    tl.basic_info = {"characters": [
        {"name": "顾衡", "role": "主角", "importance": 1, "identity": "调查员",
         "speech_profile": {}},
        {"name": "老赵", "role": "配角", "importance": 2, "identity": "线人",
         "speech_profile": {}}]}
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
    receipt = agent_tools.record_plot_review(bid, plot_id, gate["gate_digest"], "accept",
                                             issues=[{"code": "PACE", "severity": "warning",
                                                      "summary": "节奏可再紧一点"}])
    accepted = agent_tools.accept_plot_draft(bid, plot_id, gate_digest=gate["gate_digest"],
                                             review_receipt=receipt["receipt_id"])
    assert accepted["review"]["state"] == "accepted", accepted
    return plot_id


def main():
    from libraries.book_manager import BookManager
    from libraries import dsh_bridge as DB
    from libraries.write_flow import active_flow_id, load_flow
    import agent_tools

    bm = BookManager(os.path.join(_ROOT, "books"))
    prev_gate = os.environ.get("NOVEL_REVIEW_GATE")
    os.environ["NOVEL_REVIEW_GATE"] = "1"
    bid = _seed(agent_tools, bm, "中章失败恢复验收", 4)
    try:
        # ── 1. 正常写出并接受前两段 ──
        written = [_write_and_accept(agent_tools, bid) for _ in range(2)]
        assert written == ["p1", "p2"], written
        state = agent_tools.get_orchestration_state(bid)
        flow_id = state["flow_id"]
        assert flow_id, state
        draft_before = agent_tools._draft_read(bid) or {}
        bridges_before = [b.get("plot_id") for b in draft_before.get("bridges") or []]
        assert bridges_before == ["p1", "p2"], bridges_before
        chapter_num = int(draft_before.get("chapter_num") or 1)
        rev_before = int(state["storyline_revision"])
        lease_flow = active_flow_id(bid)
        assert lease_flow == flow_id, (lease_flow, flow_id)

        # ── 2. 第三段的 Writer 子 run 以 EMPTY_RESPONSE 失败：走**真实**失败出口 ──
        raw = FAIL_RAW
        msg = DB._writer_failure_message(raw, "", bid)
        assert "诊断提示" in msg and "EMPTY_RESPONSE" in msg, msg
        assert "未完成有效 Plot 提交" not in msg, msg
        events = list(DB._fail_flow(bid, flow_id, f"writer_failed:{DB._llm_failure_code(raw)}", msg))
        assert [e.get("type") for e in events] == ["error", "done"], events
        assert "诊断提示" in events[0]["message"], events[0]

        # ── 3. 失败是干净的 ──
        flow_after = load_flow(bid, flow_id) or {}
        assert flow_after.get("phase") == "FAILED", flow_after.get("phase")
        assert "EMPTY_RESPONSE" in str(flow_after.get("error") or ""), flow_after.get("error")
        assert active_flow_id(bid) == "", "失败后必须释放租约（否则下一次写作被它吸住）"

        # ── 4. 失败没牵动已写内容 ──
        draft_after = agent_tools._draft_read(bid) or {}
        assert [b.get("plot_id") for b in draft_after.get("bridges") or []] == ["p1", "p2"], draft_after
        assert int(draft_after.get("chapter_num") or 0) == chapter_num
        tl = agent_tools.load_tl(bid)
        assert int(tl.storyline_revision) == rev_before, "失败不该 bump revision"
        assert not (bm.load_chapter(bid, chapter_num) or {}).get("content"), "失败时不该有章节落盘"

        # ── 5. 上游恢复后再次执行本章：从第三段继续，不重写前两段 ──
        resumed = _write_and_accept(agent_tools, bid)
        assert resumed == "p3", f"应从 p3 继续，实际 {resumed}"
        new_lease = active_flow_id(bid)
        assert new_lease and new_lease != flow_id, f"应重新拿到租约（新 flow），实际 {new_lease}"
        draft_resumed = agent_tools._draft_read(bid) or {}
        assert [b.get("plot_id") for b in draft_resumed.get("bridges") or []] == ["p1", "p2", "p3"], \
            draft_resumed

        # ── 6. 恢复后仍能正常收章，且正文包含失败前写的那两段 ──
        state = agent_tools.get_orchestration_state(bid)
        assert state["advisory"]["decision_options"]["finalize_chapter"]["allowed"], state["advisory"]
        result = agent_tools.finalize_draft_chapter(
            bid, state["flow_id"], expected_revision=state["storyline_revision"],
            expected_draft_digest=state["draft_digest"])
        assert result.get("ok"), result
        chapter = bm.load_chapter(bid, chapter_num) or {}
        assert chapter.get("content", "").strip(), "恢复后必须能落盘"
        tl = agent_tools.load_tl(bid)
        for pid in ("p1", "p2", "p3"):
            plot = next(p for p in tl.plots if p.id == pid)
            assert int(plot.written_chapter) == chapter_num, f"{pid} 应归入第 {chapter_num} 章"
        assert active_flow_id(bid) == "", "收章后租约释放"

        print("[OK] 中章失败：flow=FAILED + 租约释放 + 失败原因留在 flow.error")
        print("[OK] 既有草稿不被牵动：bridges 仍是 p1,p2，revision 未 bump，章节未落盘")
        print("[OK] 恢复：重新拿到租约、从 p3 继续、前两段未被重写")
        print("[OK] 恢复后正常收章，正文包含失败前写的那两段")
        print("\n  ✅ 中章失败后可恢复（不卡死、不丢草稿）")
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", bid), ignore_errors=True)
        if prev_gate is None:
            os.environ.pop("NOVEL_REVIEW_GATE", None)
        else:
            os.environ["NOVEL_REVIEW_GATE"] = prev_gate


if __name__ == "__main__":
    main()
