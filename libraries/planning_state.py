"""增量故事规划状态、建书草稿与边界检测。

storyline/章节仍是事实源；本模块只保存可重建的规划导航状态。
"""
from __future__ import annotations

import copy
import json
import os
import time
import uuid
from pathlib import Path

from core.json_store import read_json, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DEFAULT_HORIZON = {"h0_executable_plots": 3, "h1_near_arcs": 2, "h2_intents": 4}
_LIST_FIELDS = {
    "story_questions", "active_threads", "critical_promises", "character_intents",
    "future_intents", "decision_points",
}
_FIELDS = {
    "schema_version", "book_id", "storyline_revision", "mode",
    "target_word_budget", "written_until_word", "committed_until_word", "current",
    "horizon", "tension", "story_questions", "active_threads", "critical_promises",
    "character_intents", "future_intents", "decision_points", "last_replan",
}


def enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def planning_path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "planning_state.json"


def replan_preview_path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "replan_preview.json"


def build_session_path(session_id: str) -> Path:
    safe = "".join(c for c in str(session_id or "") if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("build_session_id 不能为空")
    return ROOT / "storage" / "build_sessions" / f"{safe}.json"


def _max_committed(tl) -> int:
    return max((int(getattr(o, "end_word", 0) or 0) for o in (getattr(tl, "outlines", None) or [])), default=0)


def _written(book) -> int:
    return int(getattr(book, "total_words", 0) or 0) if book else 0


def default_state(book_id: str, tl, book=None, target_word_budget: int = 0) -> dict:
    committed = _max_committed(tl)
    target = max(int(target_word_budget or 0), committed)
    return {
        "schema_version": SCHEMA_VERSION,
        "book_id": book_id,
        "storyline_revision": int(getattr(tl, "storyline_revision", 0) or 0),
        "mode": "legacy_full" if committed else "open",
        "target_word_budget": target,
        "written_until_word": _written(book),
        "committed_until_word": committed,
        "current": {"arc_id": "", "plot_id": ""},
        "horizon": dict(DEFAULT_HORIZON),
        "tension": {},
        "story_questions": [],
        "active_threads": [],
        "critical_promises": [],
        "character_intents": [],
        "future_intents": [],
        "decision_points": [],
        "last_replan": {},
    }


def validate_patch(patch: dict | None) -> dict:
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        raise ValueError("planning_patch 必须是 object")
    unknown = sorted(set(patch) - _FIELDS)
    if unknown:
        raise ValueError("planning_patch 含未知字段: " + ", ".join(unknown))
    out = copy.deepcopy(patch)
    for key in _LIST_FIELDS:
        if key in out and not isinstance(out[key], list):
            raise ValueError(f"planning_patch.{key} 必须是 list")
    for key in ("current", "horizon", "tension", "last_replan"):
        if key in out and not isinstance(out[key], dict):
            raise ValueError(f"planning_patch.{key} 必须是 object")
    for key in ("target_word_budget", "written_until_word", "committed_until_word", "storyline_revision"):
        if key in out:
            if isinstance(out[key], bool) or not isinstance(out[key], int) or out[key] < 0:
                raise ValueError(f"planning_patch.{key} 必须是非负整数")
    return out


def merge_state(base: dict, patch: dict | None) -> dict:
    result = copy.deepcopy(base)
    for key, value in validate_patch(patch).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    result["schema_version"] = SCHEMA_VERSION
    return result


# ─── 语义合并：story_questions / character_intents（R4 修订）───
# 这些是「跨章累积、不无脑 append」的字段。replan 全量替换仍走 merge_state；
# 章末增量上报走本组 helper（稳定 id / 状态机 / 去重 / 按人物 upsert）。

_QUESTION_STATUS = {"open", "progressed", "answered", "superseded"}


def upsert_story_questions(existing: list | None, updates: list | None) -> list:
    """合并 story_questions：稳定 id **或同文**双路去重 → update；否则新开一条（默认 open）。

    状态机：已 answered/superseded 的问题默认保持终态，除非显式带 status 才被重开/推进。
    """
    res = [dict(x) for x in (existing or []) if isinstance(x, dict)]
    id2i = {str(x.get("id") or "").strip(): i for i, x in enumerate(res)
            if str(x.get("id") or "").strip()}
    txt2i = {}
    for i, x in enumerate(res):
        t = str(x.get("question") or "").strip()
        if t:
            txt2i.setdefault(t, i)
    for u in (updates or []):
        if not isinstance(u, dict):
            continue
        qtext = str(u.get("question") or "").strip()
        u_id = str(u.get("id") or "").strip()
        if not qtext and not u_id:
            continue
        idx = id2i.get(u_id) if u_id else None
        if idx is None and qtext:
            idx = txt2i.get(qtext)
        if idx is None:
            nid = u_id or uuid.uuid4().hex[:8]
            if nid in id2i:
                nid = uuid.uuid4().hex[:8]
            entry = {k: copy.deepcopy(v) for k, v in u.items() if v is not None}
            entry["id"] = nid
            entry["question"] = qtext
            entry.setdefault("status", "open")
            entry["status"] = entry["status"] if entry["status"] in _QUESTION_STATUS else "open"
            res.append(entry)
            id2i[nid] = len(res) - 1
            if qtext:
                txt2i.setdefault(qtext, len(res) - 1)
        else:
            cur = res[idx]
            merged = {k: copy.deepcopy(v) for k, v in u.items() if v is not None and k != "id"}
            if "status" in merged:
                merged["status"] = (merged["status"] if merged["status"] in _QUESTION_STATUS
                                    else cur.get("status", "open"))
            else:
                # 不带显式 status 的增量：不得重开已终态问题
                cur_status = str(cur.get("status") or "open")
                if cur_status not in ("answered", "superseded"):
                    merged["status"] = "progressed"
            for k, v in merged.items():
                if v is not None:
                    cur[k] = v
            if not str(cur.get("id") or "").strip():  # 同文匹配到无 id 旧条 → 补稳定 id
                cur["id"] = uuid.uuid4().hex[:8]
                id2i[cur["id"]] = idx
            res[idx] = cur
    return res


def upsert_character_intents(existing: list | None, updates: list | None) -> list:
    """按人物 upsert character_intents；同人多次上报只更新该人条目，不按章无限追加。"""
    res = [dict(x) for x in (existing or []) if isinstance(x, dict)]
    idx_by_name = {str(x.get("name") or "").strip(): i for i, x in enumerate(res)}
    for u in (updates or []):
        if not isinstance(u, dict):
            continue
        name = str(u.get("name") or "").strip()
        if not name:
            continue
        idx = idx_by_name.get(name)
        if idx is None:
            entry = {k: copy.deepcopy(v) for k, v in u.items() if v is not None}
            entry["name"] = name
            res.append(entry)
            idx_by_name[name] = len(res) - 1
        else:
            cur = res[idx]
            for k, v in u.items():
                if v is not None and k != "name":
                    cur[k] = copy.deepcopy(v)
            res[idx] = cur
    return res


def load_planning_state(book_id: str, tl, book=None, persist: bool = True) -> dict:
    path = planning_path(book_id)
    data = read_json(path) if path.exists() else None
    if not isinstance(data, dict):
        data = default_state(book_id, tl, book)
        if persist:
            write_json_atomic(path, data)
    else:
        original = copy.deepcopy(data)
        data = merge_state(default_state(book_id, tl, book), data)
        data["book_id"] = book_id
        data["storyline_revision"] = int(getattr(tl, "storyline_revision", 0) or 0)
        # 只有拿到真实 book 时才回写已写字数：`book=None` 的只读调用（如 FSM 评估）
        # 否则会把 written_until_word 覆写成 0 并落盘，与 UI/写路径互相打架。
        if book is not None:
            data["written_until_word"] = _written(book)
        # 承诺水位只增不减：换/缩短故事线（mode=replace）不会把已承诺区缩回去，
        # 因此边界判定对「被替换掉的那段承诺」仍按旧水位理解——这是刻意选择（宁可多补一批，
        # 也不要让已向读者承诺的内容失去边界保护）。
        data["committed_until_word"] = max(int(data.get("committed_until_word") or 0), _max_committed(tl))
        if persist and data != original:
            write_json_atomic(path, data)
    return data


def save_planning_state(book_id: str, state: dict) -> None:
    checked = merge_state({"book_id": book_id}, state)
    checked["book_id"] = book_id
    write_json_atomic(planning_path(book_id), checked)


def load_replan_preview(book_id: str) -> dict | None:
    path = replan_preview_path(book_id)
    data = read_json(path) if path.exists() else None
    return data if isinstance(data, dict) else None


def save_replan_preview(book_id: str, preview: dict) -> dict:
    """保存非权威续规划预览；每本书只保留最新一份。

    预览是**单份覆盖**语义：并发计划器后写会盖掉先写，所以写入纳入书锁（与 commit-plan
    同一把锁），并记下产出方的运行身份（flow/child run），便于审计与「这是谁写的预览」。
    """
    from libraries.book_lock import BookBusyError, BookLock
    data = copy.deepcopy(preview or {})
    data["book_id"] = book_id
    data["preview_id"] = str(data.get("preview_id") or uuid.uuid4().hex)
    data["created_at"] = str(data.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
    data.setdefault("owner_flow_id", os.environ.get("NOVEL_WRITE_FLOW_ID", ""))
    data.setdefault("owner_child_run_id", os.environ.get("NOVEL_WRITE_CHILD_RUN_ID", ""))
    lock = BookLock(book_id)
    if not lock.acquire(timeout=30.0, purpose="save_replan_preview"):
        raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
    try:
        write_json_atomic(replan_preview_path(book_id), data)
        # 迭代留痕：**同一把书锁内**追加一行（含完整 plots/outlines 快照）。
        # 预览本身仍是单份覆盖——它是提交目标，按 preview_id 校验，出现第二份就产生
        # "提交哪一份"的歧义；历史只作审计与对照，不参与提交。
        _append_replan_history(book_id, data)
    finally:
        lock.release()
    return data


def replan_history_path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "replan_history.jsonl"


def _append_replan_history(book_id: str, data: dict) -> None:
    """记一行续规划历史（append-only，失败不阻断预览落盘）。"""
    try:
        row = {
            "preview_id": data.get("preview_id"),
            "expected_revision": data.get("expected_revision"),
            "at": data.get("created_at"),
            "direction_ids": [d.get("id") for d in (data.get("directions") or [])
                              if isinstance(d, dict)],
            "selected_direction_id": data.get("selected_direction_id"),
            "plots_count": len(data.get("plots") or []),
            "outlines_count": len(data.get("outlines") or []),
            "planned_promises": sum(1 for p in (data.get("plots") or [])
                                    if isinstance(p, dict)
                                    for f in (p.get("foreshadow") or [])
                                    if isinstance(f, dict)),
            "validation_passed": bool((data.get("validation") or {}).get("passed")),
            "snapshot": {"outlines": data.get("outlines") or [],
                         "plots": data.get("plots") or [],
                         "planning_patch": data.get("planning_patch") or {}},
        }
        with open(replan_history_path(book_id), "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_replan_history(book_id: str, limit: int = 20) -> list:
    """续规划历史（最近 limit 条，倒序由调用方决定）。"""
    path = replan_history_path(book_id)
    if not path.exists():
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out[-limit:]


def delete_replan_preview(book_id: str, preview_id: str = "") -> bool:
    from libraries.book_lock import BookBusyError, BookLock
    path = replan_preview_path(book_id)
    data = load_replan_preview(book_id)
    if not data:
        return False
    if preview_id and str(data.get("preview_id") or "") != str(preview_id):
        return False
    lock = BookLock(book_id)
    if not lock.acquire(timeout=30.0, purpose="delete_replan_preview"):
        raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
    try:
        path.unlink(missing_ok=True)
    finally:
        lock.release()
    return True


def save_build_session(session_id: str, draft: dict) -> dict:
    path = build_session_path(session_id)
    previous = read_json(path) if path.exists() else {}
    previous_draft = previous.get("planning_draft") if isinstance(previous, dict) else {}
    merged = merge_state(previous_draft if isinstance(previous_draft, dict) else {}, draft or {})
    rec = {"schema_version": SCHEMA_VERSION, "build_session_id": session_id,
           "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "planning_draft": merged}
    write_json_atomic(path, rec)
    return rec


def attach_build_session(session_id: str, book_id: str, tl, book=None) -> dict:
    path = build_session_path(session_id)
    rec = read_json(path) if path.exists() else {}
    draft = rec.get("planning_draft") if isinstance(rec, dict) else {}
    state = merge_state(default_state(book_id, tl, book), draft if isinstance(draft, dict) else {})
    state["book_id"] = book_id
    state["storyline_revision"] = int(getattr(tl, "storyline_revision", 0) or 0)
    state["committed_until_word"] = max(int(state.get("committed_until_word") or 0), _max_committed(tl))
    save_planning_state(book_id, state)
    if path.exists():
        path.unlink()
    return state


# ── H0 承诺批次策略（按**承诺字数**控制 horizon，阈值与目标必须同源）──────────
# 早期实现是「触发阈值 ≤2 段」+「每次只补 3–8 段」，等于每 1–7 段就要跑一整轮计划器会话
# （一次会话 ≈ 10 轮 LLM + 12KB NOVEL_AGENT.md + skills + 10 个工具 schema），
# 规划开销与写作产出严重失衡。
#
# 2026-09-11 改基线：Plot 粒度从「一大段故事发展」(平均 ~1300 字) 收到「一个主要戏剧变化」
# (~600 字)，**批次不能再按段数定义**——同样的 6–12 段，承诺字数直接腰斩，replan 会翻倍触发；
# 而把段数翻到 10–20 只是补偿，下次 Plot 平均字数再变又要改一次。改成按承诺字数定 horizon：
# 计划器生成 Plot 直到累计 committed 字数达到 REPLAN_TARGET_WORDS，段数只作安全上限。
#
# 注意：触发阈值与批次目标仍然必须同源（就是本节开头那个论证）——只是单位从段数换成字数。
REPLAN_TARGET_WORDS = 9000        # 计划器单批承诺目标（≈3 章 @3000）
REPLAN_MIN_REMAINING_WORDS = 2500  # 承诺余量低于此即请求续规划
REPLAN_MAX_PLOTS = 20             # 安全上限（不是目标）
REPLAN_MIN_PLOTS = 2              # PLOTS_LOW 安全下限（= 原 max(2, 6//3)，行为不变）


def replan_low_plot_threshold() -> int:
    """剩余情节段 ≤ 此值即请求续规划（段数只是兜底安全网，horizon 主体由字数定）。"""
    return REPLAN_MIN_PLOTS


def detect_story_boundary(*, written_until_word: int, committed_until_word: int,
                          remaining_plots: int, words_per_batch: int = 0,
                          storyline_revision: int, last_replan: dict | None = None,
                          low_plot_threshold: int | None = None,
                          replan_min_remaining_words: int | None = None) -> dict:
    """纯函数：剩余 plot 低于阈值 或 承诺余量不足一个批次时请求 replan。

    注意两个信号的角色：它们是**续规划信号**（UI 横幅 + 计划器输入），不是「停写」信号。
    写作 FSM 的动作优先级是「章满收章 → 还有可写 Plot 就继续写 → 都没有才续规划」，
    所以情节段没耗尽时不会为了边界中断写作（`write_flow.next_action`）。
    """
    threshold = replan_low_plot_threshold() if low_plot_threshold is None else int(low_plot_threshold)
    # 字数阈值优先用显式入参；未给时回退旧口径（words_per_batch=words_per_chapter）。
    min_remaining = (int(replan_min_remaining_words) if replan_min_remaining_words is not None
                     else (int(words_per_batch) if words_per_batch
                           else REPLAN_MIN_REMAINING_WORDS))
    remaining_words = max(0, int(committed_until_word or 0) - int(written_until_word or 0))
    reasons = []
    if int(remaining_plots or 0) <= threshold:
        reasons.append("PLOTS_LOW")
    if remaining_words <= max(1, min_remaining):
        reasons.append("WORDS_LOW")
    last = last_replan or {}
    same_revision = int(last.get("from_revision", -1) or -1) == int(storyline_revision or 0)
    last_reasons = set(last.get("reason_codes") or [])
    debounced = bool(reasons) and same_revision and set(reasons).issubset(last_reasons)
    return {"needs_replan": bool(reasons) and not debounced, "reason_codes": reasons,
            "remaining_plots": max(0, int(remaining_plots or 0)), "remaining_words": remaining_words,
            "low_plot_threshold": threshold,
            "replan_min_remaining_words": min_remaining,
            "replan_target_words": REPLAN_TARGET_WORDS,
            "replan_max_plots": REPLAN_MAX_PLOTS,
            "debounced": debounced, "storyline_revision": int(storyline_revision or 0)}
