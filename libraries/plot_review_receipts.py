"""Critic 判决的**服务端签发凭据**（不变量 I1：判决来源不可伪造）。

为什么需要它：主 Agent 是 Critic 子代理的调用方。如果 accept 只是收一个 root 转交的
JSON，那么 root 完全可以自己拼一个 `{"verdict": "accept"}` 递进来——评审门禁在**能力
边界**上就是假的（服务端无法证明这个判决真的来自 Critic）。

因此判决的写入路径只有一个：**Critic profile 独有的 MCP 工具** `record_plot_review`
自己调服务端，服务端校验 `gate_digest` 与当前正文报告一致后签发 receipt；root 手里
**没有任何工具**能调它，只能拿着 receipt id 去 `accept_plot_draft`。accept 再复核
receipt 绑定的是不是**当前**的 plot_id + gate_digest。

失效语义天然成立、不需要额外失效逻辑：正文一改 → `plot_quality_gate` 重算出新的
`gate_digest` → 旧 receipt 的 digest 对不上 → 自动 stale。TTL 只是兜底。

账本惯例比照 `libraries/plot_revision_tokens.py`（签发 / 校验 / 一次性消费 / 有界回收）。
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from core.json_store import read_json, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
TTL_SECONDS = 30 * 60
VERDICTS = ("accept", "revise_text", "patch_character", "replan", "stop")
CONFIDENCE = ("high", "medium", "low")
MAX_RECORDS = 200


def _path(book_id: str) -> Path:
    return ROOT / "books" / book_id / "plot_review_receipts.json"


def _load(book_id: str) -> dict:
    path = _path(book_id)
    value = read_json(path) if path.exists() else None
    return value if isinstance(value, dict) else {"schema_version": 1, "receipts": {}}


def _save(book_id: str, value: dict) -> None:
    value["schema_version"] = 1
    write_json_atomic(_path(book_id), value)


def _sweep(state: dict, now: float) -> None:
    """有界回收：过期且未消费的（无用）直接删；**已消费的留下作审计**，只受条数上限约束。"""
    receipts = state.setdefault("receipts", {})
    for rid, old in list(receipts.items()):
        if not isinstance(old, dict):
            receipts.pop(rid, None)
            continue
        expired = float(old.get("expires_at") or 0) < now
        if expired and not old.get("consumed"):
            receipts.pop(rid, None)
    if len(receipts) > MAX_RECORDS:
        ordered = sorted(receipts.items(), key=lambda kv: float((kv[1] or {}).get("issued_at") or 0))
        for rid, _ in ordered[:len(receipts) - MAX_RECORDS]:
            receipts.pop(rid, None)


def issue(book_id: str, plot_id: str, gate_digest: str, payload: dict,
          critic_run_id: str = "") -> str:
    """签发 receipt。调用方（record_plot_review）负责先校验 digest 与 verdict 合法性。"""
    state = _load(book_id)
    now = time.time()
    _sweep(state, now)
    receipt_id = "rr_" + secrets.token_urlsafe(24)
    record = dict(payload or {})
    record.update({"receipt_id": receipt_id, "plot_id": str(plot_id or ""),
                   "gate_digest": str(gate_digest or ""), "critic_run_id": str(critic_run_id or ""),
                   "issued_at": now, "expires_at": now + TTL_SECONDS, "consumed": False})
    state.setdefault("receipts", {})[receipt_id] = record
    _save(book_id, state)
    return receipt_id


def load(book_id: str, receipt_id: str) -> dict | None:
    record = (_load(book_id).get("receipts") or {}).get(str(receipt_id or ""))
    return record if isinstance(record, dict) else None


def latest_for_plot(book_id: str, plot_id: str) -> dict | None:
    """该 Plot 最新一枚未消费且未过期的 receipt（供状态读取；**纯读不写盘**）。"""
    now = time.time()
    rows = [r for r in (_load(book_id).get("receipts") or {}).values()
            if isinstance(r, dict) and str(r.get("plot_id") or "") == str(plot_id or "")
            and not r.get("consumed") and float(r.get("expires_at") or 0) >= now]
    if not rows:
        return None
    return max(rows, key=lambda r: float(r.get("issued_at") or 0))


def verify(book_id: str, receipt_id: str, plot_id: str, gate_digest: str) -> dict:
    """校验 receipt 绑定的是当前 Plot 与当前正文报告；不通过就抛（消息可行动）。"""
    record = load(book_id, receipt_id)
    if not record:
        raise RuntimeError("review_receipt 无效；请让 Critic 重新调用 record_plot_review")
    if record.get("consumed"):
        raise RuntimeError("review_receipt 已使用；请让 Critic 重新评审当前 Plot")
    if float(record.get("expires_at") or 0) < time.time():
        raise RuntimeError("review_receipt 已过期；请让 Critic 重新评审当前 Plot")
    if str(record.get("plot_id") or "") != str(plot_id or ""):
        raise RuntimeError("review_receipt 不是针对当前 Plot 签发的；请重新评审")
    if str(record.get("gate_digest") or "") != str(gate_digest or ""):
        raise RuntimeError("正文已变化，review_receipt 已失效；请重新体检并让 Critic 重新评审")
    return record


def consume(book_id: str, receipt_id: str, result: dict | None = None) -> None:
    state = _load(book_id)
    record = (state.get("receipts") or {}).get(str(receipt_id or ""))
    if not isinstance(record, dict):
        return
    record["consumed"] = True
    record["consumed_at"] = time.time()
    record["result"] = result or {}
    _save(book_id, state)
