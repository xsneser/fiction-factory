"""世界观设定卡（启动新书前置 v3）— 蓝图（自 ui/web_ui.py 按域拆分）。

启动新书改为「设定先行」：一句话设定 → 世界观设定卡（生成/示例候选/从书借鉴/逐项编辑）
→ 确认 → 进现有大纲生成（OutlineGenerator 此时 skip Phase 1 LLM 分析）。
"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for
from .ctx import *

bp = Blueprint("world_builder", __name__)


def _profile_for(tl):
    """按笔名取风格档案（没有则 None，生成时降级为无风格约束）。"""
    if tl and tl.pen_name:
        try:
            return profiles.get_by_name(tl.pen_name)
        except Exception:
            return None
    return None


# ═══════════════════════════════════════════
# 世界观设定卡审查/编辑页
# ═══════════════════════════════════════════

@bp.route("/books/<book_id>/world")
def world_card(book_id):
    """世界观设定卡：展示/编辑已生成设定，支持重新生成、示例候选、从已有书借鉴。"""
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"error": "not found"}), 404
    bi = tl.basic_info or {}
    borrow_books = []
    for b in book_mgr.list_all():
        if b.book_id == book_id:
            continue
        src = _resolve_storyline(b.book_id)
        if src and (src.basic_info or {}):
            borrow_books.append({"book_id": b.book_id,
                                 "title": src.book_title or b.title,
                                 "genre": src.genre or b.genre})
    return render_template("world_card.html",
        book_id=book_id,
        storyline=tl,
        basic_info=bi,
        world_building=bi.get("world_building", {}) or {},
        protagonist=bi.get("protagonist", {}) or {},
        supporting_cast=bi.get("supporting_cast", []) or [],
        tone=bi.get("tone", ""),
        target_audience=bi.get("target_audience", ""),
        pov=bi.get("pov", ""),
        era_language=bi.get("era_language", ""),
        borrow_books=borrow_books,
        world_generated=bool(bi.get("_world_generated")),
    )


# ═══════════════════════════════════════════
# API：一句话/借鉴 → 生成世界观（SSE 流式）
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/generate", methods=["POST"])
def api_world_generate(book_id):
    """生成世界观。body: {mode: "one"|"borrow", idea?, source_book_id?, tweak?}"""
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500

    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "one")
    idea = (body.get("idea") or "").strip()
    source_book_id = (body.get("source_book_id") or "").strip()
    tweak = (body.get("tweak") or "").strip()

    seed = None
    if mode == "borrow" and source_book_id:
        src = _resolve_storyline(source_book_id)
        if src:
            from libraries.world_builder import WorldBuildingGenerator
            seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if mode == "borrow":
        idea = tweak  # 借鉴模式：微调句即主输入
    if not idea:
        idea = str((tl.basic_info or {}).get("world_building", {}).get("description", "") or "").strip()

    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)

    def generate():
        import json as _json
        try:
            for event_type, message, data_dict in gen.generate(
                genre=tl.genre, sub_genre=tl.sub_genre, idea=idea,
                pen_name=tl.pen_name, platform=tl.platform,
                seed_basic_info=seed, storyline=tl,
                on_save=lambda _tl: _save_storyline(_tl, book_id),
            ):
                if event_type == "thinking":
                    continue  # 思考流不在设定卡页逐字展示
                payload = {"event": event_type, "message": message}
                if data_dict:
                    payload.update(data_dict)
                if event_type in ("world_summary", "phase_done", "done", "error"):
                    payload["basic_info"] = tl.basic_info
                yield "data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            err_payload = {"event": "error", "message": str(e),
                           "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err_payload, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


# ═══════════════════════════════════════════
# API：示例候选（JSON，非流式）
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/candidates", methods=["POST"])
def api_world_candidates(book_id):
    """示例候选：一次产出 2-3 个差异化世界观方向供点选。"""
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    llm = get_llm()
    if not llm:
        return jsonify({"ok": False, "error": "LLM 未配置，请先在设置页配置 API"}), 500
    body = request.get_json(silent=True) or {}
    idea = (body.get("idea") or "").strip()

    profile = _profile_for(tl)
    from libraries.world_builder import WorldBuildingGenerator
    from libraries.prompt_harness import PromptHarness
    harness = PromptHarness(storyline=tl, profile=profile)
    gen = WorldBuildingGenerator(llm_client=llm, profile=profile, harness=harness)
    candidates = gen.generate_candidates(genre=tl.genre, sub_genre=tl.sub_genre, idea=idea)
    if not candidates:
        return jsonify({"ok": False, "error": "示例候选生成失败，请重试"}), 500
    return jsonify({"ok": True, "candidates": candidates})


# ═══════════════════════════════════════════
# API：从已有书借鉴预览
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/borrow-preview", methods=["POST"])
def api_world_borrow_preview(book_id):
    """预览将借鉴源书的哪些设定（extract_seed 结果）。"""
    body = request.get_json(silent=True) or {}
    source_book_id = (body.get("source_book_id") or "").strip()
    src = _resolve_storyline(source_book_id) if source_book_id else None
    if not src:
        return jsonify({"ok": False, "error": "源书不存在"}), 404
    from libraries.world_builder import WorldBuildingGenerator
    seed = WorldBuildingGenerator.extract_seed(src.basic_info)
    if not seed:
        return jsonify({"ok": False, "error": "源书没有可借鉴的设定"}), 404
    return jsonify({"ok": True, "seed": seed,
                    "source_title": src.book_title or src.pen_name or source_book_id,
                    "source_genre": src.genre})


# ═══════════════════════════════════════════
# API：确认世界观设定，进入大纲生成
# ═══════════════════════════════════════════

@bp.route("/api/world-builder/<book_id>/confirm", methods=["POST"])
def api_world_confirm(book_id):
    """打 _world_generated 标记并落盘，返回下一步跳转（故事线编辑器）。"""
    tl = _resolve_storyline(book_id)
    if not tl:
        return jsonify({"ok": False, "error": "not found"}), 404
    tl.basic_info = tl.basic_info or {}
    tl.basic_info["_world_generated"] = True
    tl.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_storyline(tl, book_id)
    return jsonify({"ok": True, "redirect": f"/storyline/{book_id}/edit"})
