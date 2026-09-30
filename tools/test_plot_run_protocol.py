"""Plot-Run v3 验收：暂存事实、重新准备、章节 Delta 与未来消耗门禁。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def prose(n, name="顾衡"):
    unit = f"{name}握紧仪器，走廊的红灯闪了一次，他没有回头。"
    return (unit * (n // len(unit) + 1))[:n]


def main():
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    from agent_tools import prepare_plot_run, save_plot_draft, finalize_draft_chapter, chapter_quality_gate
    bm = BookManager(os.path.join(ROOT, "books"))
    cfg = bm.create(title="Plot Run 验收", pen_name="测试", chapter_count=20)
    bid = cfg.book_id
    try:
        tl = BookStoryline(book_title="Plot Run 验收", pen_name="测试", phase="ready", words_per_chapter=3000)
        tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                          "identity": "调查员", "speech_profile": {}}]}
        tl.outlines = [OutlineSlot(id="a", template_id="", name="开局", start_word=0, end_word=9000)]
        tl.plots = [
            PlotSlot(id="p1", template_id="", name="转移", outline_id="a", words=1500, order=1,
                     roles=["顾衡"], protocol_version=2),
            PlotSlot(id="p2", template_id="", name="追查", outline_id="a", words=1500, order=2,
                     roles=["顾衡"], protocol_version=2),
            PlotSlot(id="p3", template_id="", name="未来地点", outline_id="a", words=1500, order=3,
                     roles=["顾衡"], protocol_version=2,
                     expected_facts=[{"subject": "顾衡", "type": "location_shift", "expected_to": "终点", "strength": "must"}]),
        ]
        bm.save_storyline(bid, tl)
        import agent_tools as agent_tools_module
        original_picker = agent_tools_module.pick_plot_sample
        picks = []
        def counted_picker(book_id, query=None):
            picks.append((book_id, query))
            return {"ok": True, "text": "固定样文", "sample": {"id": ""},
                    "sample_receipt": {"sample_id": ""}}
        agent_tools_module.pick_plot_sample = counted_picker
        try:
            r1 = prepare_plot_run(bid)
            r1_retry = prepare_plot_run(bid)
        finally:
            agent_tools_module.pick_plot_sample = original_picker
        assert r1["execution"]["id"] == "p1" and not r1["previous_change"]
        assert r1_retry["run"]["commit_token"] == r1["run"]["commit_token"]
        assert len(picks) == 1
        t1 = prose(1300)
        original_builder = agent_tools_module._build_plot_run
        agent_tools_module._build_plot_run = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("save must not rebuild PlotRun"))
        try:
            save_plot_draft(r1["run"]["commit_token"], t1, "顾衡转移至终点并留下新的调查线索，准备继续追查异常信号的来源与去向，同时维持紧张的行动连续性，确认这次转移没有解除危机。",
                            character_events=[{"name": "顾衡", "events": [{"type": "location_shift", "to": "终点"}]}],
                            outcome={"choices_made": ["前往终点"]})
        finally:
            agent_tools_module._build_plot_run = original_builder
        r2 = prepare_plot_run(bid)
        assert r2["execution"]["id"] == "p2"
        assert r2["previous_change"]["plot_id"] == "p1"
        active = r2["cast"]["protagonists"][0]
        assert active["dyn"]["location"] == "终点" and active["state_source"] == "staged_fact"
        t2 = prose(1300)
        save_plot_draft(r2["run"]["commit_token"], t2, "顾衡沿着终点的异常痕迹继续追查，确认新的危险尚未解除，并为下一步调查留下明确的行动接口与悬念，同时让追查目标变得更加紧迫。", outcome={})
        committed = finalize_draft_chapter(bid)
        assert committed["chapter_delta"] and len(committed["chapter_delta"]["plot_deltas"]) == 2
        gate = chapter_quality_gate(bid, 1)
        assert gate["checks"]["future_consumption"]["passed"] is False
        print("plot-run protocol: OK")

        # ── 崩溃恢复：draft 已写、staged/accept 缺失时，重放同一令牌应补齐而不是报「不匹配」 ──
        import json
        from libraries import plot_commit_tokens as tokens_mod
        from libraries.plot_run_state import load_staged
        r3 = prepare_plot_run(bid)
        t3 = prose(1300)
        saved3 = save_plot_draft(r3["run"]["commit_token"], t3,
                                 "顾衡在终点确认了新的异常来源，并把线索固定下来，为下一步行动留出明确的接口、代价与时间压力，同时保持调查节奏不松。",
                                 outcome={})
        assert saved3.get("writer_run_complete") and not saved3.get("recovered_from_draft")
        # 回滚成「提交中断」：草稿桥保留，删掉 staged 事实与令牌 accepted 标记
        staged_path = os.path.join(ROOT, "books", bid, "staged_story_state.json")
        with open(staged_path, encoding="utf-8") as f:
            staged_before = json.load(f)
        assert staged_before.get("plot_deltas"), "前置条件：staged 事实已写入"
        os.remove(staged_path)
        state = tokens_mod._load(bid)
        rec = state["tokens"][r3["run"]["commit_token"]]
        rec["accepted"] = False
        rec.pop("result", None)
        rec.pop("accepted_at", None)
        tokens_mod._save(bid, state)
        replay = save_plot_draft(r3["run"]["commit_token"], t3,
                                 "顾衡在终点确认了新的异常来源，并把线索固定下来，为下一步行动留出明确的接口、代价与时间压力，同时保持调查节奏不松。",
                                 outcome={})
        assert replay.get("recovered_from_draft") is True, replay
        replays = [d for d in (load_staged(bid).get("plot_deltas") or []) if d.get("plot_id") == "p3"]
        assert replays, "恢复应把该情节段的 staged 事实补回来"
        assert tokens_mod.accepted_result(bid, r3["run"]["commit_token"]), "恢复应完成令牌签收"
        print("plot commit crash recovery: OK")
    finally:
        bm.delete(bid)


if __name__ == "__main__":
    main()
