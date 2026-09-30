# -*- coding: utf-8 -*-
"""章节标题与摘要验收（2026-09-11）。纯规则 + 一次真实收章（临时书，用完即删）。

要保护的不变量：
1. 章标题由**实际开章的第一个情节段**给出，服务端一章只接受一次（章界是运行时按字数
   动态决定的，规划期无法预知谁开章）；
2. 库里存**裸标题**，展示端加前缀——存量「第1章 天闪裂空」在读取处归一，不靠迁移；
3. 章节摘要**全部情节段参与**且总长受预算约束（旧实现只取前 3 段，细粒度后会丢掉后半章）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def prose(n, name="顾衡"):
    unit = f"{name}握紧仪器，走廊的红灯闪了一次，他没有回头。"
    return (unit * (n // len(unit) + 1))[:n]


def main():
    from libraries.book_manager import chapter_display_title, normalize_chapter_title
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    import agent_tools

    # ── 1. 标题归一（展示层规范，覆盖存量带前缀数据）──
    for raw, want in (("第1章 天闪裂空", "天闪裂空"), ("天闪裂空", "天闪裂空"),
                      ("第5章", ""), ("第12章：墙起炉鸣", "墙起炉鸣"),
                      ("第 3 章 · 铁城之夜", "铁城之夜"), ("第十章 归途", "归途"),
                      ("", ""), (None, "")):
        got = normalize_chapter_title(raw)
        assert got == want, (raw, got, want)
    assert chapter_display_title({"title": "第1章 天闪裂空"}) == "天闪裂空"
    assert chapter_display_title({}) == ""
    print("[1] 标题归一（含存量前缀）OK")

    # ── 2. 摘要：全部情节段参与 + 预算约束 ──
    bridges = [{"plot_summary": f"这是第{i}段的情节摘要，描述该段发生的主要变化与人物选择。" + "补充细节。" * 20}
               for i in range(1, 7)]
    s6 = agent_tools._chapter_summary(bridges)
    assert len(s6) <= agent_tools.CHAPTER_SUMMARY_MAX_CHARS, len(s6)
    assert all(f"第{i}段" in s6 for i in range(1, 7)), s6      # 六段全在（旧实现只留前三段）
    assert "第4段" in s6 and "第6段" in s6, s6
    # 短摘要不截断
    short = [{"plot_summary": "短摘要"} for _ in range(3)]
    assert agent_tools._chapter_summary(short) == "短摘要；短摘要；短摘要"
    assert agent_tools._chapter_summary([]) == ""
    assert agent_tools._chapter_summary([{"plot_summary": ""}]) == ""
    print("[2] 章节摘要（全段参与 + 预算）OK")

    # ── 3. 真实收章：标题由开章段给出，且一章只收一次 ──
    bm = BookManager(os.path.join(ROOT, "books"))
    cfg = bm.create(title="章节元数据验收", pen_name="测试", chapter_count=20)
    bid = cfg.book_id
    saved = {"pick": agent_tools.pick_plot_sample, "profile": agent_tools._profile_for}
    try:
        tl = BookStoryline(book_title="章节元数据验收", pen_name="测试", phase="ready",
                           words_per_chapter=3000)
        tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                          "identity": "调查员", "speech_profile": {}}]}
        tl.outlines = [OutlineSlot(id="a", template_id="", name="开局", start_word=0, end_word=9000)]
        tl.plots = [PlotSlot(id=f"p{i}", template_id="", name=f"段{i}", outline_id="a", words=1000,
                             order=i, roles=["顾衡"], protocol_version=2)
                    for i in range(1, 5)]
        bm.save_storyline(bid, tl)
        # 无样文也能写（style card 保底），本测试不关心风格
        agent_tools.pick_plot_sample = lambda *_a, **_k: {"ok": False, "error": "test: no sample"}

        r1 = agent_tools.prepare_plot_run(bid)
        assert r1["execution"]["is_chapter_opening"] is True
        agent_tools.save_plot_draft(
            r1["run"]["commit_token"], prose(1500),
            "顾衡在旧工区确认了异常信号的来源并把线索固定下来，为下一步行动留下明确的接口与代价，节奏不松。",
            outcome={}, chapter_title="第1章 天闪裂空")     # 故意带前缀，服务端须归一
        draft = agent_tools._draft_read(bid)
        assert draft["chapter_title"] == "天闪裂空", draft.get("chapter_title")

        r2 = agent_tools.prepare_plot_run(bid)
        assert r2["execution"]["is_chapter_opening"] is False
        agent_tools.save_plot_draft(
            r2["run"]["commit_token"], prose(1400),
            "顾衡沿着新线索推进调查并确认危险尚未解除，为下一步留下明确的行动接口、代价与时间压力。",
            outcome={}, chapter_title="后面来的候选标题")     # 非开章段 → 必须被忽略
        assert agent_tools._draft_read(bid)["chapter_title"] == "天闪裂空"
        print("[3] 开章段定标题 / 后续候选被忽略 OK")

        result = agent_tools.finalize_draft_chapter(bid)
        assert result.get("ok"), result
        ch = bm.load_chapter(bid, 1)
        assert ch, "章必须落盘"
        assert ch["title"] == "天闪裂空", ch["title"]      # 库里是裸标题
        assert ch["summary"] and "顾衡" in ch["summary"], ch["summary"]
        print(f"[4] 收章标题（裸标题 {ch['title']!r}）与摘要 OK")
    finally:
        agent_tools.pick_plot_sample = saved["pick"]
        agent_tools._profile_for = saved["profile"]
        bm.delete(bid)

    print()
    print("章节标题与摘要：全部通过")


if __name__ == "__main__":
    main()
