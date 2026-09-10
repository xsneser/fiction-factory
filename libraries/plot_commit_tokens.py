"""服务端签发的 Plot 提交令牌。

令牌把一次 prepare 所见的 plot、故事线版本、上下文快照和样文回执绑定在
服务端。Writer 只需带回令牌和语义输出，不能伪造或复用旧上下文。

账本按书分片存放，且**必须有界**：每条记录携带 7–11 KB 的 `prepared_snapshot`，
早期实现从不回收，一本书的账本会随情节段数线性膨胀（实测 7 条 = 175 KB），
而每次签发/提交都要整份读写。因此：
  - 已接受(accepted)记录只保留结果与身份字段（丢弃大快照）并设保留窗口；
  - 过期未使用、已吊销(revoked)的记录在下次写入时清掉。
读取路径（find_prepared/verify/_active_record）保持无副作用、不做回收。
"""
from __future__ import annotations

import copy
import secrets
import time
from pathlib import Path

from core.json_store import read_json, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TTL_SECONDS = 20 * 60
# 幂等重试窗口：accepted 记录保留多久（网络重试/断线重连要能拿到同一结果）。
ACCEPTED_KEEP_SECONDS = 24 * 60 * 60
# accepted 记录上限（超出按 accepted_at 从旧到新丢），防止长跑书无限增长。
ACCEPTED_KEEP_MAX = 32


def _path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "plot_commit_tokens.json"


def _load(book_id: str) -> dict:
    raw = read_json(_path(book_id)) if _path(book_id).exists() else None
    return raw if isinstance(raw, dict) else {"schema_version": 1, "tokens": {}}


def _save(book_id: str, payload: dict) -> None:
    payload = copy.deepcopy(payload)
    payload["schema_version"] = 1
    write_json_atomic(_path(book_id), payload)


def _prune_tokens(state: dict, now: float | None = None) -> None:
    """就地回收账本：过期未使用/已吊销全删；accepted 压缩 + 限额 + 保留窗口。

    只在写路径调用（issue/accept），保证读取路径无副作用。
    """
    now = time.time() if now is None else now
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        state["tokens"] = {}
        return
    keep: dict = {}
    accepted: list[tuple[float, str]] = []
    for token, record in tokens.items():
        if not isinstance(record, dict):
            continue
        if record.get("accepted"):
            record.pop("prepared_snapshot", None)      # 大快照不再需要（结果已存 result）
            keep[token] = record
            accepted.append((float(record.get("accepted_at") or 0), token))
            continue
        if record.get("revoked"):
            continue
        if float(record.get("expires_at") or 0) < now:  # 过期且未被使用
            continue
        keep[token] = record
    cutoff = now - ACCEPTED_KEEP_SECONDS
    stale = [t for at, t in sorted(accepted, reverse=True) if at < cutoff]
    for token in stale:
        keep.pop(token, None)
    fresh = [t for _, t in sorted(accepted, reverse=True) if t in keep]
    for token in fresh[ACCEPTED_KEEP_MAX:]:
        keep.pop(token, None)
    state["tokens"] = keep


def _iter_book_ids() -> list[str]:
    """全库回退扫描用：books/ 下的书目录名（单测可 patch 成扁平布局）。"""
    books_dir = ROOT / "books"
    if not books_dir.is_dir():
        return []
    return [c.name for c in books_dir.iterdir() if c.is_dir()]


def resolve_book_id(commit_token: str, hinted_book_id: str = "") -> str:
    """把 commit_token 解析成归属书。

    Writer 子进程不带书号（工具面只有 commit_token），但 FSM 会给子进程注入
    `NOVEL_WRITE_BOOK_ID`。给了提示就**只查这本书**（O(1)）；提示与账本不符时
    直接失败（fail-closed）——那说明子进程上下文错了，绝不该去翻别的书。
    没有提示（手工调用/测试）才回退到全库扫描。
    """
    token = str(commit_token or "")
    if not token:
        return ""
    hint = str(hinted_book_id or "").strip()
    if hint:
        try:
            if token in (_load(hint).get("tokens") or {}):
                return hint
        except (OSError, ValueError):
            return ""
        return ""
    for book_id in _iter_book_ids():
        try:
            if token in (_load(book_id).get("tokens") or {}):
                return book_id
        except (OSError, ValueError):
            continue
    return ""


def _active_record(state: dict, *, flow_id: str, plot_id: str,
                   storyline_revision: int, prepared_key: str = "") -> tuple[str, dict] | None:
    """Find an uncommitted prepared snapshot without running any resolver."""
    now = time.time()
    expected_flow = flow_id or "adhoc"
    rows = sorted((state.get("tokens") or {}).items(),
                  key=lambda item: float(item[1].get("issued_at") or 0), reverse=True)
    for token, record in rows:
        if record.get("accepted") or record.get("revoked"):
            continue
        if float(record.get("expires_at") or 0) < now:
            continue
        if record.get("flow_id") != expected_flow or record.get("plot_id") != plot_id:
            continue
        if int(record.get("storyline_revision") or 0) != int(storyline_revision):
            continue
        if prepared_key and record.get("prepared_key") != prepared_key:
            continue
        if isinstance(record.get("prepared_snapshot"), dict):
            found = copy.deepcopy(record)
            snapshot = found["prepared_snapshot"]
            # A crash between issue and attach_snapshot must still return a
            # usable token to the retrying Writer.
            if isinstance(snapshot.get("run"), dict):
                snapshot["run"]["commit_token"] = token
            return token, found
    return None


def find_prepared(*, book_id: str, flow_id: str, plot_id: str,
                  storyline_revision: int, prepared_key: str = "") -> tuple[str, dict] | None:
    """Return the existing prepared Plot snapshot, if it is still resumable."""
    return _active_record(_load(book_id), flow_id=flow_id, plot_id=plot_id,
                          storyline_revision=storyline_revision, prepared_key=prepared_key)


def issue(*, book_id: str, flow_id: str, child_run_id: str, plot_id: str,
          storyline_revision: int, context_fingerprint: str,
          sample_receipt: dict | None = None, ttl_seconds: int | None = None,
          prepared_key: str = "", prepared_snapshot: dict | None = None,
          version_vector: dict | None = None) -> str:
    """签发令牌；同一未提交 prepared key 复用原令牌和快照。"""
    now = time.time()
    state = _load(book_id)
    if prepared_key:
        existing = _active_record(state, flow_id=flow_id, plot_id=plot_id,
                                  storyline_revision=storyline_revision,
                                  prepared_key=prepared_key)
        if existing:
            return existing[0]
    for record in (state.get("tokens") or {}).values():
        if (record.get("plot_id") == plot_id and
                int(record.get("storyline_revision") or 0) == int(storyline_revision) and
                not record.get("accepted") and not record.get("revoked")):
            record["revoked"] = True
    token = secrets.token_urlsafe(32)
    snapshot = copy.deepcopy(prepared_snapshot) if isinstance(prepared_snapshot, dict) else None
    if isinstance(snapshot, dict):
        # 令牌在签发时即写进快照：调用方不必再 attach 一次（少一次全账本重写），
        # 崩溃在 issue 与 attach 之间也能重试（快照自含 commit_token）。
        snapshot.setdefault("run", {})
        if isinstance(snapshot["run"], dict):
            snapshot["run"]["commit_token"] = token
    state.setdefault("tokens", {})[token] = {
        "flow_id": flow_id or "adhoc",
        "child_run_id": child_run_id or f"plot:{plot_id}",
        "plot_id": plot_id,
        "storyline_revision": int(storyline_revision or 0),
        "context_fingerprint": context_fingerprint,
        "prepared_key": prepared_key or context_fingerprint,
        "version_vector": copy.deepcopy(version_vector or {}),
        "sample_receipt": copy.deepcopy(sample_receipt or {}),
        "prepared_snapshot": snapshot,
        "issued_at": now,
        "expires_at": now + int(ttl_seconds or DEFAULT_TTL_SECONDS),
        "accepted": False,
        "revoked": False,
    }
    _prune_tokens(state, now)
    _save(book_id, state)
    return token


def attach_snapshot(book_id: str, token: str, snapshot: dict) -> None:
    """Persist the exact Writer-facing preparation after token issuance."""
    state = _load(book_id)
    record = (state.get("tokens") or {}).get(token)
    if not record or record.get("accepted") or record.get("revoked"):
        raise ValueError("commit_token 无效或已失效")
    record["prepared_snapshot"] = copy.deepcopy(snapshot)
    _save(book_id, state)


def verify(book_id: str, token: str, *, plot_id: str, storyline_revision: int,
           context_fingerprint: str | None = None) -> dict:
    state = _load(book_id)
    record = state["tokens"].get(token)
    if not record:
        raise ValueError("commit_token 无效；请重新 prepare")
    if record.get("revoked") or float(record.get("expires_at") or 0) < time.time():
        raise ValueError("commit_token 已失效；请重新 prepare")
    if record.get("plot_id") != plot_id or int(record.get("storyline_revision") or 0) != int(storyline_revision):
        raise ValueError("commit_token 与当前 Plot 或故事线版本不匹配")
    if context_fingerprint is not None and record.get("context_fingerprint") != context_fingerprint:
        raise ValueError("commit_token 上下文已失效；请重新 prepare")
    return copy.deepcopy(record)


def accept(book_id: str, token: str, result: dict) -> None:
    state = _load(book_id)
    record = state["tokens"].get(token)
    if not record:
        raise ValueError("commit_token 无效")
    record["accepted"] = True
    record["accepted_at"] = time.time()
    record["result"] = copy.deepcopy(result)
    record.pop("prepared_snapshot", None)   # 结果已存 result，大快照不再需要
    _prune_tokens(state)
    _save(book_id, state)


def accepted_result(book_id: str, token: str) -> dict | None:
    record = _load(book_id).get("tokens", {}).get(token) or {}
    return copy.deepcopy(record.get("result")) if record.get("accepted") else None
