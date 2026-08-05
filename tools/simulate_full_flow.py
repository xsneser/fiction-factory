# -*- coding: utf-8 -*-
"""
全流程模拟脚本：创建小说 → 大纲/故事线 → 章节写作 → 续写 → 上架 → 导出 → 清理

真实 LLM 调用。模拟"用户从创建到续写上架"的完整闭环，逐段断言，失败即退出非零。
成本控制：max_outlines=2、短章（--words-per-chapter 默认 800）、detector_frequency=999 跳过
灵机一动探测器；上架检查用 --min-total-words 覆盖真实阈值（真实番茄/起点上架线为 2w/3w 字）。

用法:
    python tools/simulate_full_flow.py                      # 默认跑通并清理测试书
    python tools/simulate_full_flow.py --keep-book          # 保留测试书便于人工检查
    python tools/simulate_full_flow.py --chapters 2         # 新书写 2 章再续写
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NOVEL_ENGINE_DIR"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认 GBK，直接 print 中文/✓/→ 会崩 → 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.llm_client import LLMClient
from core.models import APIConfig
from libraries.outline_generator import OutlineGenerator
from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.timeline import BookTimeline
from libraries.engine import NovelEngine
from libraries.book_manager import BookManager
from libraries.publisher import Publisher


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
    ap.add_argument("--words-per-chapter", type=int, default=800)
    ap.add_argument("--max-outlines", type=int, default=2)
    ap.add_argument("--chapters", type=int, default=1, help="新书阶段写满几章再续写")
    ap.add_argument("--continue-bridges", type=int, default=1, help="续写阶段写几个桥段")
    ap.add_argument("--min-total-words", type=int, default=800, help="上架检查的字数线（覆盖真实阈值）")
    ap.add_argument("--min-chapters", type=int, default=1, help="上架检查的章节线")
    ap.add_argument("--keep-book", action="store_true", help="保留测试书不清理")
    args = ap.parse_args()

    llm = make_llm()
    t0 = time.time()
    book_id = None
    bm = BookManager("books")

    try:
        # ═══ ① 创建小说 + 大纲/故事线（真实 6 阶段）═══
        stage("① 创建 + 大纲", f"OutlineGenerator 生成故事线（max_outlines={args.max_outlines}）")
        gen = OutlineGenerator(llm_client=llm, structure_lib=StructureLibrary(),
                               plot_lib=PlotLibrary(), gag_lib=GagLibrary(),
                               theme_lib=ThemeLibrary(), profile=None)
        result = None
        for evt, msg, data in gen.generate(
                genre=args.genre, sub_genre=args.sub, custom_context=args.context,
                pen_name="模拟笔名", words_per_chapter=args.words_per_chapter,
                max_outlines=args.max_outlines):
            if evt == "done":
                result = data.get("timeline")
            elif evt == "error":
                print("❌ 大纲生成失败:", msg)
                return 1
        if not result:
            print("❌ 未得到大纲结果")
            return 1
        tl = BookTimeline.from_dict(result)
        n_o, n_p = len(tl.outlines), len(tl.plots)
        total_ch = max((o.end_chapter for o in tl.outlines), default=0)
        print(f"✅ 大纲生成：{n_o} 条大纲 / {n_p} 个桥段 / 共 {total_ch} 章")
        if n_o < 1 or n_p < 1:
            print("❌ 大纲或桥段为空")
            return 1

        # ═══ ② 开始写作 → 建正式书（模拟用户点「开始写作」）═══
        stage("② 开始写作", "start_new_book_timeline 建正式书")
        engine = NovelEngine(llm_client=llm)
        # detector_frequency 调大 → 跳过探测器 LLM 调用（省钱，只验主链路）
        state = engine.start_new_book_timeline(tl, config={"detector_frequency": 999})
        book_id = state.book_id
        print(f"✅ 建书完成: {book_id} | total_chapters={state.total_chapters}")
        if not book_id:
            print("❌ 建书失败")
            return 1
        if not bm.get(book_id) or not bm.load_timeline(book_id):
            print("❌ book.json / timeline.json 未落盘")
            return 1

        # ═══ ③ 章节写作（逐桥段，写满 args.chapters 章）═══
        stage("③ 章节写作", f"逐桥段写入（目标 {args.chapters} 章）")
        chapter_done = 0
        error_evt = None
        for i in range(15):
            if chapter_done >= args.chapters:
                break
            for evt in engine._write_next_bridge_stream():
                t = evt.get("type")
                if t == "bridge_start":
                    print(f"   [写] {evt.get('plot_name')} (预计 {evt.get('planned_words')}字)")
                elif t == "chapter_done":
                    chapter_done += 1
                    print(f"   [第{evt.get('chapter')}章完成] {evt.get('word_count')}字 · 审查 {evt.get('review', {}).get('score', '?')}分")
                elif t == "complete":
                    print(f"   [全书完成] {evt.get('message')}")
                elif t in ("error",):
                    error_evt = evt
                    break
        if error_evt:
            print("❌ 写作出错:", error_evt.get("message"))
            return 1
        if chapter_done < 1:
            print("❌ 新书阶段未固化任何章节")
            return 1
        print(f"✅ 写作固化 {chapter_done} 章")

        # ═══ ④ 续写（continue_book → 再写 1 桥段）═══
        stage("④ 续写", "continue_book 恢复引擎 → 写下一桥段")
        engine2 = NovelEngine(llm_client=llm)
        engine2.continue_book(book_id)
        cont_events = list(engine2._write_next_bridge_stream())
        cont_err = [e for e in cont_events if e.get("type") == "error"]
        cont_done = [e for e in cont_events if e.get("type") == "chapter_done"]
        print(f"   续写事件: {[e.get('type') for e in cont_events]}")
        if cont_err:
            print("❌ 续写出错:", cont_err[0].get("message"))
            return 1
        print(f"✅ 续写执行成功{'（又固化1章）' if cont_done else ''}")

        # ═══ ⑤ 上架前检查（免费规则）═══
        stage("⑤ 上架检查", f"Publisher.build_report（阈值 {args.min_total_words}字/{args.min_chapters}章）")
        pub = Publisher(engine2.book_mgr)
        book = engine2.book
        report = pub.build_report(
            book, timeline=engine2.timeline,
            outline=bm.get_outline(book_id),
            thresholds={"min_total_words": args.min_total_words, "min_chapters": args.min_chapters})
        for item in report.items:
            print(f"   [{'✅' if item.passed else '❌'}] {item.label}: {item.detail}")
        print(f"   → can_publish={report.can_publish} · 共 {report.chapters} 章 / {report.total_words} 字")
        if len(report.items) != 5 or report.total_words <= 0:
            print("❌ 上架检查项数量或字数异常")
            return 1

        # ═══ ⑥ 完本 + 上架（状态机）═══
        stage("⑥ 完本 + 上架", "mark_finished → publish")
        mf = pub.mark_finished(book_id)
        print(f"   完本: status={mf.get('status')} · finished_at={mf.get('finished_at')} · total_words={mf.get('total_words')}")
        if mf.get("status") != "finished":
            print("❌ 完本标记失败")
            return 1
        pb = pub.publish(book_id, force=True)
        print(f"   上架: status={pb.get('status')} · published_at={pb.get('published_at')}")
        if pb.get("status") != "published" or not pb.get("published_at"):
            print("❌ 上架标记失败")
            return 1

        # ═══ ⑦ 导出投稿包 ═══
        stage("⑦ 导出", "export_book → meta.json + 逐章 txt + zip")
        ex = pub.export_book(book_id)
        export_dir = ex.get("export_dir", "")
        files = ex.get("files", [])
        chapter_txts = [f for f in files if f.startswith("第") and f.endswith(".txt")]
        has_meta = os.path.exists(os.path.join(export_dir, "meta.json")) if export_dir else False
        has_zip = os.path.exists(ex.get("zip_path", "")) if ex.get("zip_path") else False
        print(f"   导出目录: {export_dir}")
        for f in sorted(files):
            print(f"   - {f}")
        if not (has_meta and chapter_txts and has_zip):
            print("❌ 导出缺少 meta.json / 章节txt / zip")
            return 1
        print(f"✅ 导出 {ex.get('chapter_count')} 章 / {ex.get('total_words')} 字 / {len(files)} 个文件")

        # ═══ ⑧ 清理 ═══
        stage("⑧ 清理", "delete 测试书")
        if args.keep_book:
            print(f"⏸ --keep-book 已传，保留测试书 {book_id}")
        else:
            bm.delete(book_id)
            if os.path.exists(os.path.join("books", book_id)):
                print("❌ 测试书清理失败")
                return 1
            print(f"✅ 测试书 {book_id} 已清理")

        print(f"\n{'='*60}\n✅ 全流程模拟通过：创建 → 大纲 → 写作 → 续写 → 上架 → 导出（总耗时 {time.time()-t0:.1f}s）\n{'='*60}")
        return 0

    except KeyboardInterrupt:
        print("\n⏹ 已中断")
        return 130
    except Exception as e:
        import traceback
        print("❌ 模拟失败:", e)
        traceback.print_exc()
        if book_id and not args.keep_book:
            try:
                bm.delete(book_id)
                print(f"⚠️ 已清理失败现场测试书 {book_id}")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
