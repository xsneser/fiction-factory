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
    from libraries import style_samples
    _ss = style_samples.load_samples()
    return render_template("dashboard.html",
        books=rows, pen_names=pen_names,
        plot_count=len(plot_lib.templates),
        struct_count=len(struct_lib.roots()),
        gag_count=len(gag_lib.patterns),
        char_count=len(char_lib.archetypes),
        style_rule_count=len(style_rules.rules),
        sample_count=len(_ss) if _ss is not None else 0,
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
                         "tags": ((src.basic_info or {}).get("world_building") or {}).get("tags") or []})
    return rows


@bp.route("/books/start", methods=["GET", "POST"])
def start_new_book():
    """新书启动 — v4 单页多步向导：一句话设定（含题材标签）→ AI 候选挑世界观 → 微调设定 → 生成大纲 → 前三章撰写。

    POST 双轨：
      - JSON（向导用）：body 含 tags/borrow 等全部字段 → 返回 {"ok", "book_id", "redirect"}
      - form（兼容旧入口/smoke 测试）：保留 302 重定向
    """
    if request.method == "POST":
        is_json = request.is_json
        data = request.get_json(silent=True) or {} if is_json else {}
        src = data if is_json else request.form
        build_session_id = str(data.get("build_session_id") or "").strip() if is_json else ""
        if build_session_id:
            from libraries.planning_state import save_build_session
            outline_preview = data.get("_outline_data") or {}
            committed = max((int(o.get("end_word") or 0)
                             for o in (outline_preview.get("outlines") or []) if isinstance(o, dict)), default=0)
            save_build_session(build_session_id, {
                "mode": "open",
                "target_word_budget": max(int(data.get("target_word_budget") or 0), committed),
                "committed_until_word": committed,
                "future_intents": data.get("future_intents") or [],
                "story_questions": data.get("story_questions") or [],
                "decision_points": data.get("decision_points") or [],
            })
        llm = get_llm()
        if not llm:
            return jsonify({"error": "LLM 未配置"}), 500

        # 新流程：步 3 ②生成的大纲+情节段（generate_outline_preview 产出，set_outline 存入）
        outline_data = data.get("_outline_data") if is_json else None
        if not isinstance(outline_data, dict):
            outline_data = None
        if outline_data:
            from libraries.planning_state import enabled
            if enabled("INCREMENTAL_STORY_PLANNING", False):
                committed = max((int(o.get("end_word") or 0)
                                 for o in (outline_data.get("outlines") or []) if isinstance(o, dict)), default=0)
                if committed > 30000:
                    return jsonify({"ok": False, "error": "incremental_horizon_exceeded",
                                    "committed_until_word": committed,
                                    "max_initial_committed_words": 30000,
                                    "action": "只保留开篇承诺区，远期方向写入 future_intents"}), 400

        pen_name = src.get("pen_name", "")
        platform = src.get("platform", "") or "fanqie"

        # 一句话设定（主入口）→ 存 world_building.description（世界观由它直接生成，不再追加）
        # 向导 JS 发 idea；旧 form 入口发 world_idea → 两者都收，避免一句话设定存成空串
        world_idea = (src.get("idea") or src.get("world_idea", "") or "").strip()
        description = world_idea

        # 题材标签（番茄式硬约束，向导①多选 chips）；题材方向与标签同源，恒由标签推导
        # （genre/sub_genre 已从可见面移除，仅内部 book.json 保留；修复曾引用未定义 genre 的 NameError）
        tags = data.get("tags") if is_json else []
        if not isinstance(tags, list):
            tags = []
        from libraries.world_tags import derive_genre
        genre = derive_genre(tags)
        sub_genre = ""

        # 主角 + 配角（向导③可多选/多次生成/手动编辑）：JSON 带 characters 数组直接用；
        # form（smoke 兼容）回退 protag_* 单主角
        characters = data.get("characters") if is_json else None
        if not isinstance(characters, list) or not characters:
            # 新流程兜底：步 3 ②大纲管线 Phase 1 产出的临时人物（步 4 未生成/失败时用）
            temp_chars = ((outline_data or {}).get("basic_info") or {}).get("characters") or []
            if temp_chars:
                characters = temp_chars
            else:
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

        # 世界观补全（向导③ JSON）：客户端 world_building 为 12 维 dict；form/旧入口回退 description+tags
        wb = data.get("world_building") if is_json else None
        if not isinstance(wb, dict):
            wb = {}
        wb.setdefault("description", description)
        wb.setdefault("tags", tags)
        from libraries.storyline import DEFAULT_WORLD_BUILDING
        # 新流程兜底：Phase 1 产出的世界观补空（步 3 ⑤未生成/失败时用）
        fallback_wb = ((outline_data or {}).get("basic_info") or {}).get("world_building") or {}
        for k, v in DEFAULT_WORLD_BUILDING.items():
            if k not in wb:
                wb[k] = fallback_wb.get(k, list(v) if isinstance(v, list) else v)

        basic_info = {
            "characters": characters,
            "world_building": wb,
            "tone": (data.get("tone") if is_json else "") or "",
            "target_audience": (data.get("target_audience") if is_json else "") or "",
            "pov": (data.get("pov") if is_json else "") or "第三人称",
            "era_language": (data.get("era_language") if is_json else "") or "",
        }
        # 新流程兜底：基调空时用 Phase 1 产出值
        if is_json and outline_data:
            obi = outline_data.get("basic_info") or {}
            if not basic_info.get("tone"):
                basic_info["tone"] = obi.get("tone", "")
            if not basic_info.get("target_audience"):
                basic_info["target_audience"] = obi.get("target_audience", "")
            if not basic_info.get("era_language"):
                basic_info["era_language"] = obi.get("era_language", "")
        # 世界观已在向导③补全且充实 → 打 _world_generated，让 generate_full_outline 跳过 Phase 1 故事分析
        if is_json:
            from libraries.outline_generator import basic_info_is_rich
            if basic_info_is_rich(basic_info):
                basic_info["_world_generated"] = True
            # 分阶段构建②选定的开篇大纲/情节段（generate_full_outline picks=None 时自动消费）
            picks = data.get("_outline_picks")
            if isinstance(picks, dict) and (picks.get("templates") or picks.get("plots")):
                basic_info["_outline_picks"] = picks
        # 故事线想法：不再立即生成大纲，存入 basic_info 供「一键生成完整大纲」使用
        storyline_hint = (src.get("storyline_hint", "") or "").strip()
        if storyline_hint:
            basic_info["storyline_hint"] = storyline_hint

        # 势力名归一去重兜底（防 agent 把描述塞进括号/同名势力重复）：剥离括号、保留规范名首个
        _factions = (basic_info.get("world_building") or {}).get("factions") or []
        if _factions:
            _norm_seen = {}
            _clean = []
            for _f in _factions:
                _fn = _f if isinstance(_f, str) else (_f or {}).get("name") or ""
                _norm = _fn
                for _o in ("（", "("):
                    _i = _norm.find(_o)
                    if _i > 0:
                        _norm = _norm[:_i].strip()
                if not _norm or _norm in _norm_seen:
                    continue
                _norm_seen[_norm] = True
                if isinstance(_f, str):
                    _clean.append(_norm)
                else:
                    _clean.append({**_f, "name": _norm})
            basic_info.setdefault("world_building", {})["factions"] = _clean

        # 创建故事线配置（BookStoryline 是 @dataclass，无 genre/sub_genre 字段——已随流派移除删除）
        storyline = BookStoryline(
            book_title=src.get("title", ""),
            words_per_chapter=parse_int(src.get("words_per_chapter"), 3000, min_value=500, max_value=20000),
            pen_name=pen_name,
            platform=platform,
            basic_info=basic_info,
            phase="config",
        )

        # 深化并入步3（2026-09-05）：步 3 深化式生成的大纲+情节段随书落库，
        # 用户浏览器点提交即 phase=ready（解锁写作，无书详情二次确认/深化段）。
        # ready 前在此补齐原 confirm-storyline 职责（挂内涵+角色标注）；仅 outlines 无 plots
        # 则留 plots（config/补弧兜底恢复，需用户在书详情确认，见 /api/book/<id>/confirm-storyline）。
        if outline_data and isinstance(outline_data.get("outlines"), list) and outline_data["outlines"]:
            from libraries.storyline import BookStoryline as _BS, annotate_plot_roles
            _tmp = _BS.from_dict({
                "outlines": outline_data["outlines"],
                "plots": outline_data.get("plots", []),
                "threads": outline_data.get("threads", []),
                "themes": outline_data.get("themes", []),
            })
            storyline.outlines = _tmp.outlines
            storyline.plots = _tmp.plots
            storyline.threads = _tmp.threads
            storyline.themes = _tmp.themes
            storyline.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            if _tmp.plots:
                StorylineBuilder(structure_lib=struct_lib, plot_lib=plot_lib,
                                 gag_lib=gag_lib).fill_themes_and_hooks(storyline.plots, storyline)
                storyline.phase = "ready"
            else:
                storyline.phase = "plots"  # 兜底：无情节段不 ready（config/补弧 → 用户书详情确认恢复）
            annotate_plot_roles(storyline)

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
        if build_session_id:
            from libraries.planning_state import attach_build_session
            attach_build_session(build_session_id, book.book_id, storyline, book)

        # 向导（JSON）返回 book_id 供前端接续生成；旧 form 入口保留 302
        # 深化已并入步3、提交即 ready → 跳书详情不再带 ?newdraft=1（无自动深化派发）
        if is_json:
            return jsonify({"ok": True, "book_id": book.book_id,
                            "redirect": url_for("books.book_detail", book_id=book.book_id)})
        return redirect(url_for("books.book_detail", book_id=book.book_id))

    from libraries.world_tags import WORLD_TAG_GROUPS
    return render_template("start_book.html",
        pen_names=profiles.list_all(),
        structures=struct_lib.display_trees(),
        openings=plot_lib.search(category="开篇引入"),
        golden_fingers=plot_lib.search(category="战斗历练") + plot_lib.search(category="谋划布局"),
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
