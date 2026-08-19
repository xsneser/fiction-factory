"""工具调用日志 — 进程内环形缓冲 + 跨进程 JSONL（外部 MCP 调用对 Web 可见）。

内部 agent_loop 调用（source=web）写内存环形缓冲（会话级，重启即清）；
外部 MCP 调用（source=mcp）另追加 `storage/tool_log.jsonl`；Web 端
`get_tool_log` 合并两处按 ts 倒序展示（设计文档 §1.4）。

日志写入尽力而为，异常静默降级，不阻塞主链路。
"""
import json
import os
import threading
import time

_MAX_TOOL_LOG = 200
_EXT_LIMIT = 200

_TOOL_LOG: list = []
_TOOL_LOCK = threading.Lock()
_EXT_PATH = os.path.join("storage", "tool_log.jsonl")


def log_tool_call(entry: dict) -> None:
    entry = dict(entry or {})
    if not entry.get("ts"):
        entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not entry.get("time"):
        entry["time"] = entry["ts"][11:19]
    with _TOOL_LOCK:
        _TOOL_LOG.append(entry)
        if len(_TOOL_LOG) > _MAX_TOOL_LOG:
            del _TOOL_LOG[:len(_TOOL_LOG) - _MAX_TOOL_LOG]
    if entry.get("source") == "mcp":
        _append_ext(entry)


def _append_ext(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_EXT_PATH) or ".", exist_ok=True)
        with open(_EXT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_ext(limit: int = _EXT_LIMIT) -> list:
    try:
        with open(_EXT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        items = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
        return items
    except Exception:
        return []


def get_tool_log(limit: int = _MAX_TOOL_LOG) -> list:
    """合并内存环形缓冲 + 外部 JSONL，按 ts 倒序返回最近 limit 条。"""
    with _TOOL_LOCK:
        merged = list(_TOOL_LOG)
    merged.extend(_read_ext())
    merged.sort(key=lambda e: (e.get("ts") or ""), reverse=True)
    return merged[:limit]


def clear_tool_log() -> None:
    with _TOOL_LOCK:
        _TOOL_LOG.clear()
    try:
        with open(_EXT_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
