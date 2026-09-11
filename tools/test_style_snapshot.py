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


def test_anchor_selector_version_isolated():
    """章锚走独立版本号：不动全局 SELECTOR_VERSION，两条链互不干扰。"""
    from libraries.style_snapshot import (CHAPTER_ANCHOR_SELECTOR_VERSION, SELECTOR_VERSION,
                                           build_snapshot, rendered_sample_digest,
                                           snapshot_matches)

    profile = SimpleNamespace(id="profile-test")
    digest = rendered_sample_digest("# STYLE REFERENCE\n样文甲")
    receipt = {"sample_id": "s1", "content_digest": digest}

    # 1) 全局版本号没被改名——legacy pick_plot_sample 的账仍按 plot-sample-v1 记
    assert SELECTOR_VERSION == "plot-sample-v1", SELECTOR_VERSION
    assert CHAPTER_ANCHOR_SELECTOR_VERSION != SELECTOR_VERSION

    legacy = build_snapshot(profile, "卡", receipt)
    assert legacy["selector_version"] == SELECTOR_VERSION
    assert snapshot_matches(profile, "卡", legacy, digest), "legacy 路径被破坏"

    # 2) 章锚快照带 anchor 身份；同一样文属于不同章锚时不可互相匹配
    a1 = build_snapshot(profile, "卡", receipt,
                        selector_version=CHAPTER_ANCHOR_SELECTOR_VERSION,
                        anchor_extra={"scope": "chapter", "chapter_num": 1, "anchor_id": "chapter:1:x"})
    a2 = build_snapshot(profile, "卡", receipt,
                        selector_version=CHAPTER_ANCHOR_SELECTOR_VERSION,
                        anchor_extra={"scope": "chapter", "chapter_num": 2, "anchor_id": "chapter:2:x"})
    assert a1["selector_version"] == CHAPTER_ANCHOR_SELECTOR_VERSION
    assert a1["digest"] != a2["digest"], "不同章锚用了同一 digest"
    extra1 = {"scope": "chapter", "chapter_num": 1, "anchor_id": "chapter:1:x"}
    assert snapshot_matches(profile, "卡", a1, digest, anchor_extra=extra1)
    # 传错 anchor_extra（模拟按另一章复现）→ 必须不匹配，而不是静默通过
    assert not snapshot_matches(profile, "卡", a1, digest,
                                anchor_extra={"scope": "chapter", "chapter_num": 2,
                                              "anchor_id": "chapter:2:x"})
    # 3) 空值的 anchor_extra 不污染 digest（首章无锚时不带这些键）
    plain = build_snapshot(profile, "卡", receipt,
                           selector_version=CHAPTER_ANCHOR_SELECTOR_VERSION)
    blank = build_snapshot(profile, "卡", receipt,
                           selector_version=CHAPTER_ANCHOR_SELECTOR_VERSION,
                           anchor_extra={"anchor_id": "", "chapter_num": 0})
    assert plain["digest"] == blank["digest"], (plain, blank)


def main():
    test_unrelated_sample_does_not_stale()
    test_prepare_save_style_stale()
    test_anchor_selector_version_isolated()
    print("style snapshot: OK")


if __name__ == "__main__":
    main()
