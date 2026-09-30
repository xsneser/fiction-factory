"""事实账本只读模型。

事实来源按可信度合并：正式 Plot Delta（canonical）优先，其次是已经通过评审的
进行中 Plot（staged），旧书仅在没有 canonical Delta 时回退 ``story_memory.json``。
本模块只投影 Agent 已经上报的结构化历史事件，不从正文或事件措辞推断“当前仍然有效”的状态。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from core.json_store import read_json


ROOT = Path(__file__).resolve().parents[1]
RECENT_DEFAULT = 24
RECENT_MAX = 100
PAGE_DEFAULT = 50
PAGE_MAX = 100
MAX_TEXT_CHARS = 1200
MAX_COLLECTION_ITEMS = 24
MAX_VALUE_DEPTH = 4

_FACT_FIELDS = (
    ("choices_made", "历史选择", "event_result"),
    ("information_revealed", "历史揭示", "event_result"),
    ("relationship_changes", "历史关系变化", "relationship_changes"),
    ("resource_changes", "历史资源变化", "resource_changes"),
    ("promise_updates", "历史承诺变化", "promise_changes"),
    ("new_story_questions", "近期提出问题", "opened_questions"),
    ("character_events", "历史人物变化", "character_changes"),
)
_TERMINAL_BAD_STATES = {"rejected", "pending", "unreviewed", "draft", "superseded", "stale"}
_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_LOCK = threading.RLock()


def _book_dir(book_id: str, root: str | Path | None = None) -> Path:
    return Path(root or ROOT) / "books" / str(book_id)


def _file_mark(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (path.name, int(stat.st_mtime_ns), int(stat.st_size))


def _source_fingerprint(book_id: str, root: str | Path | None = None) -> tuple:
    """只扫描文件元数据；命中缓存时不重新读取或解析任一事实文件。"""
    book = _book_dir(book_id, root)
    facts_dir = book / "facts" / "plot"
    canonical = []
    if facts_dir.is_dir():
        for path in sorted(facts_dir.glob("*.json"), key=lambda p: p.name):
            mark = _file_mark(path)
            if mark:
                canonical.append(mark)
    sidecars = []
    for name in ("staged_story_state.json", "draft_chapter.json", "story_memory.json"):
        mark = _file_mark(book / name)
        if mark:
            sidecars.append((name, mark[1], mark[2]))
    return tuple(canonical), tuple(sidecars)


def _revision_of(fingerprint: tuple) -> str:
    raw = json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _read_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = read_json(path)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _flatten_versions(raw: Any, *, fallback_plot_id: str = "") -> list[dict]:
    """兼容单 Delta、版本数组及 chapter/versions 包装，不解释内容。"""
    if isinstance(raw, list):
        out = []
        for item in raw:
            out.extend(_flatten_versions(item, fallback_plot_id=fallback_plot_id))
        return out
    if not isinstance(raw, dict):
        return []
    for key in ("versions", "plot_deltas", "deltas"):
        if isinstance(raw.get(key), list):
            out = []
            for item in raw[key]:
                out.extend(_flatten_versions(item, fallback_plot_id=fallback_plot_id))
            return out
    item = copy.deepcopy(raw)
    if fallback_plot_id and not item.get("plot_id"):
        item["plot_id"] = fallback_plot_id
    return [item] if item.get("plot_id") else []


def _review_state(value: dict) -> str:
    review = value.get("review")
    nested = review.get("state") if isinstance(review, dict) else ""
    return str(value.get("review_state") or value.get("state") or nested or "").strip().lower()


def _bridge_is_accepted(bridge: dict) -> bool:
    # 严格 fail-closed：没有明确 accepted，不把在途事实暴露到 ledger。
    return _review_state(bridge) == "accepted"


def _accepted_staged(staged: dict, draft: dict) -> list[dict]:
    """读取 staged_story_state.json，严格只纳入 review_state=accepted 的 Plot。

    对应关系：
    1. delta 自身明确带有 review_state / review.state；
    2. 或在 draft.bridges / staged.bridges 中有对应 bridge，其状态严格为 accepted。
    若存在 run_id 则必须精确匹配；若无明确 accepted 或被 rejected/pending 则保守不纳入。
    """
    bridges: list[dict] = []
    if isinstance(staged.get("bridges"), list):
        bridges.extend(x for x in staged["bridges"] if isinstance(x, dict))
    if isinstance(draft.get("bridges"), list):
        bridges.extend(x for x in draft["bridges"] if isinstance(x, dict))

    out = []
    for order, delta in enumerate(staged.get("plot_deltas") or []):
        if not isinstance(delta, dict):
            continue
        plot_id = str(delta.get("plot_id") or "").strip()
        if not plot_id:
            continue
        run_id = str(delta.get("run_id") or "").strip()

        delta_state = _review_state(delta)
        if delta_state == "accepted":
            item = copy.deepcopy(delta)
            item["_source_order"] = order
            item["_provenance"] = "accepted_staged"
            out.append(item)
            continue
        if delta_state in _TERMINAL_BAD_STATES:
            # 明确为 rejected/pending/unreviewed 等，绝不纳入
            continue

        # delta 自身未标，核对 bridges 对应关系
        matching_bridges = [b for b in bridges if str(b.get("plot_id") or "").strip() == plot_id]
        if run_id:
            matching_bridges = [b for b in matching_bridges
                                if not str(b.get("run_id") or "").strip() or str(b.get("run_id") or "").strip() == run_id]

        if not matching_bridges:
            # 现有数据结构不足，保守不纳入，不假设 accepted
            continue

        # 必须所有匹配 bridge 均为 accepted，且不能有任何 rejected/pending
        if any(_review_state(b) in _TERMINAL_BAD_STATES for b in matching_bridges):
            continue
        if any(_bridge_is_accepted(b) for b in matching_bridges):
            item = copy.deepcopy(delta)
            item["_source_order"] = order
            item["_provenance"] = "accepted_staged"
            out.append(item)
    return out


def load_fact_sources(book_id: str, *, root: str | Path | None = None) -> dict:
    """读取并缓存一本书的原始事实源。

    缓存键包含 canonical 文件清单/mtime/size，以及 staged、draft、legacy 三个侧文件
    的 mtime/size；命中时不会重新读取全部 Plot 文件。
    """
    root_path = Path(root or ROOT).resolve()
    cache_key = (str(root_path), str(book_id))
    fingerprint = _source_fingerprint(book_id, root_path)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached.get("fingerprint") == fingerprint:
            return copy.deepcopy(cached["sources"])

    book = _book_dir(book_id, root_path)
    facts_dir = book / "facts" / "plot"
    canonical = []
    if facts_dir.is_dir():
        for file_order, path in enumerate(sorted(facts_dir.glob("*.json"), key=lambda p: p.name)):
            try:
                raw = read_json(path)
            except Exception:
                continue
            for version_order, item in enumerate(_flatten_versions(raw, fallback_plot_id=path.stem)):
                item["_source_order"] = (file_order, version_order, path.name)
                item["_provenance"] = "canonical"
                canonical.append(item)

    staged_raw = _read_dict(book / "staged_story_state.json")
    draft_raw = _read_dict(book / "draft_chapter.json")
    staged = _accepted_staged(staged_raw, draft_raw)

    legacy = []
    # canonical 是整本书的权威切换点；一旦存在 canonical，不再混入 story_memory 旧镜像
    if not canonical:
        memory = _read_dict(book / "story_memory.json")
        for chapter_order, chapter in enumerate(memory.get("chapters") or []):
            if not isinstance(chapter, dict):
                continue
            for delta_order, item in enumerate(_flatten_versions(chapter.get("plot_deltas") or [])):
                if not item.get("chapter_num"):
                    item["chapter_num"] = chapter.get("chapter_num") or 0
                item["_source_order"] = (chapter_order, delta_order)
                item["_provenance"] = "legacy_memory"
                legacy.append(item)

    sources = {
        "book_id": str(book_id),
        "revision": _revision_of(fingerprint),
        "canonical": canonical,
        "accepted_staged": staged,
        "legacy_memory": legacy,
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = {"fingerprint": fingerprint, "sources": copy.deepcopy(sources)}
    return sources


def _state_kind(item: dict) -> str:
    if item.get("current") is True:
        return "current"
    if item.get("accepted") is True:
        return "accepted"
    state = _review_state(item)
    if state == "current":
        return "current"
    if state == "accepted":
        return "accepted"
    if state in _TERMINAL_BAD_STATES:
        return "bad"
    return "implicit"


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _time_rank(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, float)):
        return (2, _number(value))
    text = str(value or "").strip()
    if not text:
        return (0, "")
    try:
        return (2, datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        return (1, text)


def _run_rank(value: Any) -> tuple:
    text = str(value or "")
    parts = re.split(r"(\d+)", text)
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


def _version_rank(item: dict) -> tuple:
    state = _state_kind(item)
    return (
        2 if state == "current" else 1 if state == "accepted" else 0,
        _number(item.get("draft_revision") or item.get("revision")),
        _time_rank(item.get("accepted_at") or item.get("updated_at") or item.get("created_at")),
        _run_rank(item.get("run_id")),
        repr(item.get("_source_order")),
    )


def _select_versions(items: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        plot_id = str(item.get("plot_id") or "").strip()
        if plot_id and _state_kind(item) != "bad":
            grouped.setdefault(plot_id, []).append(item)
    selected = {}
    for plot_id, versions in grouped.items():
        current = [x for x in versions if _state_kind(x) == "current"]
        accepted = [x for x in versions if _state_kind(x) == "accepted"]
        eligible = current or accepted or [x for x in versions if _state_kind(x) == "implicit"]
        if eligible:
            selected[plot_id] = max(eligible, key=_version_rank)
    return selected


def merge_fact_sources(sources: dict) -> list[dict]:
    """按 canonical > accepted staged > legacy 选择每个 plot 的唯一可信版本。

    同 plot_id canonical 存在时忽略 staged；同 plot 多版本只留 accepted/current。
    """
    canonical = _select_versions(list(sources.get("canonical") or []))
    staged = _select_versions(list(sources.get("accepted_staged") or []))
    legacy = _select_versions(list(sources.get("legacy_memory") or []))
    merged = dict(canonical)
    for plot_id, item in staged.items():
        merged.setdefault(plot_id, item)
    for plot_id, item in legacy.items():
        merged.setdefault(plot_id, item)
    return list(merged.values())


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _structured_identity(value: dict) -> tuple | None:
    for key in ("identity", "promise_id", "fact_id", "event_id", "question_id", "id"):
        if value.get(key) not in (None, ""):
            return (key, str(value[key]))
    fields = ("name", "subject", "type", "from", "to", "actual_to", "expected_to", "relation")
    parts = tuple((key, _normalized_text(str(value[key]))) for key in fields
                  if value.get(key) not in (None, ""))
    return ("structured",) + parts if len(parts) >= 2 else None


def _dedupe_key(plot_id: str, kind: str, value: Any) -> tuple | None:
    """去重键包含 plot_id：仅在同一个 Plot 内安全去重，保留不同 Plot 的相同文本。"""
    if isinstance(value, str):
        text = _normalized_text(value)
        return (plot_id, kind, "text", text) if text else None
    if isinstance(value, dict):
        identity = _structured_identity(value)
        if identity:
            return (plot_id, kind) + identity
        try:
            exact = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        return (plot_id, kind, "exact_object", exact)
    return None


def _fact_values(delta: dict, key: str, fallback: str) -> list:
    facts = delta.get("facts")
    if isinstance(facts, dict) and isinstance(facts.get(key), list):
        return facts[key]
    value = delta.get(fallback)
    return value if isinstance(value, list) else []


def _text_of(value: Any) -> str:
    """生成面向 UI/展示的清晰纯文本摘要。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if value.get("text"):
            return str(value["text"]).strip()
        if value.get("description") or value.get("desc"):
            return str(value.get("description") or value.get("desc")).strip()
        if value.get("question"):
            return str(value["question"]).strip()
        if value.get("summary"):
            return str(value["summary"]).strip()
        if value.get("name") and isinstance(value.get("events"), list):
            name = value["name"]
            ev_descs = []
            for ev in value["events"]:
                if isinstance(ev, dict):
                    t = ev.get("type") or "event"
                    target = ev.get("to") or ev.get("actual_to") or ""
                    reason = f" ({ev['reason']})" if ev.get("reason") else ""
                    if target:
                        ev_descs.append(f"{t} -> {target}{reason}")
                    elif ev.get("reason"):
                        ev_descs.append(f"{t}: {ev['reason']}")
                    else:
                        ev_descs.append(str(t))
                else:
                    ev_descs.append(str(ev))
            return f"{name}: " + "; ".join(ev_descs) if ev_descs else str(name)
        if value.get("name") and value.get("relation"):
            return f"{value['name']}: {value['relation']}"
        if value.get("subject") and value.get("type"):
            subj = value["subject"]
            t = value["type"]
            target = value.get("actual_to") or value.get("expected_to") or value.get("to") or ""
            target_str = f" -> {target}" if target else ""
            return f"{subj} {t}{target_str}"
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    if isinstance(value, list):
        return "; ".join(_text_of(x) for x in value if _text_of(x))
    return str(value)


def _bounded_value(value: Any, depth: int = 0) -> tuple[Any, bool]:
    if isinstance(value, str):
        text = value
        if len(text) > MAX_TEXT_CHARS:
            return text[:MAX_TEXT_CHARS], True
        return text, False
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if depth >= MAX_VALUE_DEPTH:
        return str(value)[:MAX_TEXT_CHARS], True
    if isinstance(value, list):
        out, cut = [], len(value) > MAX_COLLECTION_ITEMS
        for item in value[:MAX_COLLECTION_ITEMS]:
            bounded, child_cut = _bounded_value(item, depth + 1)
            out.append(bounded)
            cut = cut or child_cut
        return out, cut
    if isinstance(value, dict):
        out, cut = {}, len(value) > MAX_COLLECTION_ITEMS
        for key in sorted(value, key=lambda x: str(x))[:MAX_COLLECTION_ITEMS]:
            bounded, child_cut = _bounded_value(value[key], depth + 1)
            out[str(key)[:120]] = bounded
            cut = cut or child_cut or len(str(key)) > 120
        return out, cut
    text = str(value)
    return (text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS)


def _event_sort_key(event: dict) -> tuple:
    return (
        int(event.get("chapter_num") or 0),
        _number(event.get("draft_revision")),
        _time_rank(event.get("accepted_at")),
        _run_rank(event.get("run_id")),
        str(event.get("plot_id") or ""),
        int(event.get("event_index") or 0),
    )


def _events_from_deltas(deltas: list[dict]) -> list[dict]:
    events = []
    seen = set()
    for delta in deltas:
        plot_id = str(delta.get("plot_id") or "")
        event_index = 0
        provenance = str(delta.get("_provenance") or "")
        is_staged = provenance == "accepted_staged"
        for kind, label, fallback in _FACT_FIELDS:
            for value in _fact_values(delta, kind, fallback):
                key = _dedupe_key(plot_id, kind, value)
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                bounded, value_truncated = _bounded_value(copy.deepcopy(value))
                text_str = _text_of(value)
                if len(text_str) > MAX_TEXT_CHARS:
                    text_str = text_str[:MAX_TEXT_CHARS]
                    value_truncated = True

                status_str = "recent_question" if kind == "new_story_questions" else "historical"

                payload = {
                    "category": kind,
                    "kind": kind,
                    "label": label,
                    "text": text_str,
                    "plot_id": plot_id,
                    "plot_name": str(delta.get("plot_name") or "")[:240],
                    "chapter_num": int(delta.get("chapter_num") or 0),
                    "status": status_str,
                    "provenance": provenance,
                    "staged": is_staged,
                    "value": bounded,
                    "run_id": str(delta.get("run_id") or "")[:240],
                    "event_index": event_index,
                }
                if delta.get("draft_revision") is not None:
                    payload["draft_revision"] = delta.get("draft_revision")
                if delta.get("accepted_at") is not None:
                    payload["accepted_at"] = delta.get("accepted_at")
                if value_truncated:
                    payload["value_truncated"] = True
                digest_source = json.dumps(
                    [plot_id, kind, event_index, bounded], ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), default=str)
                payload["event_id"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:20]
                events.append(payload)
                event_index += 1
    events.sort(key=_event_sort_key, reverse=True)
    return events


def _provenance(events: list[dict], deltas: list[dict]) -> dict:
    names = ("canonical", "accepted_staged", "legacy_memory")
    return {
        "sources": [name for name in names if any(d.get("_provenance") == name for d in deltas)],
        "plot_counts": {name: sum(1 for d in deltas if d.get("_provenance") == name) for name in names},
        "event_counts": {name: sum(1 for e in events if e.get("provenance") == name) for name in names},
    }


def _ledger_data(book_id: str, root: str | Path | None = None) -> tuple[dict, list[dict], list[dict]]:
    sources = load_fact_sources(book_id, root=root)
    deltas = merge_fact_sources(sources)
    events = _events_from_deltas(deltas)
    return sources, deltas, events


def _encode_cursor(revision: str, offset: int) -> str:
    raw = json.dumps({"r": revision, "o": int(offset)}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, revision: str) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(data, dict) or data.get("r") != revision:
            raise ValueError("history cursor 已过期")
        offset = int(data.get("o"))
        if offset < 0:
            raise ValueError
        return offset
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("history cursor 无效") from exc


def project(book_id: str, recent_limit: int = RECENT_DEFAULT,
            *, root: str | Path | None = None) -> dict:
    """返回有界近期投影；``active`` 故意为空，不从历史事件猜当前状态。"""
    sources, deltas, events = _ledger_data(book_id, root)
    limit = max(0, min(int(recent_limit), RECENT_MAX))
    recent = events[:limit]
    archived = max(0, len(events) - len(recent))
    cursor = _encode_cursor(sources["revision"], len(recent)) if archived else None
    return {
        "revision": sources["revision"],
        "active": [],
        "recent": recent,
        "archived_count": archived,
        "history_cursor": cursor,
        "truncated": bool(archived),
        "provenance": _provenance(events, deltas),
    }


def page(book_id: str, cursor: str | None = None, limit: int = PAGE_DEFAULT,
         *, root: str | Path | None = None) -> dict:
    """按 opaque cursor 分页返回历史事件；旧 revision 的 cursor fail-closed。"""
    sources, deltas, events = _ledger_data(book_id, root)
    offset = _decode_cursor(str(cursor or ""), sources["revision"])
    size = max(1, min(int(limit), PAGE_MAX))
    items = events[offset:offset + size]
    next_offset = offset + len(items)
    has_more = next_offset < len(events)
    return {
        "revision": sources["revision"],
        "active": [],
        "recent": items,
        "archived_count": max(0, len(events) - next_offset),
        "history_cursor": _encode_cursor(sources["revision"], next_offset) if has_more else None,
        "truncated": has_more,
        "provenance": _provenance(events, deltas),
    }


def clear_cache(book_id: str | None = None, *, root: str | Path | None = None) -> None:
    """测试/维护用缓存失效；正常读取依赖文件指纹自动失效。"""
    root_key = str(Path(root or ROOT).resolve())
    with _CACHE_LOCK:
        if book_id is None:
            for key in [key for key in _CACHE if key[0] == root_key]:
                _CACHE.pop(key, None)
        else:
            _CACHE.pop((root_key, str(book_id)), None)


load_fact_ledger = project
project_fact_ledger = project
page_fact_history = page
merge = merge_fact_sources
