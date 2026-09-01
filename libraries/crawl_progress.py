"""小说抓取进度 — 跨进程共享状态文件（多任务）。

MCP 工具（fetch_novel/fetch_webnovel/fetch_book，跑在 MCP 进程）与 Web 表单抓取
（/api/scout/run，跑在 Flask 进程）共用同一进度源。**支持并行**：storage/crawl_progress.json
存 `{"tasks": {<task_id>: 快照}, "active": <task_id>, "ts": <now>}`，每个下载写自己的键，
互不覆盖；/scout 页轮询 `GET /api/crawl/progress` 实时展示每个任务。

state ∈ running / done / error / cancelled / paused。
兼容旧单任务格式：read 侧把无 `tasks` 键的旧快照归一为 `{"tasks": {"default": old}}`；
write 侧 task_id 为空 → 归 `"default"` 键。
"""
import os
import time

from core.json_store import read_json, write_json_atomic

_PATH = os.path.join("storage", "crawl_progress.json")
_DEFAULT_TASK = "default"
_TERMINAL_STATES = ("done", "error", "cancelled")
_KEEP_TERMINAL_SECONDS = 60


def write_crawl_progress(state: str, phase: str = "", current: int = 0,
                         total: int = 0, message: str = "", extra: dict = None,
                         task_id: str = "", title: str = "") -> dict:
    """写一个任务的进度快照（原子替换），返回该任务快照 dict。

    task_id 为空 → 归 'default' 键（兼容旧调用方）；多任务并行各写各键互不覆盖。
    terminal 状态（done/error/cancelled）任务保留 _KEEP_TERMINAL_SECONDS 供前端展示后自动清。
    """
    tid = task_id or _DEFAULT_TASK
    now = time.time()
    data = read_crawl_progress()
    tasks = data.get("tasks", {})
    snap = {
        "state": state,
        "phase": phase,
        "current": current,
        "total": total,
        "message": message or "",
        "ts": now,
    }
    if title:
        snap["title"] = title
    if extra:
        snap["extra"] = extra
    tasks[tid] = snap
    # 清理 terminal 状态过旧任务（保留 60s 给前端展示后移除）
    tasks = {k: v for k, v in tasks.items()
             if v.get("state") not in _TERMINAL_STATES
             or now - v.get("ts", 0) < _KEEP_TERMINAL_SECONDS}
    write_json_atomic(_PATH, {"tasks": tasks, "active": tid, "ts": now})
    return snap


def read_crawl_progress() -> dict:
    """读全部任务进度快照：{tasks: {id: 快照}, active, ts}。

    兼容旧单任务格式（无 tasks 键、带 state 的旧快照 → 归一为 default 键）。
    无记录返回 {"tasks": {}, "active": "", "ts": 0}。
    """
    d = read_json(_PATH, {}) or {}
    if "tasks" in d:
        return d
    if d.get("state"):   # 旧单任务格式
        return {"tasks": {_DEFAULT_TASK: d}, "active": _DEFAULT_TASK,
                "ts": d.get("ts", 0)}
    return {"tasks": {}, "active": "", "ts": 0}
