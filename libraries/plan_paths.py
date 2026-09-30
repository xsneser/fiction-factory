"""草稿字段路径的**寻址 / 枚举 / 锁定合并**（纯规则）。

路径约定与 `plan_diff` 的差异报告**同一套**：

    world.world_building.core_conflict
    characters[id=char_夏明鸢].goal
    world.world_building.factions[id=断脊流放营].stance
    storyline.outlines[id=arc_003].design_intent.goal
    storyline.plots[id=pl21].words

**集合内一律用稳定 entity id 寻址，不用数组下标**：agent 一重排，`characters[2]`
就可能已经换人；且 `plan_diff` 的留痕也按这套路径展示，用户读得懂。

`locked_fields` 是"用户明确要求保留"的字段（**不是**浏览器里还没保存的 dirty——
那个只在浏览器本地守卫，不进 canonical）。两件事分开，避免演化成第二套长期权限状态。

服务端维护可锁 allowlist：**只允许可编辑的语义叶字段**。结构身份字段
（id / parent_/start_word / order / written_chapter …）一律禁锁——否则会出现
"agent 需要重新规划情节段，但用户锁了它的 start_word"这种状态机死角。
"""
from __future__ import annotations

import re

_ID_SEG = re.compile(r"^(?P<name>[^\[\]]+)\[id=(?P<id>[^\]]*)\]$")

# 结构身份字段：决定"这是什么、排在哪"，不是内容。禁锁。
_STRUCTURAL_KEYS = {
    "id", "template_id", "revision", "content_revision", "storyline_revision",
    "parent_arc_id", "parent_plot_id", "children_plot_ids", "overlaps_with",
    "predecessor", "successor", "start_word", "end_word", "start_chapter", "end_chapter",
    "written_chapter", "confirmed", "order", "thread_seq", "stage_index", "outline_id",
    "protocol_version", "resolves_plot_id", "no_named_cast", "phase", "created", "book_id",
}


def _seg_name(seg: str) -> str:
    m = _ID_SEG.match(seg)
    return m.group("name") if m else seg


def split_path(path: str) -> list:
    """`a.b[id=x].c` → `["a", "b[id=x]", "c"]`（按 `.` 切，方括号内不切）。"""
    out, buf, depth = [], "", 0
    for ch in str(path or ""):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "." and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return [s for s in out if s]


def _select(items, want_id):
    for x in items or []:
        if isinstance(x, dict) and str(x.get("id") or x.get("name") or "") == want_id:
            return x
    return None


def _child(container, seg):
    """按一段路径取子节点（dict 键 或 集合内 entity）。"""
    m = _ID_SEG.match(seg)
    if m:
        rows = container.get(m.group("name")) if isinstance(container, dict) else None
        if not isinstance(rows, list):
            return None
        return _select(rows, m.group("id"))
    if isinstance(container, dict):
        return container.get(seg)
    return None


def get_path(draft, path: str):
    cur = draft
    for seg in split_path(path):
        cur = _child(cur, seg)
        if cur is None:
            return None
    return cur


def set_path(draft, path: str, value) -> bool:
    """就地写回（路径不存在则返回 False，不新建结构）。"""
    segs = split_path(path)
    if not segs:
        return False
    cur = draft
    for seg in segs[:-1]:
        cur = _child(cur, seg)
        if cur is None:
            return False
    last = segs[-1]
    m = _ID_SEG.match(last)
    if m:
        rows = cur.get(m.group("name")) if isinstance(cur, dict) else None
        if not isinstance(rows, list):
            return False
        target = _select(rows, m.group("id"))
        if target is None:
            return False
        # 末段是集合元素本身：整体替换
        rows[rows.index(target)] = value
        return True
    if isinstance(cur, dict) and last in cur:
        cur[last] = value
        return True
    return False


def walk_paths(node, base: str = "") -> list:
    """枚举所有**叶字段**路径（与 plan_diff 的寻址约定一致）。"""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            sub = f"{base}.{k}" if base else str(k)
            out.extend(walk_paths(v, sub))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                eid = item.get("id") or item.get("name")
                seg = f"{base}[id={eid}]" if eid else f"{base}[?]"
            else:
                seg = f"{base}[?]"
            out.extend(walk_paths(item, seg))
    else:
        out.append(base)
    return [p for p in out if p]


def is_structural(path: str) -> bool:
    segs = split_path(path)
    return bool(segs) and _seg_name(segs[-1]) in _STRUCTURAL_KEYS


def lockable_problems(paths, draft) -> list:
    """校验一组待锁路径；返回问题清单（空 = 全部可锁）。"""
    known = set(walk_paths(draft or {}))
    out = []
    for p in (paths or []):
        s = str(p or "").strip()
        if not s:
            continue
        if is_structural(s):
            out.append(f"{s}：结构身份字段不可锁定（它决定顺序与身份，锁住会让重新规划无从下手）")
        elif s not in known:
            out.append(f"{s}：不是当前草稿里的字段（拼写错误，或该字段还没内容）")
    return out


def apply_locked(old_draft, new_draft, locked_fields) -> tuple:
    """把被锁字段的值恢复成**用户版本**；返回 (新草稿, 冲突清单)。

    冲突 = agent 这次提交的值与用户保留值不同（页面上要如实告诉用户"你的 N 处修改
    未被 agent 改动"）。**不做静默覆盖**——此前整段替换会把用户的编辑全吞掉。
    """
    draft = new_draft if isinstance(new_draft, dict) else {}
    conflicts = []
    for path in (locked_fields or []):
        old_val = get_path(old_draft or {}, path)
        new_val = get_path(draft, path)
        if old_val is None and new_val is None:
            continue
        if old_val != new_val:
            conflicts.append({"path": path, "agent_value": _short(new_val),
                              "kept_value": _short(old_val)})
            if old_val is not None:
                set_path(draft, path, old_val)
    return draft, conflicts


def _short(v) -> str:
    text = " ".join(str(v if v is not None else "").split())
    return text[:80] + ("…" if len(text) > 80 else "")
