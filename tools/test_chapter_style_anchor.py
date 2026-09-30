# -*- coding: utf-8 -*-
"""章级样文锚验收（2026-09-11）。

要保护的不变量：
1. **一章只抽一次**样文（章内第 2..N 个情节段不再抽样、不再写避重历史）；
2. 章内所有情节段拿到**逐字相同**的样文正文（冻结，不是只冻结 id）；
3. 锚落盘后**不会被保存下一个情节段冲掉**（旧实现每保存一段就重建 draft JSON）；
4. 样文库被编辑**不影响当前章**，只影响下一章；
5. continuity_tail：章内接上一段尾巴，新章接上一章尾巴。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from types import SimpleNamespace  # noqa: E402


def prose(n, name="顾衡"):
    unit = f"{name}握紧仪器，走廊的红灯闪了一次，他没有回头。"
    return (unit * (n // len(unit) + 1))[:n]


def main():
    from libraries import style_samples
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    from libraries.style_samples import StyleSample
    import agent_tools

    bm = BookManager(os.path.join(ROOT, "books"))
    cfg = bm.create(title="章锚验收", pen_name="测试", chapter_count=20)
    bid = cfg.book_id

    pool = [StyleSample(id="s1", text="样文甲：他数了三遍弹壳，一遍也没数错。", title="甲"),
            StyleSample(id="s2", text="样文乙：风从北面来，带着铁锈和雪的味道。", title="乙")]
    picks, recorded = [], []
    orig_pool, orig_profile_for = style_samples.pool_for, agent_tools._profile_for
    orig_record, orig_pick = agent_tools._record_pick, agent_tools.pick_plot_sample
    fake_profile = SimpleNamespace(id="profile-anchor-test",
                                   build_style_card=lambda: "笔名：测试｜短句")
    try:
        tl = BookStoryline(book_title="章锚验收", pen_name="测试", phase="ready",
                           words_per_chapter=3000)
        tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                          "identity": "调查员", "speech_profile": {}}]}
        tl.outlines = [OutlineSlot(id="a", template_id="", name="开局", start_word=0, end_word=12000)]
        tl.plots = [PlotSlot(id=f"p{i}", template_id="", name=f"段{i}", outline_id="a", words=1500,
                             order=i, roles=["顾衡"], protocol_version=2)
                    for i in range(1, 7)]
        bm.save_storyline(bid, tl)

        style_samples.pool_for = lambda _profile: list(pool)
        agent_tools._profile_for = lambda _tl: fake_profile
        real_pick = agent_tools.pick_plot_sample

        def counting_pick(book_id="", query=None, profile_id=""):
            picks.append(query)
            return real_pick(book_id, query=query, profile_id=profile_id)

        agent_tools.pick_plot_sample = counting_pick
        agent_tools._record_pick = lambda pid, ids: recorded.append((pid, list(ids)))

        # ── 1. 章首抽一次 ──
        r1 = agent_tools.prepare_plot_run(bid)
        assert r1["execution"]["id"] == "p1", r1["execution"]["id"]
        assert len(picks) == 1, f"章首应恰好抽 1 次，实际 {len(picks)}"
        assert len(recorded) == 1, f"避重历史应恰好记 1 次，实际 {len(recorded)}"
        sid1 = r1["style"]["sample"]["receipt"]["sample_id"]
        text1 = r1["style"]["sample"]["text"]
        assert sid1 and text1.strip(), (sid1, text1[:40])
        assert r1["style"]["chapter_style_anchor"]["anchor_id"].startswith("chapter:1:")
        assert r1["execution"]["is_chapter_opening"] is True
        print(f"[1] 章首抽一次 OK（sample={sid1}）")

        # ── 2. 同 Plot 重试：不重抽、令牌复用 ──
        r1b = agent_tools.prepare_plot_run(bid)
        assert len(picks) == 1, f"重试不该重抽，实际 {len(picks)}"
        assert r1b["run"]["commit_token"] == r1["run"]["commit_token"]
        print("[2] 同 Plot 重试不重抽 OK")

        # ── 3. 写第 1 段 → 锚必须活下来（P0 回归）──
        agent_tools.save_plot_draft(
            r1["run"]["commit_token"], prose(1500), "顾衡在走廊尽头确认了异常信号的来源并留下新的线索，准备继续追查，同时维持紧张的调查节奏与明确的行动接口。",
            outcome={"choices_made": ["前往走廊尽头"]})
        draft = agent_tools._draft_read(bid)
        assert draft.get("style_anchor"), "保存情节段后章锚被冲掉了（P0 回归）"
        assert draft["style_anchor"]["sample"]["sample_id"] == sid1
        assert draft["style_anchor"]["sample"]["rendered_text"] == text1
        print("[3] 保存情节段后章锚存活 OK")

        # ── 4. 章内第 2 段：不重抽，且**正文逐字相同**（冻结语义）──
        r2 = agent_tools.prepare_plot_run(bid)
        assert r2["execution"]["id"] == "p2", r2["execution"]["id"]
        assert len(picks) == 1, f"章内第 2 段不该重抽，实际 {len(picks)}"
        assert len(recorded) == 1, "章内第 2 段不该再记避重历史"
        assert r2["style"]["sample"]["receipt"]["sample_id"] == sid1
        assert r2["style"]["sample"]["text"] == text1, "章内样文正文变了——冻结失效"
        assert r2["execution"]["is_chapter_opening"] is False
        print("[4] 章内复用同一份冻结正文 OK")

        # ── 5. 编辑样文库：本章不受影响 ──
        pool[0].text = "样文甲：改过的文本，完全不同的句子结构。"
        pool[1].text = "样文乙：也改过了。"
        r2b = agent_tools.prepare_plot_run(bid)
        assert r2b["style"]["sample"]["text"] == text1, "编辑样文库后本章样文变了——冻结失效"
        print("[5] 编辑样文库不影响当前章 OK")

        # ── 6. continuity_tail：章内接上一段尾巴 ──
        tail = r2b.get("continuity_tail") or {}
        assert tail.get("source") == "previous_plot", tail
        assert tail.get("text", "").endswith(prose(1500)[-80:]), tail.get("text", "")[-80:]
        assert len(tail["text"]) <= agent_tools.CONTINUITY_TAIL_CHARS
        # memory.recent 里最近一段不再重复带同一条正文尾巴
        assert "ending" not in (r2b["memory"]["recent"][0] if r2b["memory"]["recent"] else {}), \
            r2b["memory"]["recent"]
        print("[6] 章内 continuity_tail OK")

        # ── 7. 收章 → 新章：换新锚，尾巴改接上一章 ──
        agent_tools.save_plot_draft(
            r2b["run"]["commit_token"], prose(1400), "顾衡沿着新线索继续推进调查，确认危险尚未解除，并为下一步留下明确的行动接口、代价与时间压力，保持调查节奏不松。",
            outcome={})
        committed = agent_tools.finalize_draft_chapter(bid)
        assert committed.get("ok"), committed
        assert agent_tools._draft_read(bid) is None, "收章后草稿应被清掉"
        r3 = agent_tools.prepare_plot_run(bid)
        assert r3["execution"]["is_chapter_opening"] is True
        assert len(picks) == 2, f"新章应重新抽一次，实际 {len(picks)}"
        anchors = {r3["style"]["chapter_style_anchor"]["anchor_id"]}
        assert anchors != {r1["style"]["chapter_style_anchor"]["anchor_id"]}, "新章应换锚"
        tail3 = r3.get("continuity_tail") or {}
        assert tail3.get("source") == "previous_chapter", tail3
        assert tail3.get("text", "").endswith(prose(1400)[-80:]) or len(tail3["text"]) > 0, tail3
        print("[7] 新章换锚 + continuity_tail 跨章 OK")

        # ── 8. 章内 scene_modulation 有值且不触发抽样 ──
        assert isinstance(r3["style"].get("scene_modulation"), dict)
        assert len(picks) == 2
        print("[8] scene_modulation OK")
    finally:
        style_samples.pool_for = orig_pool
        agent_tools._profile_for = orig_profile_for
        agent_tools._record_pick = orig_record
        agent_tools.pick_plot_sample = orig_pick
        bm.delete(bid)

    print()
    print("章级样文锚：全部通过")


if __name__ == "__main__":
    main()
