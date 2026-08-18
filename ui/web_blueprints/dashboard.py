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
        excerpt_count=len(example_lib.excerpts),
        engine_count=len(_engines),
    )


# ═══════════════════════════════════════════
# 🔰 新书启动（两步走）
@bp.route("/books/start", methods=["GET", "POST"])
# ═══════════════════════════════════════════

def start_new_book():
    """新书启动 — v3 设定先行：一句话设定 → 世界观设定卡 → 再进大纲生成。

    主表单只留一句话设定 + 流派 + 笔名 + 平台；书名/主角/世界观/模板收进『高级设置』折叠。
    """
    if request.method == "POST":
        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置"}), 500

        pen_name = request.form.get("pen_name", "")
        genre = request.form.get("genre", "")
        sub_genre = request.form.get("sub_genre", "")
        platform = request.form.get("platform", "fanqie")

        # 一句话设定（主入口）→ 存 world_building.description；高级世界观简述追加
        world_idea = (request.form.get("world_idea", "") or "").strip()
        advanced_world = (request.form.get("world_desc", "") or "").strip()
        description = world_idea
        if advanced_world:
            description = (world_idea + "。" + advanced_world) if world_idea else advanced_world

        basic_info = {
            "protagonist": {
                "name": request.form.get("protag_name", ""),
                "identity": request.form.get("protag_identity", ""),
                "personality": request.form.get("protag_personality", ""),
                "golden_finger": request.form.get("protag_golden_finger", ""),
            },
            "world_building": {"description": description},
        }
        # 故事线想法：不再立即生成大纲，存入 basic_info 供「一键生成完整大纲」使用
        storyline_hint = (request.form.get("storyline_hint", "") or "").strip()
        if storyline_hint:
            basic_info["storyline_hint"] = storyline_hint

        # 创建故事线配置
        storyline = BookStoryline(
            book_title=request.form.get("title", ""),
            genre=genre,
            sub_genre=sub_genre,
            words_per_chapter=parse_int(request.form.get("words_per_chapter"), 3000, min_value=500, max_value=20000),
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

        # 设定先行：设定已并入书详情页（内嵌可编辑表单），生成/确认后进大纲生成
        return redirect(url_for("books.book_detail", book_id=book.book_id))

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


