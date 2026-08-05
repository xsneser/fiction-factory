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
        theme_count=len(theme_lib.entries),
        engine_count=len(_engines),
    )


# ═══════════════════════════════════════════
# 🔰 新书启动（两步走）
@bp.route("/books/start", methods=["GET", "POST"])
# ═══════════════════════════════════════════

def start_new_book():
    """新书启动 — v2: 先创建故事线配置，再跳转编辑器"""
    if request.method == "POST":
        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置"}), 500

        pen_name = request.form.get("pen_name", "")
        genre = request.form.get("genre", "")
        sub_genre = request.form.get("sub_genre", "")
        platform = request.form.get("platform", "fanqie")

        # 创建故事线配置
        storyline = BookStoryline(
            book_title=request.form.get("title", ""),
            genre=genre,
            sub_genre=sub_genre,
            words_per_chapter=parse_int(request.form.get("words_per_chapter"), 3000, min_value=500, max_value=20000),
            pen_name=pen_name,
            platform=platform,
            basic_info={
                "protagonist": {
                    "name": request.form.get("protag_name", ""),
                    "identity": request.form.get("protag_identity", ""),
                    "personality": request.form.get("protag_personality", ""),
                    "golden_finger": request.form.get("protag_golden_finger", ""),
                },
                "world_building": {
                    "description": request.form.get("world_desc", ""),
                },
            },
            phase="config",
        )

        # 如果用户给了故事线描述，立即用 AI 生成大纲序列
        storyline_hint = request.form.get("storyline_hint", "")
        if storyline_hint:
            builder = StorylineBuilder(
                structure_lib=struct_lib,
                plot_lib=plot_lib,
                gag_lib=gag_lib,
                theme_lib=theme_lib,
                llm_client=llm,
            )
            storyline.outlines = builder.build_outline_sequence(
                genre=genre, sub_genre=sub_genre, custom_context=storyline_hint)
            storyline.phase = "outlines"

        # 直接建正式书（规划书=书目录内的书；草稿目录已废弃）
        book = book_mgr.create(
            title=storyline.book_title or "(待定)",
            pen_name=pen_name,
            genre=genre,
            sub_genre=sub_genre,
            platform=platform,
            chapter_count=max((o.end_chapter for o in storyline.outlines), default=500),
            structure_template_id="storyline",
            style_profile_id="",
        )
        book_mgr.save_storyline(book.book_id, storyline)

        return redirect(url_for("storyline.storyline_edit", storyline_id=book.book_id))

    return render_template("start_book.html",
        pen_names=profiles.list_all(),
        structures=struct_lib.templates,
        openings=plot_lib.search(category="开篇"),
        golden_fingers=plot_lib.search(category="成长") + plot_lib.search(category="爽文"),
    )


# ═══════════════════════════════════════════
# 大纲生成（已内嵌到启动新书 / 续写流程）
@bp.route("/books/generator")
# ═══════════════════════════════════════════

def outline_generator_page():
    """大纲生成器已内嵌到「启动新书」流程（故事线编辑器：一键生成完整大纲）"""
    return redirect(url_for("dashboard.start_new_book"))


