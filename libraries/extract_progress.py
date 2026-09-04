"""外部书目提取实时进度（供 dsh 工具事件与 /extract 页面共享）。"""
import os
import time

from core.json_store import read_json, write_json_atomic

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "storage", "extract_progress.json")

_DEFAULT = {
    "active": False,
    "folder": "",
    "title": "",
    "status": "idle",
    "read_start": 0,
    "read_end": 0,
    "cursor": 0,
    "staged": {"plots": 0, "structures": 0, "gags": 0, "characters": 0},
    "committed": {"plots": 0, "structures": 0, "gags": 0, "characters": 0},
    "review": {},
    "last_tool": "",
    "message": "",
    "updated_at": 0,
}


def _snapshot() -> dict:
    data = read_json(_PATH, {}) or {}
    result = dict(_DEFAULT)
    result.update(data)
    for key in ("staged", "committed"):
        merged = dict(_DEFAULT[key])
        merged.update(data.get(key) or {})
        result[key] = merged
    result["review"] = data.get("review") or {}
    return result


def read_extract_progress() -> dict:
    return _snapshot()


def update_extract_progress(folder: str = "", title: str = "", **changes) -> dict:
    data = _snapshot()
    if folder:
        data["folder"] = folder
    if title:
        data["title"] = title
    for key, value in changes.items():
        if key in ("staged", "committed") and isinstance(value, dict):
            data[key].update({k: int(v or 0) for k, v in value.items()})
        elif key in data:
            data[key] = value
    data["updated_at"] = time.time()
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    write_json_atomic(_PATH, data)
    return data


def clear_extract_progress() -> None:
    update_extract_progress(active=False, status="idle", message="")


def clear_review(folder: str = "") -> dict:
    data = update_extract_progress(folder=folder, review={}, staged={
        "plots": 0, "structures": 0, "gags": 0, "characters": 0,
    })
    return data
