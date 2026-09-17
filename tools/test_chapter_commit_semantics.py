#!/usr/bin/env python3
"""批次3b 回归：收章的顺序与失败语义（W8/W9） + 续规划批次/阈值同源（P2）。

锁定：
  1. 章正文落盘是**不可回退**的提交点；其后的质量门禁异常只作诊断，绝不能让调用方以为
     「章没提交」，也绝不能漏掉 flow DONE 与租约释放（此前 DONE/release 在门禁之后）。
  2. 权威状态（故事线 written_chapter/storyline_revision）更新失败必须显式上报
     （ChapterCommittedStateError → state_error），不能静默——静默会重写同一情节段、
     并让旧 replan preview 看起来仍新鲜。
  3. 章节字数硬下限独立于审查器：审查器异常不得变成「跳过下限」（过短正文不落盘）。
  4. 续规划批大小与「剩余多少就续规划」阈值同源（REPLAN_BATCH_*）。

用法：python tools/test_chapter_commit_semantics.py
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _seed(bm, agent_tools, title, word_units=4400, created=None):
    """建书 + 弧 + 1 个情节段，并把该情节段的一段超下限正文写进草稿。"""
    from libraries.storyline import BookStoryline
    from ui.web_blueprints.ctx import book_mgr
    cfg = bm.create(title=title, pen_name="测试", chapter_count=4)
    bid = cfg.book_id
    created.append(bid)
    tl = BookStoryline(book_title=title, pen_name="测试", phase="ready", words_per_chapter=3000)
    tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                     "identity": "调查员", "speech_profile": {}}]}
    book_mgr.save_storyline(bid, tl)
    res = agent_tools.save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0, "end_word": 3000}],
        plots=[{"id": "p1", "name": "段1", "outline_id": "a1", "words": 1000, "roles": ["顾衡"],
                "primary_turn": "顾衡在旧工区找到转移装置仍在运转的证据"}],
        mode="replace", expected_revision=0, planning_patch={"committed_until_word": 3000})
    assert res.get("ok"), res
    run = agent_tools.prepare_plot_run(bid)
    repeat = max(1, word_units // 7)
    agent_tools.save_plot_draft(run["run"]["commit_token"], "顾衡继续追查真相。" * repeat,
                                plot_summary="顾衡在旧工区追查回声的来处，确认了转移装置仍在运转。",
                                outcome={"information_revealed": ["回声来自旧工区"]})
    return bid


def main():
    from libraries.book_manager import BookManager
    from libraries.planning_state import REPLAN_MAX_PLOTS, replan_low_plot_threshold
    from libraries.write_flow import active_flow_id, load_flow, start_flow
    import agent_tools
    from ui.web_blueprints.ctx import book_mgr

    bm = BookManager(os.path.join(_ROOT, "books"))
    created = []          # 只删本测试建的书（绝不按 book_00N 通配删——会误删真实书稿）
    orig_gate = agent_tools.chapter_quality_gate
    orig_save_tl = book_mgr.save_storyline      # 必须补在 agent_tools 真正用的那个实例上
    try:
        # ── 1) 门禁异常不影响「章已提交」，且 flow 一定 DONE + 租约释放 ──
        bid = _seed(bm, agent_tools, "收章语义验收", created=created)
        flow = {"flow_id": active_flow_id(bid) or start_flow(bid, 1)["flow_id"]}
        def boom(*_a, **_k):
            raise RuntimeError("门禁内部炸了")
        agent_tools.chapter_quality_gate = boom
        result = agent_tools.finalize_draft_chapter(bid, flow["flow_id"])
        assert result.get("ok") and result.get("chapter") == 1, result
        assert result["quality_gate"]["skipped"] is True, result["quality_gate"]
        assert os.path.exists(os.path.join(_ROOT, "books", bid, "chapters", "0001.json")), "章必须在盘上"
        assert not os.path.exists(os.path.join(_ROOT, "books", bid, "draft_chapter.json")), "草稿应已清"
        assert load_flow(bid, flow["flow_id"])["phase"] == "DONE", load_flow(bid, flow["flow_id"])
        assert active_flow_id(bid) == "", "收章后必须释放租约"
        agent_tools.chapter_quality_gate = orig_gate
        print("[OK] 门禁异常只作诊断：章已提交 + flow DONE + 租约释放")

        # ── 2) 权威状态更新失败 → 显式 state_error（章确实已提交），且只 bump 一次 revision ──
        bid2 = _seed(bm, agent_tools, "权威状态失败验收", created=created)
        flow2 = {"flow_id": active_flow_id(bid2) or start_flow(bid2, 1)["flow_id"]}
        revision_before = int(bm.load_storyline(bid2).storyline_revision)
        calls = {"n": 0}
        def flaky_save_storyline(book_id, tl):
            calls["n"] += 1
            raise OSError("故事线写不进去")
        book_mgr.save_storyline = flaky_save_storyline
        try:
            result2 = agent_tools.finalize_draft_chapter(bid2, flow2["flow_id"])
        finally:
            book_mgr.save_storyline = orig_save_tl
        assert result2.get("ok") and result2.get("state_error"), result2
        assert calls["n"] >= 2, f"权威状态更新应重试一次（实际 {calls['n']} 次）"
        assert os.path.exists(os.path.join(_ROOT, "books", bid2, "chapters", "0001.json"))
        assert int(bm.load_storyline(bid2).storyline_revision) == revision_before, "未写成功就不该 bump"
        f2 = load_flow(bid2, flow2["flow_id"])
        assert f2["phase"] == "DONE" and f2.get("error"), f2
        assert active_flow_id(bid2) == "", "状态失败也要释放租约"
        print("[OK] 故事线进度更新失败 → state_error 上报 + 重试一次 + flow 留痕 + 释放租约")

        # ── 3) 过短正文不落盘（且不依赖审查器是否可用）──
        bid3 = _seed(bm, agent_tools, "字数下限验收", word_units=900, created=created)
        flow3 = {"flow_id": active_flow_id(bid3) or start_flow(bid3, 1)["flow_id"]}
        try:
            agent_tools.finalize_draft_chapter(bid3, flow3["flow_id"])
            raise AssertionError("过短正文竟然落盘了")
        except RuntimeError as e:
            assert "下限" in str(e), e
        assert not os.path.exists(os.path.join(_ROOT, "books", bid3, "chapters", "0001.json")), \
            "未达下限不应写章节文件"
        print("[OK] 字数硬下限独立于审查器（过短正文不落盘）")

        # ── 4) 续规划阈值与目标同源（2026-09-11：单位从段数改为**承诺字数**）──
        from libraries.planning_state import (REPLAN_MIN_PLOTS, REPLAN_MIN_REMAINING_WORDS,
                                              REPLAN_TARGET_WORDS, detect_story_boundary)
        assert replan_low_plot_threshold() == REPLAN_MIN_PLOTS == 2, replan_low_plot_threshold()
        assert REPLAN_MIN_PLOTS < REPLAN_TARGET_WORDS, "阈值必须远小于批次目标，否则每批都要立刻续规划"
        assert REPLAN_MIN_REMAINING_WORDS < REPLAN_TARGET_WORDS, "余量阈值必须小于批次目标"
        common = dict(committed_until_word=100000, words_per_batch=3000, storyline_revision=1,
                      last_replan={})
        # 余量充足 + 段数充足 → 不触发
        assert detect_story_boundary(written_until_word=0, remaining_plots=REPLAN_MIN_PLOTS + 5,
                                     **common)["needs_replan"] is False
        # 段数落到下限 → PLOTS_LOW
        assert "PLOTS_LOW" in detect_story_boundary(
            written_until_word=0, remaining_plots=replan_low_plot_threshold(), **common)["reason_codes"]
        # 承诺余量落到字数阈值 → WORDS_LOW（段数充足，排除 PLOTS_LOW 干扰）
        _wd = dict(committed_until_word=5000, remaining_plots=20, storyline_revision=1, last_replan={})
        assert "WORDS_LOW" in detect_story_boundary(
            written_until_word=5000 - REPLAN_MIN_REMAINING_WORDS, **_wd)["reason_codes"]
        assert "WORDS_LOW" not in detect_story_boundary(
            written_until_word=5000 - REPLAN_MIN_REMAINING_WORDS * 2, **_wd)["reason_codes"]
        # 预览校验用同一批大小：低于下限 / 高于上限都被拒
        rev_now = int(bm.load_storyline(bid).storyline_revision)   # 必须与当前版本一致（否则先被版本校验拒）
        # horizon 按承诺字数控制，段数只作**安全上限**（planning_state.REPLAN_MAX_PLOTS）。
        for bad in (0, REPLAN_MAX_PLOTS + 1):
            try:
                agent_tools.drive_ui("set_replan_preview", {
                    "book_id": bid, "expected_revision": rev_now,
                    "diagnosis": {}, "directions": [{"id": "d1", "title": "t"}, {"id": "d2", "title": "t"}],
                    "selected_direction_id": "d1",
                    "outlines": [{"id": "a9", "name": "弧", "start_word": 0, "end_word": 100}],
                    "plots": [{"id": f"q{i}", "name": "段", "outline_id": "a9", "words": 10,
                               "primary_turn": f"第{i}转", "roles": ["顾衡"]} for i in range(bad)],
                    "planning_patch": {
                        "horizon": {"h1": [{"title": "下一步", "arc_intent": "推进冲突"}]},
                        "future_intents": [{"title": "远期方向", "intent": "扩大冲突"}],
                    },
                })
                raise AssertionError(f"{bad} 个 plots 竟然被接受")
            except RuntimeError as e:
                assert str(REPLAN_MAX_PLOTS) in str(e) or "非空" in str(e), e
        print(f"[OK] plots 段数上限 {REPLAN_MAX_PLOTS} 与阈值 {replan_low_plot_threshold()} 同源，越界被拒")

        # ── 5) 混合协议收章（v1 段 + v2 段同章）：canonical 集合必须是**全部草稿段** ──
        # 只比 v2 子集会让混合章永远收不了章；而一旦侥幸相等，v1 段正文会被静默丢掉。
        mix_bid = _seed(bm, agent_tools, "混合协议收章验收", created=created)
        draft_path = os.path.join(bm.dir, mix_bid, "draft_chapter.json")
        with open(draft_path, encoding="utf-8") as f:
            draft = json.load(f)
        assert draft.get("bridges"), draft
        draft["bridges"][0]["protocol_version"] = 1          # 旧段：v1
        for seg in draft["bridges"][1:]:
            seg["protocol_version"] = 2                      # 同章后续段：v2
        draft["bridges"].append({**draft["bridges"][0], "plot_id": "p2", "protocol_version": 2,
                                 "text": "第二段正文用于验证混合协议不丢字。" * 60})
        with open(draft_path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False)
        segs = [{"plot_id": b.get("plot_id"), "plot_name": b.get("plot_name"),
                 "text": b.get("text") or ""} for b in draft["bridges"]]
        try:
            agent_tools.save_chapter_text(mix_bid, int(draft["chapter_num"]),
                                          "\n\n".join(x["text"] for x in segs),
                                          plot_segments=segs[:1])          # 少提交一段
            raise AssertionError("混合协议下提交段子集竟然被接受")
        except RuntimeError as e:
            assert "plot_segments" in str(e), e
        ok = agent_tools.save_chapter_text(mix_bid, int(draft["chapter_num"]),
                                           "\n\n".join(x["text"] for x in segs),
                                           plot_segments=segs)
        assert ok.get("ok"), ok
        saved = json.load(open(os.path.join(bm.dir, mix_bid, "chapters",
                                            f"{int(draft['chapter_num']):04d}.json"), encoding="utf-8"))
        content = saved.get("content") or ""
        for seg in segs:
            head = (seg["text"] or "")[:24]
            assert head and head in content, f"混合协议收章丢了段：{seg['plot_id']}"
        print("[OK] 混合协议（v1+v2）收章：全量段提交成功且不丢字，子集提交仍被拒")
    finally:
        agent_tools.chapter_quality_gate = orig_gate
        book_mgr.save_storyline = orig_save_tl
        for leftover in created:
            try:
                bm.delete(leftover)
            except Exception:
                pass


if __name__ == "__main__":
    main()
