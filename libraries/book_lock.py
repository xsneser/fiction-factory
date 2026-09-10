"""书级文件锁 — Web / MCP 双进程同书互斥。

进程内：每本书一个 threading.Lock（Flask 多线程 / SSE 后台任务并发写同一本书）。
进程间：`books/<id>/.lock` 文件首字节锁（Windows msvcrt.locking / POSIX fcntl.flock）。
锁文件写入 {pid, ts, purpose} 元信息，**不删除锁文件**（避免 TOCTOU 删除竞态）。

**同线程可重入**：同一线程对同一本书重复 acquire 只增计数、不重新加锁，release 到 0 才真正
解锁。这是必需的——写工具链存在天然的嵌套（如 commit_replan_preview 持锁 → save_outlines →
_save_storyline 再取同一把锁），非重入锁会让这种调用直接自锁到超时（表现为「另一进程正在
操作这本书」，非常难排查）。

读操作不加锁；写文件继续走 `write_json_atomic`（防半写）。
"""
import json
import os
import threading
import time
from pathlib import Path

_BOOK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "books")
_LOCK_TIMEOUT_S = 30.0

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCAL = threading.local()          # 线程内重入深度：{book_id: depth}


def _depth_map() -> dict:
    depths = getattr(_LOCAL, "depth", None)
    if depths is None:
        depths = {}
        _LOCAL.depth = depths
    return depths


class BookBusyError(RuntimeError):
    """另一进程正在操作这本书。"""


def _thread_lock_for(book_id: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(book_id)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[book_id] = lock
        return lock


def _acquire_file_lock(fh, timeout: float) -> bool:
    """对 fh 首字节做非阻塞互斥锁；失败轮询直到超时。"""
    deadline = time.time() + timeout
    while True:
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


def _release_file_lock(fh) -> None:
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        pass


class BookLock:
    """对 `books/<id>/.lock` 加锁。上下文管理器 / acquire+release 均可。"""

    def __init__(self, book_id: str):
        self.book_id = book_id
        self.path = os.path.join(_BOOK_DIR, book_id, ".lock")
        self._thread_lock = _thread_lock_for(book_id)
        self._fh = None

    def acquire(self, timeout: float = _LOCK_TIMEOUT_S, purpose: str = "write") -> bool:
        depths = _depth_map()
        if depths.get(self.book_id, 0) > 0:
            depths[self.book_id] += 1        # 同线程重入：外层已持锁，直接放行
            return True
        if not self._thread_lock.acquire(timeout=timeout):
            return False
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+", encoding="utf-8")
            if not _acquire_file_lock(fh, timeout):
                fh.close()
                self._thread_lock.release()
                return False
            self._fh = fh
            depths[self.book_id] = 1
            # 写元信息（锁内；文件不删除）
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({
                    "pid": os.getpid(),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "purpose": purpose,
                }, ensure_ascii=False))
                fh.flush()
            except Exception:
                pass
            return True
        except Exception:
            self._thread_lock.release()
            return False

    def release(self) -> None:
        depths = _depth_map()
        depth = depths.get(self.book_id, 0)
        if depth <= 0:
            return                            # 未持有（或已被外层释放）：幂等忽略
        if depth > 1:
            depths[self.book_id] = depth - 1  # 内层退出：只减计数
            return
        depths.pop(self.book_id, None)
        if self._fh is not None:
            _release_file_lock(self._fh)
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._thread_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
