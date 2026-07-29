"""
全局任务管理器 — 跨页面跟踪运行中的抓取/分析任务

用法:
    from plugins.task_manager import task_manager
    task_manager.start("小说抓取", title="书名", total=30)
    task_manager.progress(current=5, phase="下载", message="第5章")
    task_manager.done(message="下载完成")
"""
import time
import threading
from datetime import datetime
from typing import Optional

_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def start(task_id: str, name: str = "", title: str = "",
           total: int = 0, phase: str = "准备中"):
    """注册一个新任务"""
    with _lock:
        _tasks[task_id] = {
            "id": task_id,
            "name": name or task_id,
            "title": title,
            "phase": phase,
            "current": 0,
            "total": total,
            "logs": [],
            "started_at": _now(),
            "status": "running",
        }


def progress(task_id: str, current: int = 0, total: int = 0,
             phase: str = "", message: str = ""):
    """更新任务进度"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        if current:
            t["current"] = current
        if total:
            t["total"] = total
        if phase:
            t["phase"] = phase
        if message:
            pass  # message is used for display, store in phase for now
        t["phase_display"] = message or phase


def log(task_id: str, message: str, level: str = "info"):
    """添加日志条目"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t.setdefault("logs", []).append({
            "time": _now(),
            "message": message,
            "level": level,
        })
        # 只保留最近50条日志
        if len(t["logs"]) > 50:
            t["logs"] = t["logs"][-50:]


def done(task_id: str, message: str = "完成"):
    """标记任务完成"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["status"] = "done"
        t["phase"] = "完成"
        t["phase_display"] = f"✅ {message}"
        t["current"] = t["total"]
        t["ended_at"] = _now()


def fail(task_id: str, message: str = "失败"):
    """标记任务失败"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["status"] = "failed"
        t["phase"] = "失败"
        t["phase_display"] = f"❌ {message}"
        t["ended_at"] = _now()


def get_tasks() -> list[dict]:
    """获取所有活跃任务（含刚完成的）"""
    with _lock:
        now = time.time()
        result = []
        to_remove = []
        for tid, t in _tasks.items():
            # 已完成/失败超过30秒的自动清理
            if t["status"] in ("done", "failed") and t.get("ended_at"):
                # 简单清理：保留最近的
                pass
            result.append({
                "id": t["id"],
                "name": t["name"],
                "title": t.get("title", ""),
                "phase": t.get("phase_display", t["phase"]),
                "current": t["current"],
                "total": t["total"],
                "status": t["status"],
                "time": t.get("started_at", ""),
                "logs": t.get("logs", [])[-10:],  # 最近10条
            })
        return result


def clear_old(keep_seconds: int = 60):
    """清理旧任务"""
    with _lock:
        now = time.time()
        to_remove = []
        for tid, t in _tasks.items():
            if t["status"] in ("done", "failed"):
                to_remove.append(tid)
        for tid in to_remove:
            _tasks.pop(tid, None)
