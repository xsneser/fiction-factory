"""持久化、显式的写作流程状态机。

LLM 不拥有流程状态。这个模块只负责可恢复状态、写作租约和下一步判定；真正的
Plot 写作与 Planner 仍由桥接层分别启动的子 Run 完成。
"""
from __future__ import annotations

import copy
import os
import time
import uuid
from pathlib import Path

from core.json_store import read_json, write_json_atomic
from core.text_utils import count_prose_units


ROOT = Path(__file__).resolve().parents[1]
PHASES = {"IDLE", "PREPARING_PLOT", "WRITING_PLOT", "COMMITTING_PLOT", "EVALUATING",
          "COMMITTING_CHAPTER", "QUALITY_GATE", "REPLANNING", "WAIT_CONFIRM", "FAILED", "DONE"}
DEFAULT_LEASE_SECONDS = 15 * 60


def _dir(book_id: str) -> Path:
    return ROOT / "books" / book_id


def _flow_path(book_id: str, flow_id: str) -> Path:
    return _dir(book_id) / "write_flows" / f"{flow_id}.json"


def _lease_path(book_id: str) -> Path:
    return _dir(book_id) / "write_lease.json"


def load_flow(book_id: str, flow_id: str) -> dict | None:
    path = _flow_path(book_id, flow_id)
    raw = read_json(path) if path.exists() else None
    return raw if isinstance(raw, dict) else None


def save_flow(book_id: str, flow: dict) -> dict:
    if flow.get("phase") not in PHASES:
        raise ValueError(f"未知写作流程阶段: {flow.get('phase')}")
    flow = copy.deepcopy(flow)
    flow["updated_at"] = time.time()
    write_json_atomic(_flow_path(book_id, flow["flow_id"]), flow)
    return flow


def acquire_lease(book_id: str, flow_id: str, ttl_seconds: int = DEFAULT_LEASE_SECONDS,
                  takeover: bool = False) -> dict:
    now = time.time()
    path = _lease_path(book_id)
    lease = read_json(path) if path.exists() else None
    if isinstance(lease, dict) and lease.get("owner_flow_id") != flow_id and float(lease.get("expires_at") or 0) > now and not takeover:
        raise RuntimeError("本书已有进行中的写作任务；请恢复原任务或明确接管")
    next_lease = {"owner_flow_id": flow_id, "acquired_at": now, "heartbeat_at": now,
                  "expires_at": now + int(ttl_seconds)}
    write_json_atomic(path, next_lease)
    return next_lease


def heartbeat_lease(book_id: str, flow_id: str, ttl_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
    lease = read_json(_lease_path(book_id)) if _lease_path(book_id).exists() else None
    if not isinstance(lease, dict) or lease.get("owner_flow_id") != flow_id:
        raise RuntimeError("写作租约不属于当前流程")
    now = time.time()
    lease.update(heartbeat_at=now, expires_at=now + int(ttl_seconds))
    write_json_atomic(_lease_path(book_id), lease)


def release_lease(book_id: str, flow_id: str) -> None:
    path = _lease_path(book_id)
    lease = read_json(path) if path.exists() else None
    if isinstance(lease, dict) and lease.get("owner_flow_id") == flow_id:
        path.unlink(missing_ok=True)


def active_flow_id(book_id: str) -> str:
    """返回尚未过期的租约所属 Flow；供旧入口在同一章节内续接。"""
    lease = read_json(_lease_path(book_id)) if _lease_path(book_id).exists() else None
    if isinstance(lease, dict) and float(lease.get("expires_at") or 0) > time.time():
        return str(lease.get("owner_flow_id") or "")
    return ""


def start_flow(book_id: str, chapter_num: int, *, takeover: bool = False) -> dict:
    flow_id = uuid.uuid4().hex
    acquire_lease(book_id, flow_id, takeover=takeover)
    return save_flow(book_id, {"schema_version": 1, "flow_id": flow_id, "book_id": book_id,
        "chapter_num": int(chapter_num), "phase": "PREPARING_PLOT", "current_plot_id": "",
        "completed_plot_ids": [], "child_runs": [], "replan_state": {}, "error": None,
        "resume_point": "PREPARING_PLOT", "created_at": time.time()})


def transition(book_id: str, flow_id: str, phase: str, **updates) -> dict:
    flow = load_flow(book_id, flow_id)
    if not flow:
        raise RuntimeError("写作流程不存在")
    flow.update(updates)
    flow["phase"] = phase
    flow["resume_point"] = phase
    heartbeat_lease(book_id, flow_id)
    return save_flow(book_id, flow)


def chapter_status(book_id: str, tl, draft: dict | None, *, needs_replan: bool) -> dict:
    draft = draft or {}
    words = int(draft.get("words") or count_prose_units("\n\n".join(str(x.get("text") or "") for x in draft.get("bridges") or [])))
    target = int(getattr(tl, "words_per_chapter", 0) or 3000)
    drafted = {str(x.get("plot_id") or "") for x in draft.get("bridges") or []}
    next_exists = any(not getattr(p, "written_chapter", 0) and p.id not in drafted for p in (tl.plots or []))
    ready = bool(words >= target and drafted)
    if ready:
        reason = "target_reached"
    elif next_exists:
        reason = "need_more_words"
    elif needs_replan:
        reason = "plot_exhausted_needs_replan"
    else:
        reason = "plot_exhausted_without_replan"
    return {"chapter_ready": ready, "reason": reason, "written_words": words,
            "target_words": target, "has_next_committed_plot": next_exists}


def next_action(status: dict) -> str:
    if status.get("chapter_ready"):
        return "COMMITTING_CHAPTER"
    if status.get("has_next_committed_plot"):
        return "PREPARING_PLOT"
    if status.get("reason") == "plot_exhausted_needs_replan":
        return "REPLANNING"
    return "FAILED"
