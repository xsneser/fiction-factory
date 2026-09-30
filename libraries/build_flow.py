"""持久化、显式的建书流程状态机。

与 `libraries/write_flow.py` 同形（**LLM 不拥有流程状态**，这里只负责可恢复状态与
下一步判定），但有四点建书特有，改前先读：

1. **键是 `build_session_id`，不是 book_id**：建书期书还不存在——书要等用户在自己浏览器
   点「创建并进入写作台」才由 `/books/start` 创建。所以落 `storage/build_flows/<sid>.json`。
   **不要**复用 `storage/build_sessions/<sid>.json`：那是 `libraries/planning_state.py`
   的 planning 草稿，且 `attach_build_session` 在建书成功时会把它 `unlink()` 掉，
   两者同文件会互相踩且会随建书一起消失。
2. **不做租约**：写作租约是「一本书一个 Flow」的资源锁；建书没有对应资源（可以多开向导），
   而 `dsh_bridge.run_dsh_task` 开头 `interrupt_current_task()` 已保证全服务同一时刻只有
   一个 dsh 任务，并发由它兜住。别以为这里漏了。
3. **阶段权威在浏览器**：向导把 cur/_picked/created/book_id/has_world/has_outline/...
   上报到 `storage/build_status.json`（`libraries/build_status.py`，**全局单快照、非按
   session 分键**）。因此判定必须带**新鲜度**检查：过期快照可能来自早已关闭的向导页，
   那时它不构成「当前在建书」的权威，应当退回关键词兜底而不是照它起 run。
4. `drive_ui` 是**异步**的（写 nav_intent 队列、浏览器轮询后才落到表单），工具返回 ≠
   表单已填。所以「填完了没」只能看浏览器回报的快照，不能看工具返回值。
"""
from __future__ import annotations

import copy
import time
from pathlib import Path

from core.json_store import read_json, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]

# FSM 会写入的阶段：
#   STAGING     步 1-2（候选）
#   BUILDING    步 3（agent 正在填内容构建工作台）
#   AWAIT_USER  内容已填好、**等用户 review 后自己点提交**（无进程在跑，对应写作的 WAIT_CONFIRM）
#   SUBMITTED   浏览器已建书（快照带回 book_id）
#   FAILED      显式失败（快照带 submit_error，或子 run 连续无产出）
#   DONE        收尾完成
# 注意：`next_action()` 不返回 AWAIT_USER——它是「一轮 build run 跑完后记录的停顿态」，
# 不是「下一个要 spawn 的动作」。
PHASES = {"STAGING", "BUILDING", "AWAIT_USER", "SUBMITTED", "FAILED", "DONE"}

# 快照新鲜度窗口：`updated_at` 超过这个时长即视为「没有向导在跑」。
FRESH_WINDOW_SECONDS = 30 * 60

# 保留的流程记录数（建书成功不会删记录——它是有用的审计线索；只做有界清理）。
MAX_KEPT_FLOWS = 50


def _flows_dir() -> Path:
    return ROOT / "storage" / "build_flows"


def flow_path(session_id: str) -> Path:
    safe = "".join(c for c in str(session_id or "") if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("build_session_id 不能为空")
    return _flows_dir() / f"{safe}.json"


def load_flow(session_id: str) -> dict | None:
    path = flow_path(session_id)
    raw = read_json(path) if path.exists() else None
    return raw if isinstance(raw, dict) else None


def save_flow(session_id: str, flow: dict) -> dict:
    if flow.get("phase") not in PHASES:
        raise ValueError(f"未知建书流程阶段: {flow.get('phase')}")
    flow = copy.deepcopy(flow)
    flow["updated_at"] = time.time()
    write_json_atomic(flow_path(session_id), flow)
    return flow


def _prune(session_id: str) -> None:
    """有界化：只保留最近 MAX_KEPT_FLOWS 份记录（建书记录不随建书成功删除）。"""
    try:
        files = sorted(_flows_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    keep = flow_path(session_id).name
    for path in files[MAX_KEPT_FLOWS:]:
        if path.name == keep:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def start_flow(session_id: str, *, mode: str = "") -> dict:
    flow = save_flow(session_id, {
        "schema_version": 1, "session_id": session_id, "flow_id": session_id,
        "mode": mode,                 # 起这个 Flow 时的入口（便于审计）
        "phase": "STAGING",
        "child_runs": [],             # 子 run 审计记录（parent Flow 可恢复）
        "attempts": 0,                # 连续「跑完仍无可填内容」轮数，上限后显式 FAILED
        "error": None,
        "resume_point": "STAGING",
        "created_at": time.time(),
    })
    _prune(session_id)
    return flow


def transition(session_id: str, phase: str, **updates) -> dict:
    flow = load_flow(session_id)
    if not flow:
        raise RuntimeError("建书流程不存在")
    flow.update(updates)
    flow["phase"] = phase
    flow["resume_point"] = phase
    return save_flow(session_id, flow)


def append_child_run(session_id: str, phase: str, *, kind: str, ok: bool) -> dict:
    """记一条子 run 审计（建书流程没有独立租约，子 run 记录就是可恢复的现场）。"""
    flow = load_flow(session_id) or start_flow(session_id)
    children = list(flow.get("child_runs") or [])
    children.append({"kind": kind, "phase": phase, "ok": bool(ok), "completed_at": time.time()})
    return transition(session_id, phase, child_runs=children)


# ── 纯函数：快照 → 阶段 → 下一个动作（零成本可测，不 spawn 任何东西）──────────


def _is_fresh(snapshot: dict, *, now: float | None = None) -> bool:
    """快照是否新鲜（`updated_at` 可解析且在窗口内）。

    时效戳由浏览器本地 `time.strftime` 写（`build_status.set_build_status`），故按本地时区解析。
    """
    ts = str((snapshot or {}).get("updated_at") or "").strip()
    if not ts:
        return False
    try:
        when = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return False
    return ((now if now is not None else time.time()) - when) <= FRESH_WINDOW_SECONDS


def build_stage(snapshot: dict | None, *, now: float | None = None) -> dict:
    """把浏览器上报的建书快照翻译成阶段状态（纯函数）。"""
    snap = snapshot or {}
    stage = {
        "fresh": _is_fresh(snap, now=now),
        "cur": int(snap.get("cur") or 1),
        "session_id": str(snap.get("build_session_id") or ""),
        "book_id": str(snap.get("book_id") or ""),
        "created": bool(snap.get("book_id")),
        "picked": bool(snap.get("_picked")),
        "pen_selected": bool(snap.get("pen_selected")),
        "has_world": bool(snap.get("has_world")),
        "has_outline": bool(snap.get("has_outline")),
        "submit_error": str(snap.get("submit_error") or ""),
    }
    if stage["created"]:
        stage["phase"] = "SUBMITTED"
    elif stage["submit_error"]:
        stage["phase"] = "FAILED"
    elif stage["cur"] >= 3:
        stage["phase"] = "BUILDING"
    else:
        stage["phase"] = "STAGING"
    return stage


def next_action(stage: dict) -> str:
    """按阶段选下一个动作。**优先级是有意固定的**，改前先想清楚：

        快照过期 → 收尾/完成 → 建书失败 → 步 3 填表 → 步 1-2 候选

    - STALE：没有可信的向导状态 → 调用方退回关键词兜底，**不要**照陈旧快照起 run。
    - SUBMITTED 优先于 FAILED：两者互斥（成功带回 book_id / 失败带 submit_error），
      但若同现则成功是终态。
    - 步 3 只要 `cur>=3` 就进 BUILD——**不要求 `_picked`**：手工跳过候选的路径
      （向导的「跳过，手动设定」）就没有 _picked，但它同样在步 3。
    """
    if not stage.get("fresh"):
        return "STALE"
    if stage.get("created") or stage.get("book_id"):
        return "SUBMITTED"
    if stage.get("submit_error"):
        return "FAILED"
    if int(stage.get("cur") or 1) >= 3:
        return "BUILD"
    return "CANDIDATES"
