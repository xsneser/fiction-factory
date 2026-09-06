"""四大库 + 笔名档案 — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, Response, stream_with_context, abort
from .ctx import *
from libraries.profiles import KNOWN_PLATFORMS, PLATFORM_LABELS  # noqa: E402
from libraries import style_samples  # noqa: E402  # 笔名样文库 samples.json(收敛 <sample> 解析于此处)

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


def _project_root():
    """项目根目录（libraries.py 在 ui/web_blueprints/ 下，上溯三层）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_text_file(path):
    """读取文本文件；不存在/异常返回空串（样文/MD 等可选文件）。"""
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _parse_samples(content):
    """(兼容壳)把样文文本拆成多条样本；解析实现已收敛到 libraries/style_samples。"""
    return style_samples.parse_blocks(content)


def _samples_all():
    """全局样文库词条(StyleSample 列表);无文件 → []。"""
    return style_samples.load_samples() or []


def _samples_categories():
    """样文库顶部分类按钮 = 全部词条 scene_tags 并集(去重、按首现序)。"""
    seen, out = set(), []
    for s in _samples_all():
        for t in s.scene_tags or []:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _samples_upsert(records):
    """把记录(含 id)合并进全局样文库:有 id 覆盖、无 id 追加、空正文跳过;保存并再生镜像。

    返回 (final_list, warnings)。records 元素为 dict(缺省字段兼容)。"""
    items = [s.to_dict() for s in _samples_all()]
    for r0 in records or []:
        if not isinstance(r0, dict):
            continue
        txt = (r0.get("text") or "").strip()
        if not txt:
            continue
        rid = (r0.get("id") or "").strip()
        rec = {"id": rid,
               "title": (r0.get("title") or "").strip(),
               "scene_tags": [str(t).strip() for t in (r0.get("scene_tags") or []) if str(t).strip()],
               "source": (r0.get("source") or "").strip(),
               "note": (r0.get("note") or "").strip(),
               "text": txt,
               "no_warn": bool(r0.get("no_warn")),
               "dims": r0.get("dims") or {}}
        hit = False
        if rid:
            for i, it in enumerate(items):
                if it["id"] == rid:
                    items[i] = rec
                    hit = True
                    break
        if not hit:
            items.append(rec)
    style_samples.save_samples(samples=items)
    final = _samples_all()
    warnings = _warn_short(final) + [
        f"样文 {w['id']} 与 {w['dup_of']} 内容重复(子段)——浪费预算,建议去重"
        for w in style_samples.duplicate_warnings(final)]
    return final, warnings


def _warn_short(samples):
    """单条字数软预警：<800 字可能只是金句/片段(选样方法论:完整连续场景,1500~3000 为佳)。
    no_warn(人工确认保留,如天然短的开场/对白聚焦)不预警。"""
    out = []
    for s in samples:
        if getattr(s, "no_warn", False):
            continue
        if s.word_count and s.word_count < 800:
            label = s.title or s.id
            out.append(f"样文 {label} 约 {s.word_count} 字,<800 可能只是金句/片段——"
                       "建议改喂一段「完整连续场景」")
    return out

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
    """情节弧库页：**平级独立弧**；顶栏「全部 + 各题材标签」页签过滤（类情节段库）。"""
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
        _pj = (" · ".join(r.pattern for r in pr[:3]) + ("…" if len(pr) > 3 else "")) if pr else ""
        _bf = "、".join(r.pattern for r in bn[:3]) if bn else ""
        summaries[p.id] = {
            "prefers_joined": (_pj[:140] + "…" if len(_pj) > 140 else _pj),
            "ban_count": len(bn),
            "bans_first": (_bf[:60] + "…" if len(_bf) > 60 else _bf),
        }
    # 写作风格页只留 风格 MD（styles/<笔名>.md）;样文已拆到独立全局样文库页 /samples
    _root = _project_root()
    md_content = _read_text_file(os.path.join(_root, "styles", scope_label + ".md")) if (selected and not is_new) else ""
    # 样文库词条(全量,含 id/场景标签) + 场景分类 + 当前笔名已选 id —— 供「样文库」标签勾选/展示
    all_samples = [s.to_dict() for s in _samples_all()]
    return render_template("profiles.html",
        profiles=all_profiles, selected=selected, is_new=is_new,
        current_scope=current_scope, scope_label=scope_label,
        prefers=[r for r in own if r.kind == "prefer"],
        bans=[r for r in own if r.kind == "ban"],
        summaries=summaries,
        platform_labels=PLATFORM_LABELS,
        md_content=md_content,
        all_samples=all_samples,
        sample_tags=_samples_categories(),
        selected_sample_ids=list(selected.sample_ids or []) if selected else [])


@bp.route("/api/profile/<profile_id>/md", methods=["POST"])
def profile_md_save(profile_id):
    """保存笔名风格 MD 到 styles/<笔名>.md（直接覆盖；后续 sync 可能按手写源回写）。"""
    p = profiles.get(profile_id)
    if not p:
        return jsonify({"ok": False, "error": "笔名不存在"}), 404
    d = request.get_json(silent=True) or {}
    content = str(d.get("content") or "")
    path = os.path.join(_project_root(), "styles", p.pen_name + ".md")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return jsonify({"ok": True, "path": path})


def _samples_scenes():
    """样文库顶部分类按钮 = 库内词条 scene 维枚举(按 DIM_CHOICES 顺序、有内容才显示)。"""
    order = style_samples.DIM_CHOICES.get("scene", [])
    seen, out = set(), []
    for s in _samples_all():
        for v in (s.dims or {}).get("scene", []) or []:
            if v in order and v not in seen:
                seen.add(v)
                out.append(v)
    return out


@bp.route("/samples")
def samples_page():
    """样文库(全局词条库)独立页:顶部按 scene 维分类按钮 + 词条卡(维度标签) + 新增/编辑/删除。"""
    scene = (request.args.get("scene") or "").strip()
    samples = _samples_all()
    if scene:
        samples = [s for s in samples if scene in ((s.dims or {}).get("scene") or [])]
    return render_template("samples.html",
        samples=[s.to_dict() for s in samples],
        scenes=_samples_scenes(), current_scene=scene,
        total=len(_samples_all()),
        dim_choices=style_samples.DIM_CHOICES,
        dim_labels=style_samples.DIM_LABELS,
        dim_field_zh=style_samples.DIM_FIELD_ZH)


@bp.route("/api/samples")
def samples_list_api():
    """列全局样文库词条元数据(id/title/维度/字数/来源/备注,不含正文)。"""
    rows = [{"id": s.id, "title": s.title, "scene_tags": s.scene_tags,
             "dims": s.dims, "source": s.source, "note": s.note,
             "word_count": s.word_count} for s in _samples_all()]
    return jsonify({"ok": True, "count": len(rows), "samples": rows})


@bp.route("/api/samples/annotate", methods=["POST"])
def samples_annotate_api():
    """一键预标:给 {text, scene_tags?} 返回多维权表建议(不落库),供表单填充后人工微调再保存。"""
    d = request.get_json(silent=True) or {}
    text = str(d.get("text") or "")
    if not text:
        return jsonify({"ok": False, "error": "text 不能为空"}), 400
    from libraries.style_annotate import suggest_dims
    dims = suggest_dims(text, [str(t) for t in (d.get("scene_tags") or [])])
    return jsonify({"ok": True, "dims": dims})


@bp.route("/api/samples", methods=["POST"])
def samples_save_api():
    """新增/编辑样文库词条(全局):入参 {samples:[…]} 或 {sample:{…}};有 id 覆盖、无 id 追加。

    返回 {ok, saved, warnings, samples};warnings = 短块/重复子段软提示(no_warn 条除外),不阻断。
    """
    d = request.get_json(silent=True) or {}
    if "sample" in d and isinstance(d["sample"], dict):
        records = [d["sample"]]
    elif isinstance(d.get("samples"), list):
        records = d["samples"]
    else:
        return jsonify({"ok": False, "error": "入参须为 {samples:[...]} 或 {sample:{...}}"}), 400
    final, warnings = _samples_upsert(records)
    return jsonify({"ok": True, "saved": len(records), "warnings": warnings,
                    "samples": [s.to_dict() for s in final]})


@bp.route("/api/samples/<sample_id>/delete", methods=["POST"])
def samples_delete_api(sample_id):
    """删除样文库一个词条(按 id)。"""
    cur = _samples_all()
    if not any(s.id == sample_id for s in cur):
        return jsonify({"ok": False, "error": f"词条 {sample_id} 不存在"}), 404
    cur = [s for s in cur if s.id != sample_id]
    style_samples.save_samples(samples=cur)
    return jsonify({"ok": True, "deleted": sample_id, "total": len(cur)})


# ── 兼容(旧 /profiles 样文端点 → 全局词条库;写作风格页已不含样文)──

@bp.route("/api/profile/<profile_id>/samples", methods=["POST"])
def profile_samples_save(profile_id):
    """(兼容)样文已拆为全局词条库;此端点把入参整体 upsert 进全局(不再按笔名键)。"""
    d = request.get_json(silent=True) or {}
    records = d.get("samples")
    if not isinstance(records, list):
        return jsonify({"ok": False, "error": "samples 须为数组"}), 400
    final, warnings = _samples_upsert(records)
    return jsonify({"ok": True, "saved": len(records), "warnings": warnings,
                    "samples": [s.to_dict() for s in final]})


@bp.route("/api/profile/<profile_id>/reference", methods=["POST"])
def profile_reference_save(profile_id):
    """(兼容)旧整包 <sample> 文本 → 拆块追加进全局样文库。"""
    d = request.get_json(silent=True) or {}
    content = str(d.get("content") or "")
    blocks = style_samples.parse_blocks(content)
    records = [{"id": "", "title": "", "scene_tags": [], "source": "",
                "note": "", "text": b} for b in blocks]
    final, _w = _samples_upsert(records)
    return jsonify({"ok": True, "saved": len(records), "total": len(final),
                    "samples": [s.to_dict() for s in final]})


@bp.route("/api/profile/<profile_id>/samples/select", methods=["POST"])
def profile_samples_select(profile_id):
    """保存笔名在样文库的选择(词条 id 列表)——AI 写作时按此注入对应样文。

    入参 {sample_ids:[...]} 全量覆盖;只保留库中真实存在的 id,顺序按入参去重保留。
    """
    p = profiles.get(profile_id)
    if not p:
        return jsonify({"ok": False, "error": "笔名不存在"}), 404
    d = request.get_json(silent=True) or {}
    ids = d.get("sample_ids")
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "sample_ids 须为数组"}), 400
    valid = {s.id for s in _samples_all()}
    clean = []
    for i in ids:
        i = str(i).strip()
        if i and i in valid and i not in clean:
            clean.append(i)
    p.sample_ids = clean
    profiles.update(p)
    return jsonify({"ok": True, "profile_id": profile_id, "sample_ids": clean})


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


