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

_lock = threading.Lock()
_tasks: dict[str, dict] = {}
# 每个任务关联一个取消事件，worker 线程定期检查
_cancel_events: dict[str, threading.Event] = {}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def start(task_id: str, name: str = "", title: str = "",
           total: int = 0, phase: str = "准备中", url: str = "",
           agent: str = "", book_id: str = "", book_title: str = "",
           step: str = "", sub_step: str = "", llm_calls: int = 0):
    """注册一个新任务（url: 任务对应页面的跳转地址，供侧边栏“查看”按钮使用）。

    agent 系列字段供右侧栏「Agent 活动面板」使用：
      agent      — 角色（writing/outline/world/title/outline_agent/scout）
      book_id    — 操作对象书 id
      book_title — 书标题（展示用）
      step       — 当前步骤（如「写桥段·第3章」）
      sub_step   — 子步骤（如「笑点探测器命中」）
      llm_calls  — 已发起的 LLM 调用次数（展示级近似计数，非计费）
    """
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
            "started_at_ts": time.time(),
            "status": "running",
            "url": url,
            "agent": agent,
            "book_id": book_id,
            "book_title": book_title,
            "step": step,
            "sub_step": sub_step,
            "llm_calls": llm_calls,
        }


def ensure_single(name: str):
    """确保同一工具名只有一个任务（单任务互斥）。
    同 name 的所有旧任务（运行中/已完成/失败）都会被取消并移除，由新任务替代。
    返回被替代的旧任务 id（无则 None）。
    """
    with _lock:
        replaced = None
        for tid, t in list(_tasks.items()):
            if t.get("name") == name:
                if t["status"] == "running":
                    t["status"] = "cancelled"
                    t["phase"] = "已取消"
                    t["phase_display"] = "⏹️ 被新任务替代"
                    t["ended_at"] = _now()
                    t["ended_at_ts"] = time.time()
                    evt = _cancel_events.get(tid)
                    if evt:
                        evt.set()  # 通知旧 worker 线程停止
                _tasks.pop(tid, None)
                _cancel_events.pop(tid, None)
                replaced = tid
        return replaced


def register_cancel(task_id: str):
    """注册一个取消事件，供 worker 线程检查"""
    with _lock:
        _cancel_events[task_id] = threading.Event()


def cancel(task_id: str):
    """取消指定任务（触发取消事件，标记状态）"""
    with _lock:
        t = _tasks.get(task_id)
        if t and t["status"] == "running":
            t["status"] = "cancelled"
            t["phase"] = "已取消"
            t["phase_display"] = "⏹️ 已取消"
            t["ended_at"] = _now()
            t["ended_at_ts"] = time.time()
        evt = _cancel_events.get(task_id)
    if evt:
        evt.set()  # 通知 worker 线程停止


def is_cancelled(task_id: str) -> bool:
    """检查任务是否被取消（worker 线程调用）"""
    with _lock:
        evt = _cancel_events.get(task_id)
    return evt is not None and evt.is_set()


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


def llm_call(task_id: str, n: int = 1):
    """记录 LLM 调用次数 +n（右侧栏 Agent 卡片展示用近似计数）。"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["llm_calls"] = t.get("llm_calls", 0) + n


def set_step(task_id: str, step=None, sub_step=None):
    """更新当前步骤/子步骤。step/sub_step 为 None 表示不修改，空串表示清空。"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        if step is not None:
            t["step"] = step
        if sub_step is not None:
            t["sub_step"] = sub_step


def get(task_id: str):
    """返回任务原 dict（内部/调试用），不存在返回 None。"""
    with _lock:
        return _tasks.get(task_id)


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
        t["ended_at_ts"] = time.time()


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
        t["ended_at_ts"] = time.time()


def get_tasks() -> list[dict]:
    """获取所有任务（关闭按钮手动清理，不作自动清理）"""
    with _lock:
        result = []
        for tid, t in list(_tasks.items()):
            result.append({
                "id": t["id"],
                "name": t["name"],
                "title": t.get("title", ""),
                "phase": t.get("phase_display", t["phase"]),
                "current": t["current"],
                "total": t["total"],
                "status": t["status"],
                "time": t.get("started_at", ""),
                "started_at_ts": t.get("started_at_ts", 0),
                "url": t.get("url", ""),
                "logs": t.get("logs", [])[-10:],  # 最近10条
                "agent": t.get("agent", ""),
                "book_id": t.get("book_id", ""),
                "book_title": t.get("book_title", ""),
                "step": t.get("step", ""),
                "sub_step": t.get("sub_step", ""),
                "llm_calls": t.get("llm_calls", 0),
            })
        return result


def clear_old(keep_seconds: int = 60):
    """清理超过 keep_seconds 秒的已完成/失败任务"""
    with _lock:
        now = time.time()
        to_remove = []
        for tid, t in _tasks.items():
            if t["status"] in ("done", "failed") and t.get("ended_at_ts"):
                age = now - t["ended_at_ts"]
                if age >= keep_seconds:
                    to_remove.append(tid)
        for tid in to_remove:
            _tasks.pop(tid, None)
            _cancel_events.pop(tid, None)

def remove(task_id: str):
    """手动移除指定任务"""
    with _lock:
        _tasks.pop(task_id, None)
        _cancel_events.pop(task_id, None)
