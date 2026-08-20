"""工具调用日志 — 进程内环形缓冲 + 跨进程 JSONL（外部 MCP 调用对 Web 可见）。

内置 agent 已删，外部 MCP 调用（source=mcp）写内存环形缓冲 + 追加
`storage/tool_log.jsonl`；Web 端 `get_tool_log` 合并两处按 ts 倒序展示。

日志写入尽力而为，异常静默降级，不阻塞主链路。JSONL 超长自动截断
（字节阈值快筛 + 行数上限，见 _trim_ext）。
"""
import json
import os
import threading
import time

_MAX_TOOL_LOG = 200
_EXT_LIMIT = 200

# JSONL 超长自动截断：文件超过 _MAX_EXT_BYTES 才读行数检查（O(1) 快筛），
# 行数超 _MAX_EXT_LINES 则截为最近 _KEEP_EXT_LINES 行，避免磁盘无限增长。
_MAX_EXT_BYTES = 256 * 1024       # 256KB
_MAX_EXT_LINES = 1000             # 超过此行数触发截断
_KEEP_EXT_LINES = 500             # 截断后保留行数

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
        _trim_ext()
    except Exception:
        pass


def _trim_ext() -> None:
    """JSONL 超长自动截断：超过 _MAX_EXT_BYTES 才读行数检查，超 _MAX_EXT_LINES 截为最近 _KEEP_EXT_LINES 行。

    用字节阈值做 O(1) 快筛，避免每次追加都全量读文件；异常静默降级（日志尽力而为）。
    """
    try:
        if os.path.getsize(_EXT_PATH) <= _MAX_EXT_BYTES:
            return
        with open(_EXT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_EXT_LINES:
            return
        with open(_EXT_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines[-_KEEP_EXT_LINES:])
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
