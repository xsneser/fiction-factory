"""四大库 + 笔名档案 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context, abort
from .ctx import *
from libraries.profiles import KNOWN_PLATFORMS, PLATFORM_LABELS  # noqa: E402

bp = Blueprint("libraries", __name__)


def _parse_style_assets(form):
    """从表单解析 style_assets（写法资产特征池 + enabled 开关）。

    7 类特征：4 个列表（常用词/禁用词/句首/动作节拍，逗号分隔）+ 3 个标量
    （句长/对话比/段落风格）。无值且全启用 → 返回 {}（不写）。
    """
    from libraries.style_assets import STYLE_ASSET_FEATURES
    LIST_KEYS = ("common_words", "avoid_words", "sentence_starters", "action_beats")
    sa, enabled = {}, {}
    for k in STYLE_ASSET_FEATURES:
        enabled[k] = form.get(f"sa_{k}_enabled") == "on"
        v = (form.get(f"sa_{k}", "") or "").strip()
        if k in LIST_KEYS:
            items = [x.strip() for x in v.split(",") if x.strip()]
            if items:
                sa[k] = items
        elif v:
            sa[k] = v
    if sa or any(not e for e in enabled.values()):
        sa["enabled"] = enabled
    return sa


def _parse_platform_accounts(form):
    """从表单解析 platform_accounts：每个已知平台 {registered, site_id, author_url, notes, last_published_at}。

    未登记且其余字段全空 → 该平台条目不写入（保持档案干净）。
    """
    accounts = {}
    for pl in KNOWN_PLATFORMS:
        entry = {
            "registered": form.get(f"{pl}_registered") == "on",
            "site_id": form.get(f"{pl}_site_id", "").strip(),
            "author_url": form.get(f"{pl}_author_url", "").strip(),
            "notes": form.get(f"{pl}_notes", "").strip(),
            "last_published_at": form.get(f"{pl}_last_published_at", "").strip(),
        }
        if entry["registered"] or any(entry[k] for k in ("site_id", "author_url", "notes", "last_published_at")):
            accounts[pl] = entry
    return accounts

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
    "style_rules": (style_rules, "rules"),
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


@bp.route("/api/structures/<struct_id>/node/themes", methods=["POST"])
def struct_stage_themes(struct_id):
    """编辑某个弧/阶段节点的节点级内涵 [{name, position, how}]（含插入位置+表达手法）。
    path 为沿 stages→children 的索引列表（如 [0,2] = 顶层第0个子弧的第2个孙弧），支持多层嵌套。"""
    t = struct_lib.get_by_id(struct_id)
    if not t:
        return jsonify({"ok": False, "error": "not found"}), 404
    path = (request.json or {}).get("path")
    if not isinstance(path, list) or not path:
        return jsonify({"ok": False, "error": "path must be non-empty list"}), 400
    # 沿 stages→children 递归寻址目标节点
    nodes = t.stages
    node = None
    for p in path:
        if not isinstance(p, int) or not (0 <= p < len(nodes)):
            return jsonify({"ok": False, "error": "path out of range"}), 400
        node = nodes[p]
        nodes = node.children
    themes = (request.json or {}).get("themes")
    if not isinstance(themes, list):
        return jsonify({"ok": False, "error": "themes must be list"}), 400
    clean = []
    for m in themes:
        if not isinstance(m, dict) or not str(m.get("name", "") or "").strip():
            continue
        clean.append({"name": str(m["name"]).strip(),
                      "position": str(m.get("position", "") or "").strip(),
                      "how": str(m.get("how", "") or "").strip()})
    node.themes = clean
    struct_lib._save()
    return jsonify({"ok": True, "themes": clean})


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


# ═══════════════════════════════════════════
# 风格规则库（禁句式 + 去AI词表，可编辑）——竞品 promptWorkbench 简化
# ═══════════════════════════════════════════

@bp.route("/style-rules")
def style_rules_page():
    """风格规则库页：?profile=<id> 切换作用域（空=全局基线，非空=某笔名专属）。"""
    scope = (request.args.get("profile", "") or "").strip()
    all_profiles = profiles.list_all()
    own = style_rules.rules_for(scope)
    return render_template("style_rules.html",
        bans=[r for r in own if r.kind == "ban"],
        words=[r for r in own if r.kind == "word"],
        prefers=[r for r in own if r.kind == "prefer"],
        gbl_bans=[r for r in style_rules.rules_for("") if r.kind == "ban"],
        gbl_words=[r for r in style_rules.rules_for("") if r.kind == "word"],
        gbl_prefers=[r for r in style_rules.rules_for("") if r.kind == "prefer"],
        profiles=all_profiles,
        current_scope=scope,
        scope_label=("全局基线" if not scope else
                     next((p.pen_name for p in all_profiles if p.id == scope), scope)))


def _next_rule_id(kind: str) -> str:
    n = 1
    existing = {r.id for r in style_rules.rules}
    while f"{kind}_{n}" in existing:
        n += 1
    return f"{kind}_{n}"


@bp.route("/api/style-rules", methods=["POST"])
def style_rule_create():
    """新建禁则/词条/偏好：{kind, pattern, desc, severity, replacements, profile_id}。"""
    from libraries.style_rules import StyleRule
    d = request.get_json(silent=True) or {}
    kind = str(d.get("kind", "ban"))
    pattern = str(d.get("pattern", "")).strip()
    if kind not in ("ban", "word", "prefer") or not pattern:
        return jsonify({"ok": False, "error": "kind/pattern 必填"}), 400
    rule = StyleRule(id=_next_rule_id(kind), kind=kind,
                     profile_id=str(d.get("profile_id", "") or "").strip(),
                     pattern=pattern,
                     desc=str(d.get("desc", "") or "").strip(),
                     severity=str(d.get("severity", "warning")),
                     replacements=[str(x) for x in (d.get("replacements") or []) if str(x).strip()])
    style_rules.rules.append(rule)
    style_rules._save()
    return jsonify({"ok": True, "rule": rule.to_dict()})


@bp.route("/api/style-rules/<rule_id>", methods=["POST"])
def style_rule_update(rule_id):
    """更新条目：可改 pattern/desc/severity/replacements/enabled。"""
    d = request.get_json(silent=True) or {}
    rule = next((r for r in style_rules.rules if r.id == rule_id), None)
    if not rule:
        return jsonify({"ok": False, "error": "not found"}), 404
    if "pattern" in d:
        rule.pattern = str(d.get("pattern", "")).strip()
    if "desc" in d:
        rule.desc = str(d.get("desc", "")).strip()
    if "severity" in d:
        rule.severity = str(d.get("severity", rule.severity))
    if "replacements" in d:
        rule.replacements = [str(x) for x in (d.get("replacements") or []) if str(x).strip()]
    if "enabled" in d:
        rule.enabled = bool(d.get("enabled", rule.enabled))
    style_rules._save()
    return jsonify({"ok": True, "rule": rule.to_dict()})


@bp.route("/api/style-rules/<rule_id>/toggle", methods=["POST"])
def style_rule_toggle(rule_id):
    return _lib_toggle("style_rules", rule_id)


@bp.route("/api/style-rules/<rule_id>/delete", methods=["POST"])
def style_rule_delete(rule_id):
    return _lib_delete("style_rules", rule_id)


@bp.route("/structures")
def structures():
    return render_template("structures.html", templates=struct_lib.templates)


@bp.route("/gags")
def gags():
    return render_template("gags.html", patterns=gag_lib.patterns)


@bp.route("/profiles")
def profile_list():
    return render_template("profiles.html", profiles=profiles.list_all(),
                           platform_labels=PLATFORM_LABELS)


@bp.route("/profiles/new", methods=["GET","POST"])
def new_profile():
    if request.method == "POST":
        wp = {}
        if request.form.get("common_words"): wp["common_words"] = [w.strip() for w in request.form["common_words"].split(",")]
        if request.form.get("avoid_words"): wp["avoid_words"] = [w.strip() for w in request.form["avoid_words"].split(",")]
        new_p = profiles.create(
            pen_name=request.form["pen_name"],
            language=str(request.form.get("language") or "zh"),
            description=request.form.get("description",""),
            style_fingerprint={
                "humor_style": request.form.get("humor_style",""),
                "action_style": request.form.get("action_style",""),
                "sentence_length": request.form.get("sentence_length","medium"),
            },
            word_print=wp,
            platform_accounts=_parse_platform_accounts(request.form),
        )
        # 写法资产（特征池 + 开关）——create 无此参数，落盘后回写
        sa = _parse_style_assets(request.form)
        if sa:
            new_p.style_assets = sa
            profiles.update(new_p)
        return redirect(url_for("libraries.profile_list"))
    return render_template("new_profile.html", profile=None, platform_labels=PLATFORM_LABELS)


@bp.route("/profiles/<profile_id>/edit", methods=["GET","POST"])
def edit_profile(profile_id):
    """编辑笔名档案：风格字段 + 平台账号注册（仅 UI 人工登记）。"""
    p = profiles.get(profile_id)
    if not p:
        abort(404)
    if request.method == "POST":
        wp = {}
        if request.form.get("common_words"): wp["common_words"] = [w.strip() for w in request.form["common_words"].split(",")]
        if request.form.get("avoid_words"): wp["avoid_words"] = [w.strip() for w in request.form["avoid_words"].split(",")]
        p.description = request.form.get("description","")
        p.language = str(request.form.get("language") or "zh")
        p.style_fingerprint = {
            "humor_style": request.form.get("humor_style",""),
            "action_style": request.form.get("action_style",""),
            "sentence_length": request.form.get("sentence_length","medium"),
        }
        p.word_print = wp
        p.platform_accounts = _parse_platform_accounts(request.form)
        p.style_assets = _parse_style_assets(request.form)   # 写法资产特征池 + 开关
        profiles.update(p)
        return redirect(url_for("libraries.profile_list"))
    return render_template("edit_profile.html", profile=p, platform_labels=PLATFORM_LABELS)


