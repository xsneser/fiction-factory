"""首页 Dashboard + 新书启动 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *
from .books import _book_rows
bp = Blueprint("dashboard", __name__)

# ═══════════════════════════════════════════
# 首页 Dashboard
@bp.route("/")
# ═══════════════════════════════════════════
def dashboard():
    rows = _book_rows()
    pen_names = profiles.list_all()
    return render_template("dashboard.html",
        books=rows, pen_names=pen_names,
        plot_count=len(plot_lib.templates),
        struct_count=len(struct_lib.templates),
        gag_count=len(gag_lib.patterns),
        char_count=len(char_lib.archetypes),
        engine_count=len(_engines),
    )


# ═══════════════════════════════════════════
# 🔰 新书启动（单页多步向导）
# ═══════════════════════════════════════════

def _borrow_books(exclude_book_id: str = ""):
    """可借鉴的源书列表（有非空 basic_info 的其他书）—— 供启动向导②与旧详情页。"""
    rows = []
    for b in book_mgr.list_all():
        if b.book_id == exclude_book_id:
            continue
        src = book_mgr.load_storyline(b.book_id)
        if src and (src.basic_info or {}):
            rows.append({"book_id": b.book_id,
                         "title": src.book_title or b.title,
                         "genre": src.genre or b.genre})
    return rows


@bp.route("/books/start", methods=["GET", "POST"])
def start_new_book():
    """新书启动 — v4 单页多步向导：一句话设定 → 挑选世界观(标签) → 流派 → 大纲 → 前三章。

    POST 双轨：
      - JSON（向导用）：body 含 tags/borrow 等全部字段 → 返回 {"ok", "book_id", "redirect"}
      - form（兼容旧入口/smoke 测试）：保留 302 重定向
    """
    if request.method == "POST":
        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置"}), 500

        is_json = request.is_json
        data = request.get_json(silent=True) or {} if is_json else {}
        src = data if is_json else request.form

        pen_name = src.get("pen_name", "")
        genre = src.get("genre", "")
        sub_genre = src.get("sub_genre", "")
        platform = src.get("platform", "") or "fanqie"

        # 一句话设定（主入口）→ 存 world_building.description（世界观由它直接生成，不再追加）
        world_idea = (src.get("world_idea", "") or "").strip()
        description = world_idea

        # 题材标签（番茄式硬约束，向导③多选 chips）；流派与标签同源，为空时从标签推导
        tags = data.get("tags") if is_json else []
        if not isinstance(tags, list):
            tags = []
        if not genre:
            from libraries.world_tags import derive_genre
            genre = derive_genre(tags)

        # 主角 + 配角（向导③可多选/多次生成/手动编辑）：JSON 带 characters 数组直接用；
        # form（smoke 兼容）回退 protag_* 单主角
        characters = data.get("characters") if is_json else None
        if not isinstance(characters, list) or not characters:
            characters = [{
                "name": src.get("protag_name", ""),
                "role": "主角",
                "importance": 1,
                "identity": src.get("protag_identity", ""),
                "personality": src.get("protag_personality", ""),
                "golden_finger": src.get("protag_golden_finger", ""),
                "gender": "", "catchphrase": "", "brief": "", "title": "",
                "age": 0, "death_year": 0, "archetype_id": "", "relations": [],
            }]

        basic_info = {
            "characters": characters,
            "world_building": {"description": description, "tags": tags},
        }
        # 故事线想法：不再立即生成大纲，存入 basic_info 供「一键生成完整大纲」使用
        storyline_hint = (src.get("storyline_hint", "") or "").strip()
        if storyline_hint:
            basic_info["storyline_hint"] = storyline_hint

        # 创建故事线配置
        storyline = BookStoryline(
            book_title=src.get("title", ""),
            genre=genre,
            sub_genre=sub_genre,
            words_per_chapter=parse_int(src.get("words_per_chapter"), 3000, min_value=500, max_value=20000),
            pen_name=pen_name,
            platform=platform,
            basic_info=basic_info,
            phase="config",
        )

        # 直接建正式书（规划书=书目录内的书；草稿目录已废弃）
        book = book_mgr.create(
            title=storyline.book_title or "(待定)",
            pen_name=pen_name,
            genre=genre,
            sub_genre=sub_genre,
            platform=platform,
            chapter_count=500,
            structure_template_id="storyline",
            style_profile_id="",
        )
        book_mgr.save_storyline(book.book_id, storyline)

        # 向导（JSON）返回 book_id 供前端接续生成；旧 form 入口保留 302
        if is_json:
            return jsonify({"ok": True, "book_id": book.book_id,
                            "redirect": url_for("books.book_detail", book_id=book.book_id)})
        return redirect(url_for("books.book_detail", book_id=book.book_id))

    from libraries.world_tags import WORLD_TAG_GROUPS
    return render_template("start_book.html",
        pen_names=profiles.list_all(),
        structures=struct_lib.templates,
        openings=plot_lib.search(category="开篇"),
        golden_fingers=plot_lib.search(category="成长") + plot_lib.search(category="爽文"),
        borrow_books=_borrow_books(),
        world_tags=WORLD_TAG_GROUPS,
    )


# ═══════════════════════════════════════════
# 大纲生成（已内嵌到启动新书 / 续写流程）
@bp.route("/books/generator")
# ═══════════════════════════════════════════

def outline_generator_page():
    """大纲生成器已内嵌到「启动新书」流程（故事线编辑器：一键生成完整大纲）"""
    return redirect(url_for("dashboard.start_new_book"))


