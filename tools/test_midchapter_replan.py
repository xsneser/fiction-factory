#!/usr/bin/env python3
"""FSM regression: exhaust committed Plots before the chapter target, then replan."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main():
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    from libraries.write_flow import start_flow
    import agent_tools
    import libraries.dsh_bridge as bridge
    import libraries.replan_service as replan_service

    bm = BookManager(os.path.join(_ROOT, "books"))
    cfg = bm.create(title="Midchapter Replan 验收", pen_name="测试", chapter_count=5)
    bid = cfg.book_id
    original_env = {
        "NOVEL_WRITE_FLOW_ID": os.environ.get("NOVEL_WRITE_FLOW_ID"),
        "NOVEL_WRITE_CHILD_RUN_ID": os.environ.get("NOVEL_WRITE_CHILD_RUN_ID"),
    }
    original_run = bridge.run_dsh_task
    original_commit = replan_service.commit_replan_preview
    calls = []
    try:
        tl = BookStoryline(book_title="Midchapter Replan 验收", pen_name="测试",
                           phase="ready", words_per_chapter=3000)
        tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                          "identity": "调查员", "speech_profile": {}}]}
        tl.outlines = [OutlineSlot(id="a", template_id="", name="开局", start_word=0, end_word=6000)]
        tl.plots = [PlotSlot(id="p1", template_id="", name="初次转移", outline_id="a", words=1500,
                             order=1, roles=["顾衡"], protocol_version=2)]
        bm.save_storyline(bid, tl)

        flow = start_flow(bid, 1)
        os.environ["NOVEL_WRITE_FLOW_ID"] = flow["flow_id"]
        os.environ["NOVEL_WRITE_CHILD_RUN_ID"] = "writer:seed"
        prepared = agent_tools.prepare_plot_run(bid)
        agent_tools.save_plot_draft(prepared["run"]["commit_token"], "顾衡继续向前走。" * 100,
                                    outcome={"choices_made": ["继续前进"]})

        def fake_commit(book_id, preview_id, expected_revision):
            assert book_id == bid
            calls.append("replan_commit")
            current = bm.load_storyline(book_id)
            current.plots.append(PlotSlot(
                id="p2", template_id="", name="追查回声", outline_id="a", words=1500,
                order=2, roles=["顾衡"], protocol_version=2))
            bm.save_storyline(book_id, current)
            # 显式 commit_ok：FSM 只认这个字段（返回载荷里的 ok 是规划 UI 聚合的 ok）
            return {"commit_ok": True, "ok": True}

        def fake_run(task, history=None, debug=False, **kwargs):
            calls.append(("replan" if "续规划" in (task or "") else "write", kwargs))
            if "续规划" in (task or ""):
                # 计划器必须留下**基于当前版本**的预览，FSM 才会提交它（服务端不再替计划器臆造预览）
                from libraries.planning_state import save_replan_preview
                save_replan_preview(bid, {
                    "preview_id": "pv-midchapter", "expected_revision": 0,
                    "outlines": [], "plots": [],
                    "validation": {"passed": True, "problems": []},
                    "planning_patch": {},
                })
                yield {"type": "reply", "content": "续规划已准备。"}
                yield {"type": "done"}
                return
            assert "当前 Plot" in (task or "")
            flow_id = kwargs.get("flow_id") or flow["flow_id"]
            child_id = kwargs.get("child_run_id") or "writer:test"
            old_flow = os.environ.get("NOVEL_WRITE_FLOW_ID")
            old_child = os.environ.get("NOVEL_WRITE_CHILD_RUN_ID")
            os.environ["NOVEL_WRITE_FLOW_ID"] = flow_id
            os.environ["NOVEL_WRITE_CHILD_RUN_ID"] = child_id
            try:
                next_run = agent_tools.prepare_plot_run(bid)
                assert next_run["execution"]["id"] == "p2"
                result = agent_tools.save_plot_draft(
                    next_run["run"]["commit_token"], "顾衡继续追查。" * 600,
                    outcome={"information_revealed": ["回声来自旧工区"]})
            finally:
                if old_flow is None:
                    os.environ.pop("NOVEL_WRITE_FLOW_ID", None)
                else:
                    os.environ["NOVEL_WRITE_FLOW_ID"] = old_flow
                if old_child is None:
                    os.environ.pop("NOVEL_WRITE_CHILD_RUN_ID", None)
                else:
                    os.environ["NOVEL_WRITE_CHILD_RUN_ID"] = old_child
            yield {"type": "domain", "name": "plot_run_changed", "book_id": bid,
                   "flow_id": result.get("flow_id") or flow_id, "plot_id": "p2"}
            yield {"type": "reply", "content": "Plot 已完成。"}
            yield {"type": "done"}

        replan_service.commit_replan_preview = fake_commit
        bridge.run_dsh_task = fake_run
        events = list(bridge._writer_fsm(f"继续写 {bid}", None, False, "auto"))
        event_types = [e.get("type") for e in events]
        assert "chapter_changed" in [e.get("name") for e in events if e.get("type") == "domain"]
        assert "error" not in event_types, events
        assert "replan_commit" in calls
        assert any(isinstance(item, tuple) and item[0] == "write" for item in calls)
        book = bm.get(bid)
        assert int(getattr(book, "current_chapter", 0) or 0) == 1
        print("midchapter replan: OK")
    finally:
        bridge.run_dsh_task = original_run
        replan_service.commit_replan_preview = original_commit
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        bm.delete(bid)


if __name__ == "__main__":
    main()
