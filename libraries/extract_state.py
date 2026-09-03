"""提取工作状态 —「整本扫读」式外部书目提取的断点/记忆存档。

novel-scout 的整本顺序通读（v2）由 agent 分多「段」完成：dsh 会话内无上下文压缩、
单会话装不下整本长书，因此每段是一个新会话——agent 读完自定窗口后把
「压缩记忆(digest) + 游标(cursor) + 已入库名称(committed)」落盘到这里
（`storage/extract_work/<folder>.json`），下一段 `load` 后从断点续读，
**只带 digest、不带旧章原文**。这就是「总结前段 + 记忆压缩 + 继续读」的物理形态。

与 nav_intent（消费型队列，TTL 30s）不同：本文件是**持久状态快照**，load 重复读不消费；
save 整体覆盖。每本书一个文件，folder = 书目录名（唯一路径 key）。
"""
import os

from core.json_store import read_json, write_json_atomic

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORK_DIR = os.path.join(_ROOT, "storage", "extract_work")

# 状态默认骨架：save 只覆盖给定键，缺省补默认。
_DEFAULTS = {
    "book": {},                     # {title, folder, chapter_count}
    "cursor": 0,                    # 已顺序读到第 N 章（下一段从 N+1 起）
    "status": "running",            # running | done | paused
    "memory": {                     # = 压缩记忆（新会话续读只带它）
        "digest": "",               #   前段全部已读内容的精炼梗概（段间由 agent 合并）
        "open_segments": [],        #   [{"goal","from_ch","note"}] 尚未收束的弧开口/悬念
        "people": [],               #   [{"name","note"}] 已出场关键人物
        "unresolved": [],           #   ["…线索…"]
    },
    "committed": {                  # 已入库名称（去重汇报用；四库只列名）
        "plots": [], "structures": [], "gags": [], "characters": [],
    },
    "style_rules_profile": "",      # 风格规则归属笔名（未指定默认「枫落」）
    "segments_log": [],             # ["第1-150章：…"] 轻量进度日志
    "updated_at": "",
}


def _safe_path(folder: str) -> str:
    """folder → 合法文件名（拒绝路径分隔/相对路径穿越）。"""
    if not folder:
        raise ValueError("folder 为空")
    if folder in (".", "..") or "/" in folder or "\\" in folder:
        raise ValueError(f"非法 folder: {folder!r}")
    return os.path.join(_WORK_DIR, folder + ".json")


def _path(folder: str) -> str:
    os.makedirs(_WORK_DIR, exist_ok=True)
    return _safe_path(folder)


def _normalize(state: dict) -> dict:
    """把外部传入 state 合到默认骨架（去未知键），确保落盘形状稳定。"""
    data = {}
    for k, dv in _DEFAULTS.items():
        v = state.get(k, dv) if isinstance(state, dict) else dv
        if v is None:
            v = dv
        data[k] = v
    return data


def save_extract_state(folder: str, state: dict | None = None) -> dict:
    """写一本书的整本扫读状态快照（整体覆盖；空 state 视为重置为默认 running）。

    返回 {ok, folder, cursor, status, updated_at} 供 agent 确认落盘。
    """
    data = _normalize(state or {})
    if state:  # 真实扫读上报才盖章；空 state（清理）不盖
        import time
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = _path(folder)
    write_json_atomic(path, data)
    return {"ok": True, "folder": folder, "cursor": data.get("cursor", 0),
            "status": data.get("status", "running"), "updated_at": data.get("updated_at", "")}


def load_extract_state(folder: str, mode: str = "summary") -> dict:
    """读书的扫读状态。

    mode=summary（默认）：只回 {book, cursor, status, style_rules_profile, memory,
    committed_counts, committed_names}——精简、防 dsh 8KB 工具结果裁剪，续读只需这些。
    mode=full：回全部字段（含 segments_log、原始 committed 数组等）。
    无记录：返回默认骨架 + exists=False。
    """
    path = _path(folder)
    raw = read_json(path, {}) or {}
    data = _normalize(raw)
    if mode == "full":
        data["exists"] = bool(raw)
        return data
    mem = data.get("memory") or {}
    comm = data.get("committed") or {}
    return {
        "ok": True, "folder": folder, "exists": bool(raw),
        "book": data.get("book", {}), "cursor": data.get("cursor", 0),
        "status": data.get("status", "running"),
        "style_rules_profile": data.get("style_rules_profile", ""),
        "memory": mem,
        "committed_counts": {k: len(v or []) for k, v in comm.items()},
        "committed_names": {k: v or [] for k, v in comm.items()},
    }


def clear_extract_state(folder: str) -> dict:
    """删除一本书的扫读状态（重新扫读 / 归档）。"""
    path = _path(folder)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return {"ok": True, "cleared": folder}
