# -*- coding: utf-8 -*-
"""
书目创建 3+2 桥段测试：2 条大纲（3+2=5 桥段）→ 建书 → 逐桥段写入 → 落盘校验 → 清理

真实 LLM 调用。验证优化方案收尾后"书目创建 + 按桥段撰写"主链路的完整性：
  3+2 故事线结构 → create+save_storyline+continue_book 建书 → 5 次 write_bridge 写入
  → 桥段 written_chapter 落盘 → 章节/草稿持久化 → 清理测试书

用法: python tools/test_book_32.py
"""
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
from libraries.storyline import BookStoryline
from libraries.engine import NovelEngine


def make_llm():
    cfg = json.load(open("api.json", encoding="utf-8"))
    return LLMClient(APIConfig(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        model=cfg.get("model", "deepseek-chat"),
        http_timeout_seconds=cfg.get("http_timeout_seconds", 300),
        verify_ssl=cfg.get("verify_ssl", True),
    ))


def build_storyline():
    """构造 2 条大纲 + 3+2=5 桥段的最小故事线（用真实桥段模板 id）。"""
    outlines = [
        {"id": "o1", "template_id": "", "name": "退婚羞辱开篇", "start_chapter": 1, "end_chapter": 3,
         "stages": [{"name": "开篇冲突", "min_ch": 1, "max_ch": 3,
                     "events": ["当众退婚", "主角隐忍", "家人逼问"]}]},
        {"id": "o2", "template_id": "", "name": "逆袭崛起", "start_chapter": 4, "end_chapter": 5,
         "predecessor": "o1",
         "stages": [{"name": "成长打脸", "min_ch": 1, "max_ch": 2,
                     "events": ["拜师受挫", "擂台立威"]}]},
    ]
    # outline o1 的 3 个桥段
    plots = [
        {"id": "p1", "template_id": "plot_dating_001", "name": "退婚打脸", "category": "爽文",
         "sub_category": "", "outline_id": "o1", "stage_index": 0, "order": 1,
         "template_structure": "[当众羞辱]→[主角沉默隐忍]→[关键时刻展现实力]→[全场震惊]→[对方后悔]",
         "slots": [], "thread_id": "主线", "thread_seq": 1, "cover_beats": 3},
        {"id": "p2", "template_id": "plot_dating_003", "name": "拜师入门", "category": "成长",
         "sub_category": "", "outline_id": "o1", "stage_index": 0, "order": 2,
         "template_structure": "[测试试炼]→[主角表现异常]→[大能注意到]→[破格收徒]",
         "slots": [], "thread_id": "主线", "thread_seq": 2, "cover_beats": 3},
        {"id": "p3", "template_id": "plot_dating_007", "name": "擂台比武", "category": "战斗",
         "sub_category": "", "outline_id": "o1", "stage_index": 0, "order": 3,
         "template_structure": "[大会开场]→[前几轮碾压]→[遭遇强敌]→[逆势翻盘]",
         "slots": [], "thread_id": "主线", "thread_seq": 3, "cover_beats": 3},
        # outline o2 的 2 个桥段
        {"id": "p4", "template_id": "plot_dating_006", "name": "英雄救美", "category": "情感",
         "sub_category": "", "outline_id": "o2", "stage_index": 0, "order": 1,
         "template_structure": "[危境]→[主角察觉]→[权衡犹豫]→[出手]→[逆转危局]",
         "slots": [], "thread_id": "主线", "thread_seq": 4, "cover_beats": 3},
        {"id": "p5", "template_id": "plot_dating_010", "name": "宗门危机", "category": "冲突",
         "sub_category": "", "outline_id": "o2", "stage_index": 0, "order": 2,
         "template_structure": "[危机预兆]→[敌方亮底牌]→[主角扛住压力]→[生死之战]→[胜利]",
         "slots": [], "thread_id": "主线", "thread_seq": 5, "cover_beats": 3},
    ]
    basic_info = {
        "protagonist": {"name": "萧晨", "identity": "被退婚的赘婿",
                        "personality": "隐忍坚韧", "background": "前世程序员",
                        "golden_finger": "打工人系统"},
        "world_building": {"era": "现代都市", "power_system": "系统加点",
                           "factions": [], "rules": ["系统数值全书口径唯一"]},
        "supporting_cast": [],
        "tone": "热血", "target_audience": "男频", "pov": "第三人称",
        "era_language": "禁止晚于2015年的网络新词",
    }
    return {
        "book_title": "退婚赘婿逆袭路", "genre": "都市", "sub_genre": "系统流",
        "words_per_chapter": 800, "pen_name": "测试笔名",
        "basic_info": basic_info, "outlines": outlines, "plots": plots,
        "threads": [{"id": "主线", "name": "主线", "desc": "逆袭"}],
        "themes": ["逆袭"], "global_gags": [], "phase": "ready",
    }


def main():
    t0 = time.time()
    llm = make_llm()
    sl = BookStoryline.from_dict(build_storyline())

    n_o1 = sum(1 for p in sl.plots if p.outline_id == "o1")
    n_o2 = sum(1 for p in sl.plots if p.outline_id == "o2")
    print(f"故事线结构: o1={n_o1} 桥段 + o2={n_o2} 桥段 = {len(sl.plots)} 桥段")
    if not (n_o1 == 3 and n_o2 == 2):
        print("❌ 3+2 桥段结构不符"); return 1

    # ① 建书（规划书即正式书：create + save_storyline，再 continue_book 恢复引擎）
    from libraries.book_manager import BookManager
    bm = BookManager("books")
    book = bm.create(
        title=sl.book_title or "(待定)",
        pen_name=sl.pen_name or "测试笔名",
        genre=sl.genre, sub_genre=sl.sub_genre,
        platform=sl.platform or "fanqie",
        chapter_count=max((o.end_chapter for o in sl.outlines), default=500),
        structure_template_id="storyline")
    # detector_frequency 调大 → 本测试跳过探测器 LLM 调用（省时省钱，只验写作主链路）
    book.detector_frequency = 999
    bm.update(book)
    bm.save_storyline(book.book_id, sl)
    engine = NovelEngine(llm_client=llm)
    state = engine.continue_book(book.book_id)
    book_id = state.book_id
    print(f"① 建书完成: {book_id} | total_chapters={state.total_chapters}")
    if not book_id:
        print("❌ 建书失败"); return 1
    if not bm.get(book_id):
        print("❌ book.json 未落盘"); return 1
    if not bm.load_storyline(book_id):
        print("❌ storyline.json 未落盘"); return 1
    print("   book.json / storyline.json 均已落盘 ✓")

    # ② 逐桥段写入（循环直到 5 个桥段全部 written_chapter>0；
    #    LLM 空响应/预算 skip 会保留桥段待重试，故不按固定 5 次计）
    print("\n② 逐桥段写入（目标 5 个桥段全部落盘）:")
    bridge_written = 0
    chapter_done = 0
    for i in range(12):  # 上限防死循环
        remaining = [p for p in bm.load_storyline(book_id).plots
                     if (p.written_chapter or 0) <= 0]
        if not remaining:
            break
        if i == 11:
            print(f"   ❌ 达到写入上限仍未写完全部桥段，剩余: "
                  f"{[p.name for p in remaining]}")
            bm.delete(book_id)
            return 1
        for evt in engine._write_next_bridge_stream():
            t = evt.get("type")
            if t == "bridge_start":
                print(f"   [{t}] {evt.get('plot_name')} (预计 {evt.get('planned_words')}字)")
            elif t == "bridge_skip":
                print(f"   [⚠️ {t}] {evt.get('reason')}")
            elif t == "chapter_done":
                chapter_done += 1
                print(f"   [chapter_done] 第{evt.get('chapter')}章 {evt.get('word_count')}字")
            elif t in ("complete", "error", "budget_paused"):
                print(f"   [⏹ {t}] {evt.get('message', evt.get('reason', ''))}")
        bridge_written += 1
    print(f"   → 写入请求 {bridge_written} 次, 章节固化 {chapter_done} 章")

    # ③ 校验：所有桥段 written_chapter > 0
    print("\n③ 桥段 written_chapter 落盘校验:")
    sl_after = bm.load_storyline(book_id)
    unwritten = [p.name for p in sl_after.plots if (p.written_chapter or 0) <= 0]
    written = {p.name: p.written_chapter for p in sl_after.plots if (p.written_chapter or 0) > 0}
    for name, ch in written.items():
        print(f"   ✓ {name} → 第{ch}章")
    if unwritten:
        print(f"   ❌ 未写入桥段: {unwritten}")
        bm.delete(book_id)
        return 1
    print(f"   ✓ 5 个桥段全部写入")

    # ④ 章节/草稿持久化
    print("\n④ 落盘内容校验:")
    book = bm.get(book_id)
    print(f"   book.current_chapter = {book.current_chapter}")
    chs = [bm.load_chapter(book_id, n) for n in range(1, book.current_chapter + 1)]
    ok_chs = [c for c in chs if c and c.get("content")]
    print(f"   已固化章节 {len(ok_chs)} 章（累计 {sum(len(c['content']) for c in ok_chs)} 字）")
    draft_path = os.path.join("books", book_id, "draft_chapter.json")
    if os.path.exists(draft_path):
        print(f"   进行中草稿存在（{os.path.getsize(draft_path)} 字节）")

    # ⑤ 清理
    bm.delete(book_id)
    print(f"\n⑤ 测试书 {book_id} 已清理")
    print(f"✅ 书目创建 3+2 桥段测试通过（总耗时 {time.time()-t0:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
