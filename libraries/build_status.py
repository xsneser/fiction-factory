"""建书状态 — 浏览器向导状态 → 后端共享文件 → MCP 工具读取。

内置 agent（dsh）经 drive_ui 驱动建书向导时，`drive_ui(submit)` 非阻塞、agent 拿不到
建书结果。浏览器每次向导状态变化（切步 / 选候选 / 建书成功 / reset）把 `WZ` 的关键
状态写进 `storage/build_status.json`，agent 经 MCP 工具 `get_build_status` 读取——
知道当前步、书是否建成、book_id 等，用于 submit 后校验与进度感知。

与 nav_intent（消费型队列）不同：本文件是**持久状态快照**，重复读不消费。
"""
import os

from core.json_store import read_json, write_json_atomic  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATUS_PATH = os.path.join(_ROOT, "storage", "build_status.json")

_DEFAULTS = {
    "cur": 1,
    "book_id": "",
    "creating": False,
    "created": False,
    "_picked": False,
    "has_world": False,
    "has_picks": False,
    "updated_at": "",
}


def set_build_status(state: dict) -> None:
    """写建书状态快照（浏览器上报；空 state 视为清空）。

    浏览器 WZ 用驼峰字段（bookId/creating/_picked），这里映射到 `_DEFAULTS` 的下划线键。
    """
    data = dict(_DEFAULTS)
    if state:
        mapped = {}
        for k, v in state.items():
            if k == "bookId":
                mapped["book_id"] = v
            elif k in _DEFAULTS:
                mapped[k] = v
        data.update(mapped)
    data["created"] = bool(data.get("book_id"))
    write_json_atomic(_STATUS_PATH, data)


def get_build_status() -> dict:
    """读建书状态快照（agent 经 MCP 调用）；无记录返回默认（cur=1，未建成）。"""
    data = read_json(_STATUS_PATH, {}) or {}
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in data.items() if k in _DEFAULTS})
    out["created"] = bool(out.get("book_id"))
    return out
