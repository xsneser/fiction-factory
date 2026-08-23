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


def _book_sig(bid: str):
    """books/{id} 关键文件 + cost.json + 章节文件 mtime，用于判断行级缓存是否仍有效。

    chapters 目录取全部章节文件的最大 mtime（新增/修改章节都会使字数/花费变化）。
    """
    paths = (f"books/{bid}/book.json", f"books/{bid}/storyline.json",
             f"books/{bid}/outline/outline.json", f"books/{bid}/cost.json")
    base = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)
    cdir = f"books/{bid}/chapters"
    ctime = 0
    if os.path.isdir(cdir):
        times = [os.path.getmtime(os.path.join(cdir, f))
                 for f in os.listdir(cdir) if f.endswith(".json")]
        if times:
            ctime = max(times)
    return base + (ctime,)


def _book_word_count(bid: str) -> int:
    """该书已写章节总字数：遍历 chapters/*.json 累加 word_count，缺字段时按正文算。"""
    cdir = os.path.join("books", bid, "chapters")
    if not os.path.isdir(cdir):
        return 0
    from core.text_utils import count_prose_units
    total = 0
    for f in os.listdir(cdir):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, f), encoding="utf-8") as fp:
                ch = json.load(fp)
            wc = int(ch.get("word_count") or 0)
            if not wc:
                wc = count_prose_units(ch.get("content") or "")
            total += wc
        except Exception:
            continue
    return total


def _book_cost_spent(bid: str):
    """该书已花费（元，CostTracker 持久化 books/<id>/cost.json）；无记录返回 None。"""
    cost_path = os.path.join("books", bid, "cost.json")
    if not os.path.exists(cost_path):
        return None
    try:
        from libraries.cost_tracker import CostTracker
        return CostTracker.load(cost_path).spent or 0.0
    except Exception:
        return None


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
        # 题材标签（建书向导选定，存于 world_building.tags）；无标签的老书回退书级 genre
        tags = []
        if sl and (sl.basic_info or {}):
            tags = ((sl.basic_info.get("world_building") or {}).get("tags") or [])
        # genre 已从模型移除；无标签老书 tags 留空（展示层标「未选标签」）
        row = {
            "book": b,
            "tags": tags,
            "has_storyline": sl is not None,
            "storyline_outlines": len(sl.outlines) if sl else 0,
            "storyline_plots": len(sl.plots) if sl else 0,
            "outline_count": len((outline or {}).get("stages", [])) if outline else 0,
            "word_count": _book_word_count(b.book_id),
            "cost_spent": _book_cost_spent(b.book_id),
        }
        _book_rows_cache[b.book_id] = (sig, row)
        rows.append(row)

    for key in list(_book_rows_cache):
        if key not in valid_ids:
            del _book_rows_cache[key]
    return rows


def _basic_info_from_outline(outline, book):
    """无 timeline 的书（旧引擎路径）：从结构大纲尽力还原 basic_info 供详情页展示。"""
    bi = {"characters": [], "world_building": {},
          "tone": "", "target_audience": "", "synopsis": ""}
    if not outline:
        return bi
    bi["synopsis"] = outline.get("synopsis", "") or ""
    chars = outline.get("characters", []) or []
    if isinstance(chars, list):
        first = True
        for c in chars:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            item = dict(c)
            item.setdefault("role", "主角" if first else "配角")
            item.setdefault("relations", [])
            bi["characters"].append(item)
            first = False
    return bi


@bp.route("/books/<book_id>")
def book_detail(book_id):
    book_mgr.list_all()   # mtime 感知重扫：外部进程（MCP）写入 book.json 后读到新值
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
    # 「从已有书借鉴」已挪到启动新书向导②（dashboard GET 提供 borrow_books），详情页不再传
    return render_template("book_detail.html", book=book,
        outline=outline, chapters=chapters,
        storyline=storyline,
        basic_info=basic_info,
        draft=draft)


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

    from plugins import task_manager
    task_manager.ensure_single("生成书名/简介")
    tid = f"meta_{book_id}_{int(time.time())}"
    _book_meta = book_mgr.get(book_id)
    task_manager.start(tid, name="生成书名/简介",
                       title=getattr(_book_meta, "title", "") or "",
                       url=f"/books/{book_id}")
    try:
        from libraries.engine import NovelEngine
        engine = NovelEngine(llm_client=llm)
        engine.continue_book(book_id)   # 恢复 book/storyline（无 storyline 会报错）
        result = engine._generate_book_meta(ch1["content"])
        task_manager.log(tid, f"生成书名「{result.get('title', '')}」", "success")
        task_manager.done(tid, message="书名/简介生成完成")
        # 使 web_ui 的 book 缓存失效，下次详情页加载读到磁盘新值
        book_mgr._cache.pop(book_id, None)
        _engines.pop(f"cont_{book_id}", None)
        return jsonify({"ok": True, **result})
    except Exception as e:
        task_manager.fail(tid, str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/books/<book_id>/delete", methods=["POST"])
def delete_book(book_id):
    book_mgr.delete(book_id)
    return redirect(url_for("books.books"))


# ═══════════════════════════════════════════
# 竞品规则层组件 UI 化：书详情运行时面板（只读/按需算）
# ═══════════════════════════════════════════

@bp.route("/api/book/<book_id>/character-states")
def api_character_states(book_id):
    """书详情：角色状态面板（books/<id>/character_states.json，写作时落盘）。"""
    from libraries.character_state import CharacterStateMachine
    path = os.path.join("books", book_id, "character_states.json")
    if not os.path.exists(path):
        return jsonify({"characters": [], "warnings": []})
    try:
        csm = CharacterStateMachine()
        csm.load(path)
        return jsonify({"characters": csm.to_dict().get("characters", []),
                        "warnings": csm.warnings()})
    except Exception as e:
        logger.warning("读取角色状态失败: %s", e)
        return jsonify({"characters": [], "warnings": [], "error": str(e)})


@bp.route("/api/book/<book_id>/promises")
def api_book_promises(book_id):
    """书详情：读者承诺台账（scan_promises 规则层扫描 + op 分级）。"""
    from libraries.promise_ledger import scan_promises
    tl = book_mgr.load_storyline(book_id)
    empty = {"counts": {"total": 0}, "overdue": [], "advanced": [],
             "stalled": [], "fulfilled_recently": [], "suggestions": []}
    if not tl:
        return jsonify(empty)
    book = book_mgr.get(book_id)
    cur = int((book.current_chapter if book else 0) or 1)
    chapters = []
    for n in range(1, (book.current_chapter if book else 0) + 1):
        ch = book_mgr.load_chapter(book_id, n)
        if ch:
            chapters.append({"num": ch.get("num", n), "content": ch.get("content", "")})
    return jsonify(scan_promises(tl, chapters, cur))


@bp.route("/api/book/<book_id>/diagnose", methods=["POST"])
def api_book_diagnose(book_id):
    """书详情：质量诊断（连续性/追读/承诺，规则层零成本聚合）。

    复用 agent_tools 的三个 diagnose_*（MCP 同源），点按钮跑一次全量扫描。
    """
    import agent_tools
    try:
        return jsonify({
            "continuity": agent_tools.diagnose_continuity(book_id),
            "retention": agent_tools.diagnose_retention(book_id),
            "promises": agent_tools.diagnose_promises(book_id),
        })
    except Exception as e:
        logger.warning("诊断失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ═══ 历史快照（diff 审查 + 回滚；写工具落库前自动留底） ═══

@bp.route("/api/book/<book_id>/snapshots")
def api_book_snapshots(book_id):
    """书详情：历史快照列表（新→旧）。"""
    from libraries.book_snapshot import list_snapshots
    return jsonify({"book_id": book_id, "snapshots": list_snapshots(book_id)})


@bp.route("/api/book/<book_id>/snapshots/<snapshot_id>/diff")
def api_book_snapshot_diff(book_id, snapshot_id):
    """书详情：某次快照 vs 当前的 unified diff（决策点落库前的人审预览）。"""
    from libraries.book_snapshot import preview_diff
    return jsonify(preview_diff(book_id, snapshot_id))


@bp.route("/api/book/<book_id>/snapshots/<snapshot_id>/rollback", methods=["POST"])
def api_book_snapshot_rollback(book_id, snapshot_id):
    """书详情：回滚到某次快照（回滚前先自动留底一次，防误回滚）。"""
    from libraries.book_snapshot import snapshot as _snap, rollback as _rb
    _snap(book_id, "rollback_backup")   # 回滚前留底，保证可逆
    return jsonify(_rb(book_id, snapshot_id))


@bp.route("/api/book/<book_id>/chapter/<int:chapter_num>/review", methods=["POST"])
def api_chapter_review(book_id, chapter_num):
    """书内一键审查本章（ContentReviewer 规则层）。"""
    ch = book_mgr.load_chapter(book_id, chapter_num)
    if not ch or not ch.get("content"):
        return jsonify({"ok": False, "error": f"第{chapter_num}章无正文"}), 400
    from libraries.reviewer import ContentReviewer
    r = ContentReviewer().review(ch["content"], chapter_num=chapter_num)
    return jsonify({
        "ok": True, "passed": r.passed, "score": r.score, "summary": r.summary,
        "issues": [{"severity": i.severity, "category": i.category,
                    "description": i.description, "location": i.location,
                    "suggestion": i.suggestion} for i in r.issues],
    })


@bp.route("/api/book/<book_id>/chapter/<int:chapter_num>/deai", methods=["POST"])
def api_chapter_deai(book_id, chapter_num):
    """书内一键去AI味本章（DeAIEngine 规则层）。"""
    ch = book_mgr.load_chapter(book_id, chapter_num)
    if not ch or not ch.get("content"):
        return jsonify({"ok": False, "error": f"第{chapter_num}章无正文"}), 400
    from libraries.de_ai import DeAIEngine
    r = DeAIEngine().process_rule_based(ch["content"])
    return jsonify({"ok": True, "processed": r.processed,
                    "word_replacements": r.word_replacements,
                    "sentences_split": r.sentences_split,
                    "llm_rewritten": r.llm_rewritten,
                    "processed_length": len(r.processed)})


@bp.route("/api/book/<book_id>/chapter/<int:chapter_num>/punch-points", methods=["POST"])
def api_chapter_punch_points(book_id, chapter_num):
    """书详情：对指定章节跑爽点标注（tag_generator 规则层），落盘 tags.json 并返回。"""
    from libraries.tag_generator import tag_chapter
    ch = book_mgr.load_chapter(book_id, chapter_num)
    if not ch or not ch.get("content"):
        return jsonify({"ok": False, "error": f"第{chapter_num}章无正文"}), 400
    res = tag_chapter(ch["content"])
    tags = res.get("tags", []) if isinstance(res, dict) else res
    out = {"chapter": chapter_num, "tags": tags, "ok": True}
    try:
        with open(os.path.join("books", book_id, "tags.json"), "w", encoding="utf-8") as f:
            json.dump({"chapter": chapter_num, "tags": tags}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    return jsonify(out)


