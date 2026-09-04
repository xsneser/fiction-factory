"""四大库 + 笔名档案 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context, abort
from .ctx import *
from libraries.profiles import KNOWN_PLATFORMS, PLATFORM_LABELS  # noqa: E402

bp = Blueprint("libraries", __name__)


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
    # 分类页签只保留仍有内容的（其余几库同规则，统一角色库式样）
    cats = [c for c in plot_lib.categories()
            if any(t.category == c for t in plot_lib.templates)]
    return render_template("plots.html",
        templates=templates, categories=cats,
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
def struct_delete(struct_id):
    """删除一根情节弧：删除该根弧及其全部后代节点（防扁平库留下孤儿子行）。"""
    lib = struct_lib
    if not any(x.id == struct_id for x in lib.templates):
        return jsonify({"ok": False, "error": "not found"}), 404
    removed = lib.delete_tree(struct_id)
    return jsonify({"ok": True, "removed": removed})


@bp.route("/api/structures/<node_id>/node/themes", methods=["POST"])
def struct_stage_themes(node_id):
    """编辑**任意**弧节点（根或任意深度子弧，统一按 id 寻址）的节点级内涵
    [{name, position, how}]（含插入位置+表达手法）。扁平库每节点一行独立可编辑。"""
    node = struct_lib.get_by_id(node_id)
    if not node:
        return jsonify({"ok": False, "error": "not found"}), 404
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
    # 分类页签只保留仍有内容的
    counts: dict = {}
    for a in char_lib.archetypes:
        for tg in (a.tags or []):
            counts[tg] = counts.get(tg, 0) + 1
    cats = [c for c in char_lib.categories() if counts.get(c, 0) > 0]
    return render_template("characters.html",
        archetypes=archetypes, categories=cats,
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
    """风格规则已并入笔名档案页：302 重定向到 /profiles（兼容 ?profile= → ?scope=）。"""
    scope = (request.args.get("profile") or request.args.get("scope") or "").strip()
    return redirect(url_for("libraries.profile_list", scope=scope))


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
    if kind not in ("ban", "prefer") or not pattern:
        return jsonify({"ok": False, "error": "kind/pattern 必填（ban=禁止内容，prefer=句式风格）"}), 400
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
    """情节弧库页：**平级独立弧**；顶栏「全部 + 各题材标签」页签过滤（类桥段库）。"""
    tag = (request.args.get("tag") or "").strip()
    lib = struct_lib
    all_arcs = list(lib.templates)
    cats = sorted({x for t in all_arcs for x in (t.tags or [])})
    arcs = all_arcs if not tag else [t for t in all_arcs if tag in (t.tags or [])]
    return render_template("structures.html",
                           arc_cards=[n.to_dict() for n in arcs],
                           categories=cats, current_tag=tag, total=len(all_arcs))


@bp.route("/gags")
def gags():
    cat = (request.args.get("category") or "").strip()
    patterns = [p for p in gag_lib.patterns
                if (p.category or "").strip() == cat] if cat else list(gag_lib.patterns)
    cats = sorted({(p.category or "").strip() for p in gag_lib.patterns if (p.category or "").strip()})
    return render_template("gags.html",
        patterns=patterns, categories=cats,
        current_cat=cat)


@bp.route("/profiles")
def profile_list():
    """笔名档案 + 风格规则库合并页（master-detail）：?scope= 切换编辑面。
    'new'=空档案表单；<id>=该笔名档案+专属规则；空/缺失=默认选中首个笔名（不再有全局基线）。
    """
    # Jinja groupby 不排序：handler 里先排序（zh 在前，组内按笔名），保证中英文分组有序
    all_profiles = sorted(profiles.list_all(), key=lambda p: (p.language == "en", p.pen_name))
    raw = (request.args.get("scope") or request.args.get("profile") or "").strip()
    is_new = raw == "new"
    selected = None
    if is_new:
        current_scope = "new"
    elif raw and profiles.get(raw):
        selected = profiles.get(raw)
        current_scope = raw
    else:
        # 无 scope / 笔名不存在 → 默认选中首个笔名（枫落），不再有全局基线
        selected = all_profiles[0] if all_profiles else None
        current_scope = selected.id if selected else "new"

    own = [] if is_new else (style_rules.rules_for(current_scope) if selected else [])
    scope_label = ("新建笔名" if is_new else
                   (selected.pen_name if selected else "新建笔名"))
    # 抽屉内每个笔名下方展示其专属风格摘要（仅 enabled，精简单行）
    summaries = {}
    for p in all_profiles:
        own_p = style_rules.rules_for(p.id)
        pr = [r for r in own_p if r.kind == "prefer" and r.enabled]
        bn = [r for r in own_p if r.kind == "ban" and r.enabled]
        summaries[p.id] = {
            "prefers_joined": (" · ".join(r.pattern for r in pr[:3]) + ("…" if len(pr) > 3 else "")) if pr else "",
            "ban_count": len(bn),
            "bans_first": "、".join(r.pattern for r in bn[:3]) if bn else "",
        }
    return render_template("profiles.html",
        profiles=all_profiles, selected=selected, is_new=is_new,
        current_scope=current_scope, scope_label=scope_label,
        prefers=[r for r in own if r.kind == "prefer"],
        bans=[r for r in own if r.kind == "ban"],
        summaries=summaries,
        platform_labels=PLATFORM_LABELS)


@bp.route("/profiles/<profile_id>/delete", methods=["POST"])
def delete_profile(profile_id):
    """删除笔名档案 + 清理其专属风格规则（孤儿）。防空 rules 落盘覆盖内置种子。"""
    profiles.delete(profile_id)
    orphaned = [r for r in style_rules.rules if (r.profile_id or "").strip() == profile_id]
    if orphaned:
        style_rules.rules = [r for r in style_rules.rules
                             if (r.profile_id or "").strip() != profile_id]
        style_rules._save()
    return redirect(url_for("libraries.profile_list"))


@bp.route("/profiles/new", methods=["GET","POST"])
def new_profile():
    if request.method == "POST":
        new_p = profiles.create(
            pen_name=request.form["pen_name"],
            language=str(request.form.get("language") or "zh"),
            description=request.form.get("description",""),
            style_fingerprint={
                "humor_style": request.form.get("humor_style",""),
                "action_style": request.form.get("action_style",""),
            },
            platform_accounts=_parse_platform_accounts(request.form),
        )
        return redirect(url_for("libraries.profile_list", scope=new_p.id))  # 新建后自动选中
    # GET：独立页已并入 /profiles，302 到合并页新建模式（旧书签/外链不 404）
    return redirect(url_for("libraries.profile_list", scope="new"))


@bp.route("/profiles/<profile_id>/edit", methods=["GET","POST"])
def edit_profile(profile_id):
    """编辑笔名档案：风格字段 + 平台账号注册（仅 UI 人工登记）。"""
    p = profiles.get(profile_id)
    if not p:
        abort(404)
    if request.method == "POST":
        p.description = request.form.get("description","")
        p.language = str(request.form.get("language") or "zh")
        p.style_fingerprint = {
            "humor_style": request.form.get("humor_style",""),
            "action_style": request.form.get("action_style",""),
        }
        p.platform_accounts = _parse_platform_accounts(request.form)
        profiles.update(p)
        return redirect(url_for("libraries.profile_list", scope=profile_id))  # 保存后保持选中
    # GET：独立页已并入 /profiles，302 到合并页该笔名（旧书签/外链不 404）
    return redirect(url_for("libraries.profile_list", scope=profile_id))


