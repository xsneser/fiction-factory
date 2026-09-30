#!/usr/bin/env python3
"""R3/R4/R5 文件级验收 —— 纯 Python（不需 mcp/LLM），走真实薄工具落盘。

覆盖（修订验收）：
  1. save_chapter_text 每次成功落盘 → storyline_revision +1
  2. replan preview expected_revision=旧值（N）→ 章节 bump 到 N+1 后 commit 必须 stale_storyline
  3. save_chapter_text(planning_patch) 把 story_questions/character_intents 语义合并进
     planning_state（重复上报去重、不无限 append）

用法：python tools/test_replan_revisions.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _filler(n: int) -> str:
    """造 n 个中文字符的可发布文本（重复模板+序号，够字数门禁即可，非文学）。"""
    base = ("夜色落进舱室，顾衡把检测仪抵在金属壁上，读数跳动，他屏住呼吸。"
            "门轴摩擦的声响从走廊那头传来，一下，又一下。他没回头，指节收拢，"
            "知道灯亮起之前自己只有几秒。")
    out = []
    while sum(len(x) for x in out) < n:
        out.append(base)
    s = "".join(out)
    return s[:n]


def _make_book(bm, title="Replan验收"):
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline
    cfg = bm.create(title=title, pen_name="测试", chapter_count=500)
    bid = cfg.book_id
    bm.save_storyline(bid, BookStoryline(
        book_title=title, pen_name="测试", platform=cfg.platform,
        words_per_chapter=3000, basic_info={}, phase="ready", outlines=[], plots=[],
        storyline_revision=5))
    return bm, bid


def main():
    from libraries.book_manager import BookManager
    from libraries.planning_state import (planning_path, read_json,
                                          save_replan_preview, load_replan_preview)
    from libraries.replan_service import commit_replan_preview
    from agent_tools import save_chapter_text

    bm = BookManager(os.path.join(_ROOT, "books"))
    bm, bid = _make_book(bm)
    try:
        text = _filler(2200)

        # 1) 章节落盘 → revision +1（5→6）
        r1 = save_chapter_text(bid, 1, text, title="第1章", summary="测试摘要",
                               planning_patch={
                                   "story_questions": [{"question": "谁是内鬼？", "priority": 0.9,
                                                        "source_plot_id": "p1"}],
                                   "character_intents": [{"name": "顾衡", "intent": "查清真相"}],
                               })
        check("save_chapter_text 成功", bool(r1.get("ok")), f"{r1}")
        from libraries.storyline import load_storyline
        tl1 = load_storyline(os.path.join(_ROOT, "books", bid, "storyline.json"))
        check("章节落盘后 storyline_revision=6", int(getattr(tl1, "storyline_revision", 0)) == 6,
              f"(rev={getattr(tl1, 'storyline_revision', None)})")

        # 2) planning_patch 语义合并：story_questions 入库且重复上报不翻倍
        plan1 = read_json(planning_path(bid)) or {}
        check("planning_patch story_questions 入库", len(plan1.get("story_questions") or []) == 1,
              f"{plan1.get('story_questions')}")
        check("planning_patch character_intents 入库(顾衡)",
              any(x.get("name") == "顾衡" for x in (plan1.get("character_intents") or [])),
              f"{plan1.get('character_intents')}")

        # 3) 重复上报同文问题 → 仍是 1 条（去重）
        r2 = save_chapter_text(bid, 2, text, title="第2章", summary="测试摘要",
                               planning_patch={
                                   "story_questions": [{"question": "谁是内鬼？", "status": "progressed",
                                                        "last_touched_plot_id": "p2"}],
                                   "character_intents": [{"name": "顾衡", "intent": "承担系统责任"}],
                               })
        check("save_chapter_text(2) 成功", bool(r2.get("ok")), f"{r2}")
        plan2 = read_json(planning_path(bid)) or {}
        check("同题重复上报去重为 1 条且推进",
              len(plan2.get("story_questions") or []) == 1
              and (plan2["story_questions"][0].get("status") == "progressed"),
              f"{plan2.get('story_questions')}")
        cq = [x for x in (plan2.get("character_intents") or []) if x.get("name") == "顾衡"]
        check("character_intents 同人 upsert 不翻倍",
              len(cq) == 1 and cq[0].get("intent") == "承担系统责任", f"{cq}")

        # 4) 章节 bump 到 7 → 旧 preview(expected=6) commit 必须 stale
        tl_now = load_storyline(os.path.join(_ROOT, "books", bid, "storyline.json"))
        check("二次落盘后 revision=7", int(getattr(tl_now, "storyline_revision", 0)) == 7,
              f"(rev={getattr(tl_now, 'storyline_revision', None)})")
        save_replan_preview(bid, {"preview_id": "pv_old", "expected_revision": 6,
                                  "outlines": [], "plots": [], "planning_patch": {},
                                  "validation": {"passed": True}})
        stale = commit_replan_preview(bid, "pv_old", 6)
        check("旧 preview commit → stale_storyline(actual=7)",
              stale.get("ok") is False and stale.get("error") == "stale_storyline"
              and stale.get("actual") == 7, f"{stale}")
    finally:
        try:
            bm.delete(bid)
        except Exception:
            pass

    print("\n" + "=" * 50)
    print(f"  Replan Revision 验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 50)


if __name__ == "__main__":
    main()
