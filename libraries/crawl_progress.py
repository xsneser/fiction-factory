"""小说抓取进度 — 跨进程共享状态文件。

MCP 工具（fetch_novel，跑在 MCP 进程）与 Web 表单抓取（/api/scout/run，跑在 Flask 进程）
共用同一进度源：谁在抓取就往 storage/crawl_progress.json 写快照，/scout 页轮询
`GET /api/crawl/progress` 实时展示。镜像 storage/build_status.json 的「非消费快照」协调模式。

state ∈ running / done / error。
"""
import os
import time

from core.json_store import read_json, write_json_atomic

_PATH = os.path.join("storage", "crawl_progress.json")


def write_crawl_progress(state: str, phase: str = "", current: int = 0,
                         total: int = 0, message: str = "", extra: dict = None) -> dict:
    """写一条抓取进度快照（原子替换），返回写入的 dict。"""
    data = {
        "state": state,
        "phase": phase,
        "current": current,
        "total": total,
        "message": message or "",
        "ts": time.time(),
    }
    if extra:
        data["extra"] = extra
    write_json_atomic(_PATH, data)
    return data


def read_crawl_progress() -> dict:
    """读当前进度快照（无记录返回空 dict）。"""
    return read_json(_PATH, {}) or {}
