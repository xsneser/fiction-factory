"""书级快照 + diff 预览 + 回滚（最小可用版）——决策点落库前的「commit」语义。

竞品移植：OpenNovel「AI 提议，人类批准」的 diff 审查 + 全量回滚（系统层）。
用法：
  · 写类工具经 agent_tools._wrap_book_lock 在落库前自动 snapshot（保留最近 N 份）
  · MCP 面：preview_diff / rollback_book / list_snapshots（只读预览 + 手动回滚，
    不引入阻塞式人审；agent 可自主决定 rollback）
"""
import difflib
import shutil
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BOOKS_ROOT = _ROOT / "books"
SNAPSHOT_ROOT = _ROOT / "storage" / "snapshots"
MAX_SNAPSHOTS = 10


def _book_dir(book_id: str) -> Path:
    return _BOOKS_ROOT / book_id


def _snapshot_dir(book_id: str, snapshot_id: str) -> Path:
    return SNAPSHOT_ROOT / book_id / snapshot_id


def snapshot(book_id: str, tool: str = "") -> str:
    """把书目录整体拷贝为快照，返回 snapshot_id（保留最近 MAX_SNAPSHOTS 份）。"""
    src = _book_dir(book_id)
    if not src.is_dir():
        return ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sid = f"{ts}_{(tool or 'tool').replace('/', '_')}"
    dst = _snapshot_dir(book_id, sid)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _prune(book_id)
    return sid


def _prune(book_id: str) -> None:
    root = SNAPSHOT_ROOT / book_id
    if not root.is_dir():
        return
    snaps = sorted(p for p in root.iterdir() if p.is_dir())
    for old in snaps[:-MAX_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)


def list_snapshots(book_id: str) -> list:
    """列出某书的全部快照（新→旧）。"""
    root = SNAPSHOT_ROOT / book_id
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir(), reverse=True):
        if p.is_dir():
            out.append({"snapshot_id": p.name, "tool": p.name.split("_", 2)[-1]})
    return out


def preview_diff(book_id: str, snapshot_id: str, max_files: int = 10) -> dict:
    """当前书目录 vs 快照的 unified diff（逐文件，每份 diff 截断）。"""
    snap = _snapshot_dir(book_id, snapshot_id)
    cur = _book_dir(book_id)
    if not snap.is_dir():
        return {"error": f"快照不存在: {snapshot_id}"}
    diffs = []
    files = [f for f in sorted(snap.rglob("*")) if f.is_file()][:max_files]
    for f in files:
        rel = f.relative_to(snap)
        cur_f = cur / rel
        old = f.read_text(encoding="utf-8", errors="replace")
        new = cur_f.read_text(encoding="utf-8", errors="replace") if cur_f.exists() else ""
        if old == new:
            continue
        udiff = difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"{snapshot_id}/{rel}", tofile=f"now/{rel}", lineterm="")
        diffs.append({"file": str(rel), "diff": "\n".join(udiff)[:2000]})
    return {"snapshot_id": snapshot_id, "files_changed": len(diffs), "diffs": diffs}


def rollback(book_id: str, snapshot_id: str) -> dict:
    """把快照内容全量恢复回书目录（先清后拷，恢复至快照时刻状态）。"""
    snap = _snapshot_dir(book_id, snapshot_id)
    cur = _book_dir(book_id)
    if not snap.is_dir():
        return {"ok": False, "error": f"快照不存在: {snapshot_id}"}
    if not cur.is_dir():
        return {"ok": False, "error": f"书目录不存在: {book_id}"}
    for child in cur.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink()
    shutil.copytree(snap, cur, dirs_exist_ok=True)
    return {"ok": True, "snapshot_id": snapshot_id}
