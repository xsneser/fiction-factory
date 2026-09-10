#!/usr/bin/env python3
"""Style snapshot consistency tests for prepared Plot Runs."""
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def test_unrelated_sample_does_not_stale():
    from libraries import style_samples
    from libraries.style_samples import StyleSample
    from libraries.style_snapshot import (build_snapshot, rendered_sample_digest,
                                           selected_sample_digest, snapshot_matches)

    profile = SimpleNamespace(id="profile-test", sample_ids=[], sample_books=[])
    samples = [StyleSample(id="a", text="样本文本 A", title="A")]
    original_pool_for = style_samples.pool_for
    try:
        style_samples.pool_for = lambda _profile: list(samples)
        rendered = style_samples.render_reference([samples[0]])
        digest = rendered_sample_digest(rendered)
        snapshot = build_snapshot(profile, "笔名：测试", {
            "sample_id": "a", "content_digest": digest,
        })
        samples.append(StyleSample(id="z", text="无关样本文本 Z", title="Z"))
        assert selected_sample_digest(profile, "a") == digest
        assert snapshot_matches(profile, "笔名：测试", snapshot, digest)
        samples[0].text = "替换后的样本文本 A"
        replaced_digest = selected_sample_digest(profile, "a")
        assert replaced_digest != digest
        assert not snapshot_matches(profile, "笔名：测试", snapshot, replaced_digest)
        samples[:] = [samples[1]]
        assert selected_sample_digest(profile, "a") is None
    finally:
        style_samples.pool_for = original_pool_for


def test_prepare_save_style_stale():
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    import agent_tools

    bm = BookManager(os.path.join(_ROOT, "books"))
    cfg = bm.create(title="Style Snapshot 验收", pen_name="测试", chapter_count=5)
    bid = cfg.book_id
    original_profile_for = agent_tools._profile_for
    original_picker = agent_tools.pick_plot_sample
    state = {"card": "笔名：测试｜短句"}
    fake_profile = SimpleNamespace(
        id="profile-test",
        build_style_card=lambda: state["card"],
    )
    try:
        tl = BookStoryline(book_title="Style Snapshot 验收", pen_name="测试",
                           phase="ready", words_per_chapter=3000)
        tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                          "identity": "调查员", "speech_profile": {}}]}
        tl.outlines = [OutlineSlot(id="a", template_id="", name="开局", start_word=0, end_word=3000)]
        tl.plots = [PlotSlot(id="p1", template_id="", name="转移", outline_id="a", words=1500,
                             order=1, roles=["顾衡"], protocol_version=2)]
        bm.save_storyline(bid, tl)
        agent_tools._profile_for = lambda _tl: fake_profile
        agent_tools.pick_plot_sample = lambda _book_id, query=None: {
            "ok": False, "error": "test: no sample"
        }
        prepared = agent_tools.prepare_plot_run(bid)
        state["card"] = "笔名：测试｜长句"
        try:
            agent_tools.save_plot_draft(prepared["run"]["commit_token"], "顾衡走进走廊。")
        except RuntimeError as exc:
            assert "风格上下文已失效" in str(exc)
        else:
            raise AssertionError("style mutation must stale the prepared token")

        state["card"] = "笔名：测试｜短句"
        saved = agent_tools.save_plot_draft(prepared["run"]["commit_token"], "顾衡走进走廊。")
        assert saved["writer_run_complete"] is True
    finally:
        agent_tools._profile_for = original_profile_for
        agent_tools.pick_plot_sample = original_picker
        bm.delete(bid)


def main():
    test_unrelated_sample_does_not_stale()
    test_prepare_save_style_stale()
    print("style snapshot: OK")


if __name__ == "__main__":
    main()
