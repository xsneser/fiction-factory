"""Plot-Run 的暂存事实账本与确定性记忆投影。

正文不是事实源。这里保存的 delta 全部来自写作 Agent 的结构化 outcome 和
character_events；章节提交后才由调用方把它升级为正式 chapter_delta。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

from core.json_store import read_json, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
FACT_KEYS = ("choices_made", "information_revealed", "relationship_changes",
             "resource_changes", "promise_updates", "new_story_questions")


def staged_path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "staged_story_state.json"


def history_path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "story_memory.json"


class PlotMemoryStore:
    """事实与派生缓存的存储边界。

    上层只依赖这个接口，不依赖当前一 Plot 一文件的实现；之后可以无损迁到
    JSONL/SQLite。Plot Delta 是 canonical，arc/index 均可从它重建。
    """
    def __init__(self, book_id: str):
        self.book_id = book_id
        self.root = ROOT / "books" / book_id

    @property
    def facts_dir(self) -> Path:
        return self.root / "facts" / "plot"

    @property
    def index_path(self) -> Path:
        return self.root / "cache" / "index.json"

    def append(self, delta: dict) -> None:
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.facts_dir / f"{delta.get('plot_id')}.json", copy.deepcopy(delta))

    def get(self, plot_id: str) -> dict | None:
        path = self.facts_dir / f"{plot_id}.json"
        raw = read_json(path) if path.exists() else None
        return raw if isinstance(raw, dict) else None

    def all(self) -> list[dict]:
        if not self.facts_dir.exists():
            return []
        out = []
        for path in self.facts_dir.glob("*.json"):
            raw = read_json(path)
            if isinstance(raw, dict):
                out.append(raw)
        return out

    def rebuild_index(self) -> dict:
        items = [{"plot_id": d.get("plot_id"), "keywords": d.get("memory", {}).get("keywords") or [],
                  "summary": d.get("plot_summary") or d.get("memory", {}).get("summary", "")}
                 for d in self.all()]
        payload = {"schema_version": 1, "derived_from": "plot_deltas", "items": items}
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.index_path, payload)
        arcs = {}
        for delta in self.all():
            arc_id = str(delta.get("arc_id") or "")
            if arc_id:
                arcs.setdefault(arc_id, []).append({"plot_id": delta.get("plot_id"),
                    "summary": delta.get("plot_summary") or delta.get("memory", {}).get("summary", "")})
        for arc_id, entries in arcs.items():
            write_json_atomic(self.root / "cache" / "arc" / f"{arc_id}.json",
                              {"schema_version": 1, "derived_from": "plot_deltas", "arc_id": arc_id, "plots": entries})
        return payload


def load_staged(book_id: str) -> dict:
    raw = read_json(staged_path(book_id)) if staged_path(book_id).exists() else None
    return raw if isinstance(raw, dict) else {"schema_version": 1, "book_id": book_id,
                                                "chapter_num": 0, "revision": 0, "plot_deltas": []}


def save_staged(book_id: str, state: dict) -> dict:
    state = copy.deepcopy(state)
    state["schema_version"] = 1
    state["book_id"] = book_id
    state["revision"] = int(state.get("revision") or 0)
    write_json_atomic(staged_path(book_id), state)
    return state


def clear_staged(book_id: str) -> None:
    staged_path(book_id).unlink(missing_ok=True)


def validate_outcome(outcome) -> dict:
    if outcome is None:
        return {key: [] for key in FACT_KEYS}
    if not isinstance(outcome, dict):
        raise ValueError("outcome 必须是 object")
    unknown = sorted(set(outcome) - set(FACT_KEYS))
    if unknown:
        raise ValueError("outcome 含未知字段: " + ", ".join(unknown))
    result = {}
    for key in FACT_KEYS:
        value = outcome.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"outcome.{key} 必须是 list")
        result[key] = copy.deepcopy(value)
    return result


def make_plot_delta(*, plot_id: str, plot_name: str, chapter_num: int, facts: dict,
                    text: str, run_id: str, reconcile: dict, plot_summary: str = "", arc_id: str = "") -> dict:
    """构造可审计的 Plot Delta；连续性只取正文尾部，不从中推断语义。"""
    facts = facts or {}
    return {
        "plot_id": plot_id, "plot_name": plot_name, "arc_id": arc_id, "chapter_num": int(chapter_num),
        "run_id": run_id, "reconcile": {k: reconcile.get(k) for k in
            ("kind", "matched", "drifts", "unpredicted", "stale", "summary")},
        "event_result": list(facts.get("choices_made") or []) + list(facts.get("information_revealed") or []),
        "character_changes": list(facts.get("character_events") or []),
        "relationship_changes": list(facts.get("relationship_changes") or []),
        "resource_changes": list(facts.get("resource_changes") or []),
        "promise_changes": list(facts.get("promise_updates") or []),
        "opened_questions": list(facts.get("new_story_questions") or []),
        "continuity": {"last_action": (text or "").strip()[-500:]},
        "plot_summary": str(plot_summary or "").strip(),
        "facts": copy.deepcopy(facts),
        "memory": {"summary": ("；".join(str(x) for x in (facts.get("choices_made") or [])[:3])
                               or (text or "").strip()[-300:]),
                   "keywords": _keywords(facts)},
    }


def stage_delta(book_id: str, delta: dict) -> dict:
    state = load_staged(book_id)
    chapter = int(delta.get("chapter_num") or 0)
    if state.get("chapter_num") not in (0, chapter):
        raise ValueError("存在另一章的暂存事实；请先提交或放弃该章节草稿")
    state["chapter_num"] = chapter
    entries = [x for x in (state.get("plot_deltas") or []) if x.get("plot_id") != delta.get("plot_id")]
    entries.append(copy.deepcopy(delta))
    state["plot_deltas"] = entries
    state["revision"] = int(state.get("revision") or 0) + 1
    return save_staged(book_id, state)


def _keywords(value) -> list[str]:
    out = []
    def add(v):
        if isinstance(v, str) and v.strip() and v.strip() not in out:
            out.append(v.strip())
        elif isinstance(v, dict):
            for k, x in v.items():
                if k in {"name", "id", "subject", "type", "to", "question", "location"}:
                    add(x)
        elif isinstance(v, list):
            for x in v: add(x)
    add(value)
    return out[:30]


def previous_change(book_id: str) -> dict:
    entries = load_staged(book_id).get("plot_deltas") or []
    if not entries:
        return {}
    # 只投影上一段的 delta，禁止携带摘要、全量承诺或全量问题。
    d = entries[-1]
    facts = d.get("facts") or {}
    return {
        "plot_id": d.get("plot_id"),
        "character_changes": copy.deepcopy(d.get("character_changes") or []),
        "relationship_changes": copy.deepcopy(d.get("relationship_changes") or []),
        "resource_changes": copy.deepcopy(d.get("resource_changes") or []),
        "information_changes": copy.deepcopy((facts.get("information_revealed") or [])),
        "location_changes": [x for x in (d.get("character_changes") or []) if any(
            (e or {}).get("type") == "location_shift" for e in ((x or {}).get("events") or []))],
        "question_changes": {"opened": copy.deepcopy(d.get("opened_questions") or []),
                             "advanced": [], "closed": []},
        "promise_changes": {"created": copy.deepcopy(d.get("promise_changes") or []),
                            "progressed": [], "closed": []},
        "continuity_anchor": copy.deepcopy(d.get("continuity") or {}),
    }


def build_chapter_delta(book_id: str, chapter_num: int) -> dict:
    staged = load_staged(book_id)
    entries = [x for x in (staged.get("plot_deltas") or []) if int(x.get("chapter_num") or 0) == int(chapter_num)]
    result = {"chapter_num": int(chapter_num), "plot_deltas": copy.deepcopy(entries),
              "facts_added": [], "character_changes": [], "relationship_changes": [],
              "resource_changes": [], "questions_opened": [], "promise_changes": [],
              "world_state_changes": []}
    for d in entries:
        result["facts_added"].extend(d.get("event_result") or [])
        result["character_changes"].extend(d.get("character_changes") or [])
        result["relationship_changes"].extend(d.get("relationship_changes") or [])
        result["resource_changes"].extend(d.get("resource_changes") or [])
        result["questions_opened"].extend(d.get("opened_questions") or [])
        result["promise_changes"].extend(d.get("promise_changes") or [])
    return result


def commit_chapter_delta(book_id: str, chapter_delta: dict) -> dict:
    store = PlotMemoryStore(book_id)
    for delta in chapter_delta.get("plot_deltas") or []:
        store.append(delta)
    store.rebuild_index()
    # 保留旧汇总文件，供已有书籍和工具懒加载兼容；不再作为权威来源。
    raw = read_json(history_path(book_id)) if history_path(book_id).exists() else None
    memory = raw if isinstance(raw, dict) else {"schema_version": 1, "chapters": []}
    memory["chapters"] = [x for x in memory.get("chapters", []) if x.get("chapter_num") != chapter_delta.get("chapter_num")]
    memory["chapters"].append(copy.deepcopy(chapter_delta))
    memory["schema_version"] = 1
    write_json_atomic(history_path(book_id), memory)
    clear_staged(book_id)
    return chapter_delta


def retrieved_memory(book_id: str, terms: list[str], limit: int = 6) -> list[dict]:
    store = PlotMemoryStore(book_id)
    deltas = store.all()
    if not deltas:  # 旧书懒加载回退，首次新提交会逐步转入 canonical store。
        raw = read_json(history_path(book_id)) if history_path(book_id).exists() else {}
        deltas = [d for chapter in (raw or {}).get("chapters") or [] for d in chapter.get("plot_deltas") or []]
    needles = {str(x).strip() for x in terms if str(x).strip()}
    found = []
    for delta in reversed(deltas):
        text = " ".join(delta.get("memory", {}).get("keywords") or [])
        if needles and any(x in text for x in needles):
            found.append({"plot_id": delta.get("plot_id"), "summary": delta.get("plot_summary") or delta.get("memory", {}).get("summary", ""),
                          "facts": delta.get("facts", {})})
            if len(found) >= limit:
                return found
    return found
