"""JSON 文件存储工具。

提供进程内按文件加锁的 JSON 读写，以及“写临时文件 → 原子替换”的保存方式。
这样可以降低 Flask 多线程 / SSE 后台任务同时写入时把 JSON 写坏的风险。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_log = logging.getLogger("json_store")

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


@contextmanager
def process_file_lock(path: str | Path, timeout: float = 1.0) -> Iterator[None]:
    """跨进程文件锁（同机多进程互斥）：Windows msvcrt.locking / POSIX fcntl.flock。

    `json_store.file_lock` 只是进程内 RLock——MCP 进程与 Flask 进程会并发对同一
    storage/*.json 读-改-写（crawl_progress / hot_cache），需 OS 级排他。此锁用独立的
    `<path>.lock` 文件。短超时（默认 1s，避免拖住 Flask 单线程）；**抢不到即抛
    TimeoutError**（不静默无锁放行——那会复现要修的 RMW 竞态），调用方自行降级为
    进程内 file_lock 或跳过。
    """
    p = _resolved(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_file = p.with_name(p.name + ".lock")
    fd = lock_file.open("a+b")
    acquired = False
    try:
        fd.seek(0)
        if fd.read(1) == b"":
            fd.write(b"\x00")
            fd.flush()
        fd.seek(0)
        deadline = time.time() + timeout
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.time() > deadline:
                        break
                    time.sleep(0.03)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.time() > deadline:
                        break
                    time.sleep(0.03)
        if not acquired:
            # 短超时仍抢不到（另一进程持锁 >1s）：降级为进程内锁 + 明确告警，不静默无锁、
            # 不长时间拖住 Flask 单线程（尽力而为，多数瞬态竞争 1s 内即让出）。
            _log.warning("跨进程文件锁获取超时(%.0fs)，降级进程内锁(尽力而为): %s", timeout, lock_file)
            with file_lock(p):
                yield
            return
        yield
    finally:
        if acquired:
            try:
                fd.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fd.close()


def _replace_with_retry(src: Path, dst: Path, attempts: int = 6,
                        delay: float = 0.15) -> None:
    """os.replace 带瞬态重试：Windows 上目标被其他进程瞬态占用（杀软扫描/并发读）
    会抛 PermissionError/WinError 5，重试短等可越过；多次仍失败则抛出。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last = e
            time.sleep(delay * (i + 1))
    assert last is not None
    raise last


def read_json(path: str | Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在时返回 default。"""
    p = Path(path)
    with file_lock(p):
        if not p.exists():
            return default
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)


def write_json_atomic(path: str | Path, data: Any, *, indent: int = 2,
                      fsync: bool = True) -> None:
    """原子保存 JSON。

    临时文件创建在目标文件同目录，确保 os.replace 在同一文件系统内完成。
    fsync=False 用于可再生成的正文类（save_chapter 逐章），省每章一次磁盘 fsync；
    状态/进度/info 等权威小文件保持默认 True。
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
                if fsync:
                    os.fsync(f.fileno())
            _replace_with_retry(tmp_path, p)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            finally:
                raise


def read_jsonl(path: str | Path) -> list:
    """读取 JSONL（每行一个 JSON 对象）；文件不存在返回 []，跳过空行/坏行。"""
    p = Path(path)
    with file_lock(p):
        if not p.exists():
            return []
        items = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items


def write_jsonl_atomic(path: str | Path, items: list) -> None:
    """原子保存 JSONL（每行一个 JSON 对象，ensure_ascii=False，临时文件+os.replace）。"""
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
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp_path, p)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            finally:
                raise
