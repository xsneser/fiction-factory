"""主 Agent 编排的硬预算（不变量 I8）。

服务端 FSM 原来天然有状态循环边界（每轮由 `next_action` 决定，模型不掌权）。调度权交给
root Agent 后，循环边界必须由服务端继续钉住，否则「Write → Critic → 改稿 → Critic → …」
没有任何东西叫停，一次用户任务能把整章预算和上下文全烧在自旋上。

计数**按章**落盘（`books/<id>/orchestration_budget.json`）：root 默认一次任务写一章，
所以按章计数等价于「按 run」，而且天然跨进程重启累计——重启不能成为绕过预算的后门。

刻意**不**把这些计数写进 write_flow 记录：flow 带写租约，而预算必须在租约之外也能读
（见不变量 I7「不要为了省一个文件去扩大租约生命周期」的同一考虑）。
"""
from __future__ import annotations

import os
from pathlib import Path

from core.json_store import read_json, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]


def _cap(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def limits() -> dict:
    """三个预算上限（env 可覆盖；非法值回退默认，不抛）。"""
    return {
        "actions_max": _cap("MAX_ORCHESTRATOR_ACTIONS_PER_RUN", 24),
        "revise_max": _cap("MAX_REVISE_ATTEMPTS_PER_PLOT", 2),
        "replan_max": _cap("MAX_REPLAN_ATTEMPTS_PER_RUN", 2),
    }


def _path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "orchestration_budget.json"


def read(book_id: str, chapter_num: int) -> dict:
    """读当前章的计数；章号变化（换章）即视为全新预算，**纯读不写盘**。"""
    raw = read_json(_path(book_id)) if _path(book_id).exists() else None
    state = raw if isinstance(raw, dict) else {}
    if int(state.get("chapter_num") or 0) != int(chapter_num or 0):
        state = {}
    return {"actions": int(state.get("actions") or 0),
            "revise": {str(k): int(v or 0) for k, v in (state.get("revise") or {}).items()},
            "replan": int(state.get("replan") or 0)}


def bump(book_id: str, chapter_num: int, key: str, plot_id: str = "") -> dict:
    """记一次动作（key ∈ actions|revise|replan）；换章自动清零。返回记完的计数。"""
    if key not in ("actions", "revise", "replan"):
        raise ValueError(f"未知预算键: {key}")
    state = read(book_id, chapter_num)
    if key == "revise":
        pid = str(plot_id or "")
        state["revise"][pid] = int(state["revise"].get(pid) or 0) + 1
    else:
        state[key] = int(state[key] or 0) + 1
    write_json_atomic(_path(book_id), {"schema_version": 1, "chapter_num": int(chapter_num or 0),
                                       "actions": state["actions"], "revise": state["revise"],
                                       "replan": state["replan"]})
    return state


def snapshot(book_id: str, chapter_num: int, plot_id: str = "") -> dict:
    """给 facts 用的预算视图：已用 / 上限 / 剩余（revise 按当前 Plot 计）。"""
    caps = limits()
    used = read(book_id, chapter_num)
    revise_used = int(used["revise"].get(str(plot_id or "")) or 0)
    return {
        "actions_used": used["actions"], "actions_max": caps["actions_max"],
        "revise_used": revise_used, "revise_max": caps["revise_max"],
        "replan_used": used["replan"], "replan_max": caps["replan_max"],
        "actions_remaining": max(0, caps["actions_max"] - used["actions"]),
        "revise_remaining": max(0, caps["revise_max"] - revise_used),
        "replan_remaining": max(0, caps["replan_max"] - used["replan"]),
    }
