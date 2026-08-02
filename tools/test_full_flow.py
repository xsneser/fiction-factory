# -*- coding: utf-8 -*-
"""
完整流程端到端测试：新书生成大纲 → 撰写 → 文本填充 → 续写

真实 LLM 调用。为控制成本，生成的故事线会被裁剪成 3 章小书再走完整写作引擎。
用法: python tools/test_full_flow.py [--genre 都市] [--sub 系统流]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NOVEL_ENGINE_DIR"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.llm_client import LLMClient
from core.models import APIConfig
from libraries.outline_generator import OutlineGenerator
from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.engine import NovelEngine, Instruction, Op


def make_llm():
    cfg = json.load(open("api.json", encoding="utf-8"))
    return LLMClient(APIConfig(
        api_key=cfg.get("api_key", ""), base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
        http_timeout_seconds=cfg.get("http_timeout_seconds", 300),
        verify_ssl=cfg.get("verify_ssl", True),
    ))


def stage(tag, msg):
    print(f"\n{'='*60}\n▶ [{tag}] {msg}\n{'='*60}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--genre", default="都市")
    ap.add_argument("--sub", default="系统流")
    ap.add_argument("--context", default="落魄程序员绑定『打工人系统』，靠加班获得超能力逆袭。开局一条街的爽感，后段转向灵气复苏阴谋。")
    args = ap.parse_args()

    llm = make_llm()
    t0 = time.time()

    # ═══ ① 新书生成大纲（真实 5 阶段）═══
    stage("新书生成大纲", "OutlineGenerator 5 阶段管线")
    gen = OutlineGenerator(llm_client=llm, structure_lib=StructureLibrary(),
                           plot_lib=PlotLibrary(), gag_lib=GagLibrary(),
                           theme_lib=ThemeLibrary(), profile=None)
    result = None
    for evt, msg, data in gen.generate(genre=args.genre, sub_genre=args.sub,
                                       custom_context=args.context, pen_name="测试笔名",
                                       words_per_chapter=2000, max_outlines=2):
        if evt == "done":
            result = data.get("timeline")
        elif evt == "error":
            print("❌ 大纲生成失败:", msg); return 1
    if not result:
        print("❌ 未得到大纲"); return 1
    n_outlines = len(result.get("outlines", []))
    n_plots = len(result.get("plots", []))
    print(f"✅ 大纲生成完成：{n_outlines} 条大纲 / {n_plots} 个桥段")

    # 裁剪成 5 章小书（控制写作成本），保留大纲的阶段与桥段；
    # 续写第 4 章仍在大纲范围内，可验证"故事线上下文"分支
    for o in result["outlines"]:
        o["end_chapter"] = min(o["end_chapter"], o["start_chapter"] + 4)
    from libraries.timeline import BookTimeline
    tl = BookTimeline.from_dict(result)
    tl.words_per_chapter = 2000
    print(f"→ 裁剪为 {len(tl.outlines)} 条大纲，总章数 "
          f"{max(o.end_chapter for o in tl.outlines)}（供写作测试）")

    # ═══ ② 新书写作：撰写 + 文本填充（TimelineChapterWriter：按桥段生成→满章切分→落盘）═══
    stage("撰写 + 文本填充", "engine.start_new_book_timeline → TimelineChapterWriter")
    engine = NovelEngine(llm_client=llm)
    state = engine.start_new_book_timeline(tl)
    book_id = state.book_id
    print(f"创建图书: {book_id}")

    for ch in range(1, 4):
        inst = Instruction(Op.WRITE_TIMELINE_CHAPTER, chapter_num=ch)
        r = engine.execute(inst)
        wc = r.get("word_count", 0)
        bp = r.get("blueprint", {})
        print(f"  第{ch}章: {wc}字 | 蓝图: 扩写{bp.get('outlines_expanded')} 填充{bp.get('plots_filled')} 跳过{bp.get('plots_skipped')}")
        if r.get("status") != "chapter_written":
            print("  ⚠️", r); return 1

    # 更新进度供续写定位（timeline 流程本身不 bump current_chapter）
    engine.book.current_chapter = 3
    engine.book_mgr.update(engine.book)

    # ═══ ③ 续写：continue_book → beat_writer 写第 4 章 ═══
    stage("续写", "continue_book → _exec_write_chapter（节拍级）")
    engine2 = NovelEngine(llm_client=llm)
    engine2.continue_book(book_id)
    r = engine2._exec_write_chapter(Instruction(Op.WRITE_CHAPTER, 4))
    wc = r.get("word_count", 0)
    print(f"  第4章: {wc}字 | status={r.get('status')} | 用了故事线上下文: "
          f"{'是' if engine2.state.current_content else '否'}")
    if r.get("status") != "chapter_written":
        print("❌ 续写失败:", r); return 1

    # ═══ 校验落盘 ═══
    stage("落盘校验", f"books/{book_id}/")
    from libraries.book_manager import BookManager
    bm = BookManager("books")
    chs = [bm.load_chapter(book_id, n) for n in (1, 2, 3, 4)]
    for i, c in enumerate(chs, 1):
        content = (c or {}).get("content", "")
        print(f"  第{i}章 {'存在' if content else '缺失'}, {len(content)} 字符, 前30字: {content[:30]}")
    if not all(chs):
        print("❌ 章节未全部落盘"); return 1

    # 清理生成的测试书
    bm.delete(book_id)
    print(f"✅ 测试书 {book_id} 已清理")

    print(f"\n{'='*60}\n✅ 完整流程通过：生成大纲 → 撰写 → 文本填充 → 续写（总耗时 {time.time()-t0:.1f}s）\n{'='*60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
