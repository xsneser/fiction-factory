"""书库（图书列表/详情/删除） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

bp = Blueprint("books", __name__)
logger = logging.getLogger("novel-engine.web")

# ═══════════════════════════════════════════
# 原有路由（保留兼容）
@bp.route("/books")
# ═══════════════════════════════════════════

def books():
    rows = _book_rows()
    return render_template("books.html", books=rows)


_book_rows_cache: dict = {}  # book_id -> (mtimes, row)


def next_step_for(book, has_storyline: bool = False,
                  world_done: bool = False, outlines_count: int = 0) -> dict:
    """根据图书状态给出「下一步」动作（供书库/仪表盘列表渲染）。

    设定先行规划书（无章节）：无世界观 → 🌍 生成世界观；有世界无大纲 → 📋 生成大纲；
    有大纲 → ✍️ 开始写作。每个状态只给一个明确的下一步。
    """
    bid = book.book_id
    if book.status in ("ready", "planning"):
        if (book.current_chapter or 0) == 0:
            if not has_storyline:
                return {"label": "查看详情", "href": f"/books/{bid}", "step": 2}
            if not world_done:
                return {"label": "🌍 生成世界观", "href": f"/books/{bid}/world", "step": 2}
            if outlines_count == 0:
                return {"label": "📋 生成大纲", "href": f"/books/{bid}/continue", "step": 3}
            return {"label": "✍️ 开始写作", "href": f"/books/{bid}/continue", "step": 4}
        return {"label": "继续写作", "href": f"/books/{bid}/continue", "step": 4}
    if book.status in ("writing", "reviewing"):
        return {"label": "继续写作", "href": f"/books/{bid}/continue", "step": 4}
    if book.status == "finished":
        return {"label": "上架出版", "href": f"/books/{bid}/publish", "step": 6}
    if book.status == "published":
        return {"label": "查看上架", "href": f"/books/{bid}/publish", "step": 6}
    return {"label": "查看详情", "href": f"/books/{bid}", "step": 2}


def _book_sig(bid: str):
    """books/{id} 三份关键文件的 mtime，用于判断行级缓存是否仍有效。"""
    paths = (f"books/{bid}/book.json", f"books/{bid}/storyline.json",
             f"books/{bid}/outline/outline.json")
    return tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)


def _book_rows():
    """书库共用：每本书附带故事线/大纲元数据（故事线已并入书目录，无独立草稿）。
    行级缓存 key = 三份文件 mtime；后续导航只做 stat，不再 parse 大 JSON。"""
    books = book_mgr.list_all()
    valid_ids = set()
    rows = []
    for b in books:
        valid_ids.add(b.book_id)
        sig = _book_sig(b.book_id)
        cached = _book_rows_cache.get(b.book_id)
        if cached and cached[0] == sig:
            rows.append(cached[1])
            continue
        sl = book_mgr.load_storyline(b.book_id)
        outline = book_mgr.get_outline(b.book_id)
        from libraries.storyline import basic_info_world_done
        world_done = basic_info_world_done(sl.basic_info if sl else None)
        row = {
            "book": b,
            "has_storyline": sl is not None,
            "storyline_outlines": len(sl.outlines) if sl else 0,
            "storyline_plots": len(sl.plots) if sl else 0,
            "outline_count": len((outline or {}).get("stages", [])) if outline else 0,
            "next": next_step_for(b, has_storyline=sl is not None,
                                  world_done=world_done,
                                  outlines_count=len(sl.outlines) if sl else 0),
        }
        _book_rows_cache[b.book_id] = (sig, row)
        rows.append(row)

    for key in list(_book_rows_cache):
        if key not in valid_ids:
            del _book_rows_cache[key]
    return rows


def _basic_info_from_outline(outline, book):
    """无 timeline 的书（旧引擎路径）：从结构大纲尽力还原 basic_info 供详情页展示。"""
    bi = {"protagonist": {}, "world_building": {}, "supporting_cast": [],
          "tone": "", "target_audience": "", "synopsis": ""}
    if not outline:
        return bi
    bi["synopsis"] = outline.get("synopsis", "") or ""
    chars = outline.get("characters", []) or []
    if isinstance(chars, list):
        for c in chars:
            if not isinstance(c, dict):
                continue
            if not bi["protagonist"]:
                bi["protagonist"] = c
            else:
                bi["supporting_cast"].append(c)
    return bi


@bp.route("/books/<book_id>")
def book_detail(book_id):
    book = book_mgr.get(book_id)
    if not book: return "Not found", 404
    outline = book_mgr.get_outline(book_id)
    storyline = book_mgr.load_storyline(book_id)
    if storyline:
        basic_info = dict(storyline.basic_info or {})  # 拷贝再打标，避免就地污染缓存对象
        basic_info["has_storyline"] = True
    else:
        basic_info = _basic_info_from_outline(outline, book)
        basic_info["has_storyline"] = False
    # 简介存在 outline.json，合并进 basic_info 供详情页显示
    if outline and (outline.get("synopsis") or ""):
        basic_info.setdefault("synopsis", outline["synopsis"])
    from core.text_utils import count_prose_units
    chapters = []
    for n in range(1, book.current_chapter + 2):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            ch["word_count"] = count_prose_units(ch.get("content") or "")
            chapters.append(ch)
    cost_path = f"books/{book_id}/cost.json"
    cost = CostTracker.load(cost_path) if os.path.exists(cost_path) else CostTracker()
    csm = CharacterStateMachine()
    char_path = f"books/{book_id}/character_states.json"
    if os.path.exists(char_path): csm.load(char_path)
    # 进行中章节草稿（按桥段撰写中断时落盘；详情页展示未固化内容，写作台才有写入）
    draft = None
    draft_path = f"books/{book_id}/draft_chapter.json"
    if os.path.exists(draft_path):
        try:
            with open(draft_path, encoding="utf-8") as f:
                _d = json.load(f)
            _buf = _d.get("buffer") or []
            if _buf:
                draft = {
                    "chapter_num": _d.get("chapter_num", 0),
                    "text": "\n\n".join(_buf),
                    "words": count_prose_units("\n\n".join(_buf)),
                }
        except Exception as e:
            logger.warning("读取章节草稿失败: %s", e)
    from libraries.storyline import basic_info_world_done
    world_done = basic_info_world_done(storyline.basic_info if storyline else None)
    has_outlines = bool(storyline and storyline.outlines)
    # 设定表单「从已有书借鉴」所需：其他有设定(故事线)的书
    borrow_books = []
    for b in book_mgr.list_all():
        if b.book_id == book_id:
            continue
        src = book_mgr.load_storyline(b.book_id)
        if src and (src.basic_info or {}):
            borrow_books.append({"book_id": b.book_id,
                                 "title": src.book_title or b.title,
                                 "genre": src.genre or b.genre})
    return render_template("book_detail.html", book=book,
        outline=outline, chapters=chapters,
        storyline=storyline,
        basic_info=basic_info,
        cost=cost.summary(), characters=csm.characters, draft=draft,
        world_done=world_done, has_outlines=has_outlines,
        borrow_books=borrow_books)


@bp.route("/api/book/<book_id>/generate-meta", methods=["POST"])
def api_book_generate_meta(book_id):
    """书库详情页：手动生成书名+简介（基于第 1 章内容）。

    从"第 1 章写完自动触发"改为详情页独立动作。
    生成结果写 book.json（title）/ storyline.json（book_title）/ outline.json（synopsis）。
    """
    if not book_mgr.get(book_id):
        return jsonify({"ok": False, "error": "not found"}), 404
    ch1 = book_mgr.load_chapter(book_id, 1)
    if not ch1 or not ch1.get("content"):
        return jsonify({"ok": False, "error": "尚无第 1 章正文，请先写作再生成书名/简介"}), 400
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置"}), 500
    try:
        from libraries.engine import NovelEngine
        engine = NovelEngine(llm_client=llm)
        engine.continue_book(book_id)   # 恢复 book/storyline（无 storyline 会报错）
        result = engine._generate_book_meta(ch1["content"])
        # 使 web_ui 的 book 缓存失效，下次详情页加载读到磁盘新值
        book_mgr._cache.pop(book_id, None)
        _engines.pop(f"cont_{book_id}", None)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/books/<book_id>/delete", methods=["POST"])
def delete_book(book_id):
    book_mgr.delete(book_id)
    return redirect(url_for("books.books"))


