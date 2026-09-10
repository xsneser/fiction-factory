#!/usr/bin/env python3
"""批次1 回归：写作流程的两处 P0/P1 口径与并发。

锁定两处回归：
1. 「已写字数」只有一个口径（绝对轴）：已落盘章节正文计字 + 当前草稿计字
   ——`agent_tools._runtime_written_words`。FSM 曾用 `draft["words"]`（章内量纲）判边界，
   导致 FSM 侧 WORDS_LOW 永不触发，并与 `prepare_plot_run` / 规划 UI 的判定互相矛盾。
   本测试同时断言「章内口径会得出相反结论」，把差异钉在测试里。
2. `save_plot_draft` 在解析出归属书后**自行** BookLock（它签名里没有 book_id，
   `_wrap_book_lock` 取不到锁目标 → 此前提交路径完全无锁）。

用法：python tools/test_writer_fsm_boundary.py
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main():
    from core.text_utils import count_prose_units
    from libraries.book_manager import BookManager
    from libraries.planning_state import detect_story_boundary
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    import agent_tools
    import libraries.book_lock as book_lock

    bm = BookManager(os.path.join(_ROOT, "books"))
    cfg = bm.create(title="Writer FSM 边界口径验收", pen_name="测试", chapter_count=8)
    bid = cfg.book_id
    old_flow = os.environ.get("NOVEL_WRITE_FLOW_ID")
    old_child = os.environ.get("NOVEL_WRITE_CHILD_RUN_ID")
    original_lock_cls = book_lock.BookLock
    trace = []

    class SpyLock(original_lock_cls):
        def acquire(self, timeout=30.0, purpose="write"):
            ok = super().acquire(timeout=timeout, purpose=purpose)
            trace.append(("acquire", self.book_id, purpose, ok))
            return ok

        def release(self):
            trace.append(("release", self.book_id))
            return super().release()

    try:
        # ── 建书：1 个弧（承诺到 6000 字）+ 1 段已写 + 4 段未写 ──
        tl = BookStoryline(book_title="Writer FSM 边界口径验收", pen_name="测试",
                          phase="ready", words_per_chapter=3000)
        tl.basic_info = {"characters": [{"name": "顾衡", "role": "主角", "importance": 1,
                                          "identity": "调查员", "speech_profile": {}}]}
        tl.outlines = [OutlineSlot(id="a", template_id="", name="开局", start_word=0, end_word=6000)]
        tl.plots = [PlotSlot(id="p0", template_id="", name="入局", outline_id="a", words=1500,
                             order=1, roles=["顾衡"], protocol_version=2, written_chapter=1)]
        tl.plots += [PlotSlot(id=f"p{i}", template_id="", name=f"段{i}", outline_id="a", words=1500,
                              order=i + 1, roles=["顾衡"], protocol_version=2) for i in range(1, 5)]
        bm.save_storyline(bid, tl)

        # ── 第 1 章已落盘（v2 计量字段随章节落盘）──
        chapter_text = "顾衡继续向前走。" * 400          # 7 CJK/句 → 2800 字
        bm.save_chapter(bid, 1, "第1章", chapter_text, "")
        book = bm.get(bid)
        book.current_chapter = 1
        book.total_words = count_prose_units(chapter_text)
        bm.update(book)

        # ── 第 2 章草稿：1 个情节段的正文（章内口径只有它）──
        draft_text = "顾衡压低声音。" * 100              # 7 CJK/句 → 700 字
        draft_words = count_prose_units(draft_text)
        draft_path = os.path.join(_ROOT, "books", bid, "draft_chapter.json")
        with open(draft_path, "w", encoding="utf-8") as f:
            json.dump({"chapter_num": 2, "buffer": [draft_text], "words": draft_words,
                       "bridges": [{"plot_id": "p1", "plot_name": "段1", "text": draft_text}]},
                      f, ensure_ascii=False, indent=2)

        # ── 1) 口径：累计 = 已落盘章 + 草稿，且必须显著大于章内草稿字数 ──
        written = agent_tools._runtime_written_words(bid)
        assert written == count_prose_units(chapter_text) + draft_words, written
        assert written > draft_words, (written, draft_words)

        # 章节缺失 v2 计量字段时按正文补算（v2 迁移前写入的旧章）
        ch_path = os.path.join(_ROOT, "books", bid, "chapters", "0001.json")
        raw_chapter = json.load(open(ch_path, encoding="utf-8"))
        raw_chapter.pop("actual_prose_units")
        raw_chapter.pop("raw_codepoints")
        json.dump(raw_chapter, open(ch_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        assert agent_tools._runtime_written_words(bid) == written, "旧章回退计字应得到同一结果"

        # ── 2) 边界：绝对轴口径触发 WORDS_LOW，章内口径不触发 ──
        common = dict(committed_until_word=6000, remaining_plots=4, words_per_batch=3000,
                      storyline_revision=int(getattr(bm.load_storyline(bid), "storyline_revision", 0) or 0),
                      last_replan={})
        fixed = detect_story_boundary(written_until_word=written, **common)
        chapter_local = detect_story_boundary(written_until_word=draft_words, **common)
        assert "PLOTS_LOW" not in fixed["reason_codes"], fixed          # 还有 4 段可写
        assert "WORDS_LOW" in fixed["reason_codes"], fixed              # 承诺余量 ≤ 一批
        assert fixed["needs_replan"] is True, fixed
        assert chapter_local["needs_replan"] is False, chapter_local    # 章内量纲会漏判
        assert fixed["remaining_words"] < chapter_local["remaining_words"], (fixed, chapter_local)

        # ── 3) 单一口径：Writer 包与 FSM 用同一判定 ──
        # FSM 的 _evaluate 是闭包、只在 spawn 子进程时可见，这里以源码守卫钉住
        # 「边界字数只走 _runtime_written_words」这一不变量（曾用 draft["words"]）。
        bridge_src = open(os.path.join(_ROOT, "libraries", "dsh_bridge.py"), encoding="utf-8").read()
        assert "written_until_word=_runtime_written_words(" in bridge_src, \
            "FSM 边界必须用绝对轴口径 _runtime_written_words，不得回退 draft[\"words\"]"
        assert "written_until_word=int(draft.get(\"words\") or 0)" not in bridge_src, \
            "FSM 不得再用章内草稿字数为边界传参"
        flow = agent_tools.prepare_plot_run(bid)
        os.environ["NOVEL_WRITE_FLOW_ID"] = flow["run"].get("flow_id") or ""
        assert flow["horizon"]["boundary"]["needs_replan"] is True, flow["horizon"]["boundary"]
        assert flow["horizon"]["boundary"]["remaining_words"] == fixed["remaining_words"], \
            (flow["horizon"]["boundary"], fixed)

        # ── 4) 提交路径自持锁（wrapper 取不到 book_id，必须在函数体内加锁）──
        assert "save_plot_draft" not in agent_tools._LOCKED_TOOLS, "自持锁工具不能进 wrapper 锁集合（不可重入）"
        assert "save_plot_draft" in agent_tools._SELF_LOCKED_TOOLS, "元数据仍应标注 locked"
        book_lock.BookLock = SpyLock
        result = agent_tools.save_plot_draft(
            flow["run"]["commit_token"], "顾衡推开门。", plot_summary="",
            outcome={"choices_made": ["推门"]})
        assert result.get("ok"), result
        assert ("acquire", bid, "save_plot_draft", True) in trace, trace
        assert ("release", bid) in trace, trace
        print("writer fsm boundary + self-lock: OK")
    finally:
        book_lock.BookLock = original_lock_cls
        for key, value in (("NOVEL_WRITE_FLOW_ID", old_flow), ("NOVEL_WRITE_CHILD_RUN_ID", old_child)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        bm.delete(bid)


if __name__ == "__main__":
    main()
