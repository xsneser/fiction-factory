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

from core.json_store import file_lock, read_json, write_json_atomic

_PATH = os.path.join("storage", "crawl_progress.json")
_DEFAULT_TASK = "default"
_TERMINAL_STATES = ("done", "error", "cancelled")
_KEEP_TERMINAL_SECONDS = 60
# 步骤清单上限：保最新 N 条（综合抓取多源时含 目录校对/取番茄头章/每源校验/下载进度等。
# 17 源 × 3 阶段（镜像解析/校对/下载）≈ 51 + 操作步骤 ≈ 60+；设 120 保证完整流程不被掐尾，
# 避免下载阶段新增步骤时挤掉早期「解析番茄/创建书目」导致前端操作顺序错乱）
_MAX_STEPS = 120

# 步骤状态枚举（前端映射图标/颜色）
_STEP_RUNNING = "running"
_STEP_OK = "ok"
_STEP_WARN = "warn"
_STEP_ERROR = "error"


def write_crawl_progress(state: str, phase: str = "", current: int = 0,
                         total: int = 0, message: str = "", extra: dict = None,
                         task_id: str = "", title: str = "", step: dict = None) -> dict:
    """写一个任务的进度快照（原子替换），返回该任务快照 dict。

    task_id 为空 → 归 'default' 键（兼容旧调用方）；多任务并行各写各键互不覆盖。
    step（可选 {label,status,detail}）：追加进该任务的 steps 清单——**按 label 幂等替换**
    （running→ok/warn/error 原地转态），新 label 追加；超 _MAX_STEPS 掐尾保最新。
    steps 在每次快照里前移保留：terminal 后同 task_id 重下（running/paused 首写）自动清空
    上一轮残留。前端经 GET /api/crawl/progress 读 t.steps 渲染步骤清单。
    terminal 状态（done/error/cancelled）任务保留 _KEEP_TERMINAL_SECONDS 供前端展示后自动清。
    """
    tid = task_id or _DEFAULT_TASK
    now = time.time()
    # 外层锁包住读-改-写（read_json/write_json_atomic 内取同锁为可重入），
    # 关掉同进程内 read→write 间隙，steps 累积不丢；跨进程仍原子替换（丢步为既有现状）。
    with file_lock(_PATH):
        data = read_crawl_progress()
        tasks = data.get("tasks", {})
        old = tasks.get(tid) or {}
        # 同 task_id 复用且上一轮已终态 → 新一轮 running/paused 先清残留步骤
        if old.get("state") in _TERMINAL_STATES and state in ("running", "paused"):
            steps = []
        else:
            steps = list((old.get("steps") or [])[:])
        if step:
            st = {
                "label": step.get("label", ""),
                "status": step.get("status", _STEP_RUNNING),
                "detail": step.get("detail", ""),
                "ts": now,
            }
            if step.get("url"):
                st["url"] = step["url"]   # 源相关步骤携带书页/主页链接（左栏渲染为超链接）
            for i, ex in enumerate(steps):
                if ex.get("label") == st["label"]:
                    steps[i] = st
                    break
            else:
                steps.append(st)
            if len(steps) > _MAX_STEPS:
                steps = steps[-_MAX_STEPS:]
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
        if steps:
            snap["steps"] = steps
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
