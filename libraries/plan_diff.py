"""规划内容的**归一化 / 语义摘要 / 语义差异**（纯规则，零 LLM、零 IO）。

三个消费者：

1. `semantic_digest()` —— 建书 canonical 的 `content_revision` 靠它递增：只有
   world / characters / storyline 的**语义内容**真变了才 +1。流程元数据（phase、
   phase_ack、locked_fields、时间戳）一律不进摘要，否则"用户只点了个确认"就会让
   刚通过的校验失效。
2. `semantic_diff()` —— 下游失效（stale）判定与迭代留痕的展示，路径用 **entity id
   寻址**（`characters[id=char_夏明鸢].goal`）而不是数组下标：agent 一重排，
   `characters[2]` 就可能已经换人。
3. `normalize_semantic()` —— 两侧先归一再比，避免首尾空白 / `None` vs `""` /
   缺键制造假失效。

**顺序语义必须保住**：`outlines` / `plots` 的**排列本身就是叙事顺序**，
`pl21→pl22→pl23` 改成 `pl21→pl23→pl22` 是实质修改，即使三个对象一个字没变。
所以这两类**不排序**（digest 与 diff 都能看见顺序变化）；而 `characters` /
`factions` / `threads` / `promises` 是集合性的，按 id/name 排序对齐。
"""
from __future__ import annotations

import hashlib
import json

# 集合性集合：顺序无语义 → 排序后比较（重排不该制造假失效）
_SET_COLLECTIONS = {"characters", "factions", "threads", "promises", "future_intents"}
# 序列性集合：顺序即叙事顺序 → 保留原序
_SEQ_COLLECTIONS = {"outlines", "plots"}

_WORLD_TOP_KEYS = ("tone", "target_audience", "pov", "era_language")
_MAX_TEXT = 80


def _norm_value(v):
    """递归归一：字符串去首尾空白、`None`→`""`、dict 按键排序、list 递归。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, dict):
        return {str(k): _norm_value(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_norm_value(x) for x in v]
    return v


def _entity_key(item):
    if not isinstance(item, dict):
        return ("", str(item))
    return (str(item.get("id") or ""), str(item.get("name") or ""))


def _norm_collection(items, *, sequenced: bool):
    rows = [_norm_value(x) for x in (items or [])]
    return rows if sequenced else sorted(rows, key=_entity_key)


def _norm_world(world):
    """世界观的语义正文（容忍裸 world_building 本体；与 build_draft.normalize_world 同形）。"""
    if not isinstance(world, dict) or not world:
        return {}
    if isinstance(world.get("world_building"), dict):
        body = world
    else:
        body = {"world_building": {k: v for k, v in world.items() if k not in _WORLD_TOP_KEYS}}
        for k in _WORLD_TOP_KEYS:
            if world.get(k):
                body[k] = world[k]
    out = {"world_building": _norm_value(body.get("world_building") or {})}
    for k in _WORLD_TOP_KEYS:
        out[k] = _norm_value(body.get(k))
    wb = out["world_building"]
    if isinstance(wb.get("factions"), list):
        wb["factions"] = _norm_collection(wb["factions"], sequenced=False)
    return out


def _norm_storyline(storyline):
    sl = _norm_value(storyline) if isinstance(storyline, dict) else {}
    if not isinstance(sl, dict):
        return {}
    for key in ("outlines", "plots"):
        if key in sl:
            sl[key] = _norm_collection(sl[key], sequenced=True)
    for key in ("threads", "promises"):
        if key in sl:
            sl[key] = _norm_collection(sl[key], sequenced=False)
    return sl


def normalize_semantic(draft) -> dict:
    """整份草稿的语义视图（三段，去掉一切非内容字段）。"""
    d = draft if isinstance(draft, dict) else {}
    return {
        "world": _norm_world(d.get("world")),
        "characters": _norm_collection(d.get("characters"), sequenced=False),
        "storyline": _norm_storyline(d.get("storyline")),
    }


def semantic_digest(draft) -> str:
    """语义摘要：**只**覆盖归一化后的 world + characters + storyline。"""
    payload = json.dumps(normalize_semantic(draft), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 语义差异（路径用 entity id 寻址）─────────────────────────────────────────

def _short(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        text = json.dumps(v, ensure_ascii=False, sort_keys=True)
    else:
        text = str(v)
    text = " ".join(text.split())
    return text[:_MAX_TEXT] + ("…" if len(text) > _MAX_TEXT else "")


def _join(base: str, seg: str) -> str:
    return f"{base}.{seg}" if base else seg


def _list_path(base: str, item) -> str:
    """集合内元素以稳定 entity id 寻址（无 id 时退化到 name，再退化到位置）。"""
    if isinstance(item, dict):
        eid = item.get("id") or item.get("name")
        if eid:
            return f"{base}[id={eid}]"
    return f"{base}[?]"


def _diff_lists(base: str, old: list, new: list, out: dict, *, sequenced: bool) -> None:
    if sequenced:
        o_ids, n_ids = [_sel_key(x) for x in old], [_sel_key(x) for x in new]
        if o_ids != n_ids:
            # 顺序本身是语义：单独记一条（元素内容的差异仍逐项走下面的对齐）
            out["changed"].append({"path": _join(base, "order"),
                                   "before": " → ".join(str(x) for x in o_ids),
                                   "after": " → ".join(str(x) for x in n_ids)})
    old_by, new_by = _index(old), _index(new)
    for key in sorted(set(old_by) | set(new_by), key=str):
        present_old, present_new = key in old_by, key in new_by
        anchor = new_by.get(key) if present_new else old_by.get(key)
        path = f"{base}[?]" if key.startswith(_POS) else _list_path(base, anchor)
        if not present_new:
            out["removed"].append(path)
        elif not present_old:
            out["added"].append(path)
        else:
            _walk(path, old_by[key], new_by[key], out)


_POS = "\x00pos"


def _sel_key(item):
    if isinstance(item, dict):
        return str(item.get("id") or item.get("name") or "")
    return _short(item)


def _index(items):
    out, seen = {}, {}
    for i, item in enumerate(items or []):
        k = _sel_key(item)
        if not k or k in seen:
            k = f"{_POS}{i}"          # 无 id / 重复 id：退化为位置对齐
        seen[k] = True
        out[k] = item
    return out


def _leaves(node, base: str) -> list:
    """整段新增/删除时摊平到**叶字段**。

    只记容器路径（如 `world.world_building`）会让下游把这一笔当成"整个世界观都写了"：
    校验强度按路径推导，粒度一粗，立命题阶段就会被要求补齐 geography/factions。
    """
    if isinstance(node, dict):
        out = []
        for k, v in node.items():
            out.extend(_leaves(v, _join(base, str(k))))
        return out or [base]
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_leaves(item, _list_path(base, item)))
        return out or [base]
    return [base]


def _walk(path: str, old, new, out: dict) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            sub = _join(path, str(key))
            if key not in new:
                out["removed"].extend(_leaves(old[key], sub))
            elif key not in old:
                out["added"].extend(_leaves(new[key], sub))
            else:
                _walk(sub, old[key], new[key], out)
        return
    if isinstance(old, list) and isinstance(new, list):
        # 序列性集合（outlines/plots）保留顺序语义；其余按下标无关对齐
        _diff_lists(path, old, new, out, sequenced=(path.rsplit(".", 1)[-1] in _SEQ_COLLECTIONS))
        return
    if old != new:
        out["changed"].append({"path": path, "before": _short(old), "after": _short(new)})


def semantic_diff(old_draft, new_draft) -> dict:
    """归一化语义差异：`{added:[path], removed:[path], changed:[{path,before,after}]}`。

    路径形如 `world.world_building.core_conflict`、`characters[id=char_夏明鸢].goal`、
    `storyline.plots[id=pl21].words`、`storyline.plots.order`（顺序变更）。
    """
    out = {"added": [], "removed": [], "changed": []}
    old, new = normalize_semantic(old_draft), normalize_semantic(new_draft)
    _walk("", old, new, out)
    return out


def changed_paths(diff: dict) -> list:
    """差异涉及的一级 path 集合（含 changed 的路径），供 stale 依赖判定。"""
    paths = list(diff.get("added") or []) + list(diff.get("removed") or [])
    paths += [c.get("path", "") for c in (diff.get("changed") or [])]
    return [p for p in paths if p]


def counts_of(draft) -> dict:
    """迭代留痕里展示的规模计数。"""
    d = normalize_semantic(draft)
    sl, chars = d.get("storyline") or {}, d.get("characters") or []
    plots = sl.get("plots") or []
    promises = sl.get("promises") or []
    return {
        "outlines": len(sl.get("outlines") or []),
        "plots": len(plots),
        "characters": len(chars),
        "promises": len(promises),
        "planned_promises": sum(1 for p in promises
                                if isinstance(p, dict) and p.get("status") == "planned"),
    }
