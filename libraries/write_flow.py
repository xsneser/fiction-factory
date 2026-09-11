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
# FSM 实际会写入的阶段：PREPARING_PLOT / EVALUATING / COMMITTING_CHAPTER / QUALITY_GATE /
# REPLANNING / WAIT_CONFIRM / FAILED / DONE。
# IDLE / WRITING_PLOT / COMMITTING_PLOT 目前只是**标签位**（合法但无人写入）——保留以便
# UI/审计按语义命名，不要据此以为 FSM 会停在这些状态上。
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


def _chapter_bands(target: int) -> dict:
    """章节字数区间：由 target 按比例推导（**单点计算**，不要散到 agent_tools / dsh_bridge）。

    只有下限（`ready = words >= target`）会让 3000 字的章一路写到 5000
    （book_002 第 6 章实测 4260）；而「到 target 就机械换章」又会把剧情切断。
    """
    from libraries.reviewer import HARD_MIN_RATIO
    t = max(1, int(target or 0))
    return {"soft_min": round(t * 0.90), "soft_max": round(t * 1.10), "hard_max": round(t * 1.20),
            "commit_floor": int(t * HARD_MIN_RATIO)}


def chapter_status(book_id: str, tl, draft: dict | None, *, needs_replan: bool,
                   next_plot_planned_words: int | None,
                   next_plot_break_after: str = "allowed") -> dict:
    """章节进度 + **预测式**收章判定。

    `next_plot_planned_words=None` 表示没有待写 committed 情节段（**不要用 0 当哨兵**：
    0 与「没有下一个」是两回事）。调用方一律用 `storyline_writer.planned_words(next_plot)`。

    收章只在情节段边界发生（本函数由 FSM 在每次 Writer 提交后调用），所以「正在写的
    不可切断高潮」天然不会被拦腰截断——不存在「写到一半强制收章」的时点。
    """
    draft = draft or {}
    words = int(draft.get("words") or count_prose_units(
        "\n\n".join(str(x.get("text") or "") for x in draft.get("bridges") or [])))
    target = int(getattr(tl, "words_per_chapter", 0) or 3000)
    bands = _chapter_bands(target)
    drafted = {str(x.get("plot_id") or "") for x in draft.get("bridges") or []}
    next_exists = any(not getattr(p, "written_chapter", 0) and p.id not in drafted
                      for p in (tl.plots or []))
    next_words = int(next_plot_planned_words) if next_plot_planned_words is not None else None
    predicted = (words + next_words) if next_words is not None else None
    break_after = str(next_plot_break_after or "allowed").lower()
    if break_after not in ("preferred", "allowed", "avoid"):
        break_after = "allowed"
    closure = bool(drafted and draft.get("bridges"))
    ready, reason = False, "need_more_words"
    forced = False
    can_commit = closure and words >= bands["commit_floor"]
    if closure and words >= bands["hard_max"]:
        ready, reason = True, "hard_max_reached"
    elif closure and words >= bands["soft_max"] and break_after in ("preferred", "allowed"):
        ready, reason = True, "soft_max_reached"
    elif next_exists and predicted is not None and predicted > bands["hard_max"] and can_commit:
        # 预测式：再塞一个完整情节段会冲破硬上限 → 这次就收章（**不是**到 target 就换章）。
        # `can_commit` 守卫是给 legacy 超长情节段的：章内字数还没到落盘下限时收章会被
        # save_chapter_text 拒（0.6×target），结果是「既不能继续写也不能收章」的死锁。
        # 新情节段（≤1200）走不到这条分支——触发它需要 words>2400，本就高于下限。
        ready, reason, forced = True, "next_plot_would_exceed_hard_max", True
    elif (next_exists and predicted is not None and predicted > bands["soft_max"]
          and break_after == "preferred" and can_commit):
        ready, reason = True, "predicted_crosses_soft_max"
    elif not next_exists and closure and words >= bands["soft_min"]:
        # 情节段耗尽但已过软下限 → 自然收束，不为凑满 target 强启一段新剧情。
        ready, reason = True, "plot_exhausted_at_soft_min"
    elif not next_exists:
        reason = "plot_exhausted_needs_replan" if needs_replan else "plot_exhausted_without_replan"
    return {"chapter_ready": ready, "reason": reason, "written_words": words,
            "target_words": target,
            "soft_min_words": bands["soft_min"], "soft_max_words": bands["soft_max"],
            "hard_max_words": bands["hard_max"], "commit_floor": bands["commit_floor"],
            "has_next_committed_plot": next_exists, "has_legal_closure": closure,
            "next_plot_planned_words": next_words,
            "predicted_words_after_next_plot": predicted,
            "next_plot_allowed": not ready,
            "forced_budget_boundary": forced,
            "next_plot_break_after": break_after}


def next_action(status: dict) -> str:
    """按状态选下一个动作。**优先级是有意固定的**：

    章满收章 → 还有可写 Plot 就继续写 → 都没有才续规划 → 否则失败。

    「还有可写 Plot」优先于边界信号：`detect_story_boundary` 给出的 PLOTS_LOW/WORDS_LOW
    是**续规划信号**（UI 横幅 + 计划器输入），不是「停写」信号。已承诺的情节段是已经向读者
    承诺的内容，写它们不需要新规划；在情节段耗尽前为边界中断写作只会白烧一轮计划器会话
    （一次 ≈ 10 轮 LLM）。相应地：审查/改动这里前请先读 planning_state 的注释与批次策略
    （REPLAN_TARGET_WORDS / REPLAN_MIN_REMAINING_WORDS），别顺手把边界提到 has_next 之前。
    """
    if status.get("chapter_ready"):
        return "COMMITTING_CHAPTER"
    if status.get("has_next_committed_plot") and status.get("next_plot_allowed", True):
        return "PREPARING_PLOT"
    if status.get("reason") == "plot_exhausted_needs_replan":
        return "REPLANNING"
    return "FAILED"
