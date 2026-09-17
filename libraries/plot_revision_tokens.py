"""Single-use tokens for revising the current draft Plot after review."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from core.json_store import read_json, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
TTL_SECONDS = 20 * 60


def _path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "plot_revision_tokens.json"


def _load(book_id: str) -> dict:
    path = _path(book_id)
    value = read_json(path) if path.exists() else None
    return value if isinstance(value, dict) else {"schema_version": 1, "tokens": {}}


def _save(book_id: str, value: dict) -> None:
    value["schema_version"] = 1
    write_json_atomic(_path(book_id), value)


def issue(book_id: str, record: dict) -> str:
    state = _load(book_id)
    now = time.time()
    tokens = state.setdefault("tokens", {})
    for token, old in list(tokens.items()):
        if not isinstance(old, dict) or float(old.get("expires_at") or 0) < now:
            tokens.pop(token, None)
    token = "pr_" + secrets.token_urlsafe(24)
    tokens[token] = {**record, "issued_at": now, "expires_at": now + TTL_SECONDS,
                     "accepted": False}
    _save(book_id, state)
    return token


def resolve(token: str) -> tuple[str, dict]:
    token = str(token or "")
    if not token.startswith("pr_"):
        return "", {}
    for book_dir in (ROOT / "books").iterdir() if (ROOT / "books").is_dir() else ():
        if not book_dir.is_dir():
            continue
        state = _load(book_dir.name)
        record = (state.get("tokens") or {}).get(token)
        if isinstance(record, dict):
            return book_dir.name, record
    return "", {}


def verify(book_id: str, token: str) -> dict:
    state = _load(book_id)
    record = (state.get("tokens") or {}).get(str(token or ""))
    if not isinstance(record, dict):
        raise RuntimeError("revision_token 无效；请重新 prepare_plot_revision")
    if record.get("accepted"):
        raise RuntimeError("revision_token 已使用；请重新 prepare_plot_revision")
    if float(record.get("expires_at") or 0) < time.time():
        raise RuntimeError("revision_token 已过期；请重新 prepare_plot_revision")
    return record


def accept(book_id: str, token: str, result: dict) -> None:
    state = _load(book_id)
    record = verify(book_id, token)
    record["accepted"] = True
    record["accepted_at"] = time.time()
    record["result"] = result
    state.setdefault("tokens", {})[token] = record
    _save(book_id, state)
