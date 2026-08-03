"""JSON 文件存储工具。

提供进程内按文件加锁的 JSON 读写，以及“写临时文件 → 原子替换”的保存方式。
这样可以降低 Flask 多线程 / SSE 后台任务同时写入时把 JSON 写坏的风险。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _lock_for(path: str | Path) -> threading.RLock:
    resolved = _resolved(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[resolved] = lock
        return lock


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """获取指定文件的进程内锁。"""
    lock = _lock_for(path)
    with lock:
        yield


def read_json(path: str | Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在时返回 default。"""
    p = Path(path)
    with file_lock(p):
        if not p.exists():
            return default
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)


def write_json_atomic(path: str | Path, data: Any, *, indent: int = 2) -> None:
    """原子保存 JSON。

    临时文件创建在目标文件同目录，确保 os.replace 在同一文件系统内完成。
    """
    p = Path(path)
    with file_lock(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{p.name}.",
            suffix=".tmp",
            dir=str(p.parent),
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, p)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            finally:
                raise
