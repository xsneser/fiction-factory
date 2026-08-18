"""四大库 + 笔名档案 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *

bp = Blueprint("libraries", __name__)

@bp.route("/plots")
def plots():
    cat = request.args.get("category","")
    templates = plot_lib.search(category=cat) if cat else plot_lib.templates
    return render_template("plots.html",
        templates=templates, categories=plot_lib.categories(),
        current_cat=cat)


@bp.route("/api/plots/<plot_id>")
def plot_detail_api(plot_id):
    t = plot_lib.get_by_id(plot_id)
    if not t: return jsonify({"error":"not found"}), 404
    return jsonify(t.to_dict())


# ─── 库启用/禁用/删除（各库共用一套逻辑） ───

# kind → (库实例, 条目列表属性名)
_LIB_TABLE = {
    "plots": (plot_lib, "templates"),
    "structures": (struct_lib, "templates"),
    "gags": (gag_lib, "patterns"),
    "characters": (char_lib, "archetypes"),
}


def _lib_toggle(kind: str, item_id: str):
    """按 kind 切换某库条目的 enabled 状态"""
    lib, attr = _LIB_TABLE[kind]
    item = next((x for x in getattr(lib, attr) if x.id == item_id), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404
    item.enabled = not item.enabled
    lib._save()
    return jsonify({"ok": True, "enabled": item.enabled})


def _lib_delete(kind: str, item_id: str):
    """按 kind 删除某库条目"""
    lib, attr = _LIB_TABLE[kind]
    items = getattr(lib, attr)
    if not any(x.id == item_id for x in items):
        return jsonify({"ok": False, "error": "not found"}), 404
    setattr(lib, attr, [x for x in items if x.id != item_id])
    lib._save()
    return jsonify({"ok": True})


@bp.route("/api/plots/<plot_id>/toggle", methods=["POST"])
def plot_toggle(plot_id): return _lib_toggle("plots", plot_id)


@bp.route("/api/plots/<plot_id>/delete", methods=["POST"])
def plot_delete(plot_id): return _lib_delete("plots", plot_id)


@bp.route("/api/structures/<struct_id>/toggle", methods=["POST"])
def struct_toggle(struct_id): return _lib_toggle("structures", struct_id)


@bp.route("/api/structures/<struct_id>/delete", methods=["POST"])
def struct_delete(struct_id): return _lib_delete("structures", struct_id)


@bp.route("/api/gags/<gag_id>/toggle", methods=["POST"])
def gag_toggle(gag_id): return _lib_toggle("gags", gag_id)


@bp.route("/api/gags/<gag_id>/delete", methods=["POST"])
def gag_delete(gag_id): return _lib_delete("gags", gag_id)


@bp.route("/api/characters/<char_id>/toggle", methods=["POST"])
def character_toggle(char_id): return _lib_toggle("characters", char_id)


@bp.route("/api/characters/<char_id>/delete", methods=["POST"])
def character_delete(char_id): return _lib_delete("characters", char_id)


@bp.route("/characters")
def characters():
    cat = request.args.get("tag", "")
    archetypes = char_lib.search(tag=cat) if cat else char_lib.archetypes
    return render_template("characters.html",
        archetypes=archetypes, categories=char_lib.categories(),
        current_cat=cat)


@bp.route("/api/characters")
def characters_api():
    """启用中的角色原型列表（供设定表单「从原型库选」下拉）。"""
    return jsonify([a.to_dict() for a in char_lib.archetypes if a.enabled])


@bp.route("/structures")
def structures():
    return render_template("structures.html", templates=struct_lib.templates)


@bp.route("/gags")
def gags():
    return render_template("gags.html", patterns=gag_lib.patterns)


@bp.route("/profiles")
def profile_list():
    return render_template("profiles.html", profiles=profiles.list_all())


@bp.route("/profiles/new", methods=["GET","POST"])
def new_profile():
    if request.method == "POST":
        wp = {}
        if request.form.get("common_words"): wp["common_words"] = [w.strip() for w in request.form["common_words"].split(",")]
        if request.form.get("avoid_words"): wp["avoid_words"] = [w.strip() for w in request.form["avoid_words"].split(",")]
        profiles.create(
            pen_name=request.form["pen_name"],
            description=request.form["description"],
            style_fingerprint={
                "humor_style": request.form.get("humor_style",""),
                "action_style": request.form.get("action_style",""),
                "sentence_length": request.form.get("sentence_length","medium"),
            },
            word_print=wp,
        )
        return redirect(url_for("libraries.profile_list"))
    return render_template("new_profile.html")


