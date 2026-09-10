#!/usr/bin/env python3
"""Prepared Plot token retry and crash-window regression tests."""
from pathlib import Path
import sys
import tempfile
import time

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries import plot_commit_tokens as tokens  # noqa: E402


def main():
    original_path = tokens._path
    original_iter = tokens._iter_book_ids
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tokens._path = lambda book_id: Path(tmp) / f"{book_id}.json"
            tokens._iter_book_ids = lambda: ["book-test", "book-other"]
            token = tokens.issue(
                book_id="book-test", flow_id="flow-a", child_run_id="child-a",
                plot_id="p1", storyline_revision=3, context_fingerprint="final",
                prepared_key="base", prepared_snapshot={"run": {"id": "p1@3"}},
            )
            found = tokens.find_prepared(
                book_id="book-test", flow_id="flow-a", plot_id="p1",
                storyline_revision=3, prepared_key="base",
            )
            assert found and found[0] == token
            assert found[1]["prepared_snapshot"]["run"]["commit_token"] == token
            assert tokens.issue(
                book_id="book-test", flow_id="flow-a", child_run_id="child-b",
                plot_id="p1", storyline_revision=3, context_fingerprint="final",
                prepared_key="base",
            ) == token
            print("[OK] prepared Plot token retry and crash recovery")

            # ── 账本必须有界：过期未使用/吊销的记录被回收，accepted 压缩 + 限额 ──
            state = tokens._load("book-test")
            now = time.time()
            state["tokens"]["expired"] = {"plot_id": "px", "expires_at": now - 1, "accepted": False}
            state["tokens"]["revoked"] = {"plot_id": "py", "expires_at": now + 999, "revoked": True}
            big = "x" * 5000
            for i in range(tokens.ACCEPTED_KEEP_MAX + 5):
                state["tokens"][f"acc{i}"] = {"plot_id": f"acc{i}", "accepted": True,
                                             "accepted_at": now - i, "result": {"ok": True},
                                             "prepared_snapshot": {"blob": big}}
            tokens._prune_tokens(state, now)
            assert "expired" not in state["tokens"] and "revoked" not in state["tokens"]
            accepted = [t for t, r in state["tokens"].items() if r.get("accepted")]
            assert len(accepted) == tokens.ACCEPTED_KEEP_MAX, len(accepted)
            assert all("prepared_snapshot" not in state["tokens"][t] for t in accepted), "accepted 应丢弃大快照"
            assert "acc0" in accepted, "保留窗口内应留最近的 accepted（幂等重试）"
            assert token in state["tokens"], "未提交的在途令牌不能被回收"
            print(f"[OK] 账本回收：过期/吊销清理 + accepted 压缩并限 {tokens.ACCEPTED_KEEP_MAX} 条")

            # ── O(1) 定位：有提示只查这本书；提示不符 fail-closed；无提示才全库回退 ──
            other = tokens.issue(book_id="book-other", flow_id="flow-b", child_run_id="child-c",
                                 plot_id="p9", storyline_revision=1, context_fingerprint="fp",
                                 prepared_snapshot={"run": {"id": "p9@1"}})
            assert tokens.resolve_book_id(token, "book-test") == "book-test"
            assert tokens.resolve_book_id(token, "book-other") == "", "提示不符必须 fail-closed"
            assert tokens.resolve_book_id(token) == "book-test", "无提示时回退全库扫描"
            assert tokens.resolve_book_id(other) == "book-other"
            assert tokens.resolve_book_id("不存在的令牌") == ""
            print("[OK] commit_token → 归属书：提示 O(1) / 不符 fail-closed / 无提示回退扫描")

            # ── accepted 幂等重试仍可用（压缩快照后）──
            tokens.accept("book-test", token, {"ok": True, "chapter": 1})
            assert tokens.accepted_result("book-test", token) == {"ok": True, "chapter": 1}
            after = tokens._load("book-test")["tokens"][token]
            assert after["accepted"] and "prepared_snapshot" not in after, after.keys()
            assert tokens.resolve_book_id(token, "book-test") == "book-test", "accepted 记录仍能定位"
            print("[OK] accepted 幂等重试在压缩后仍可用")
    finally:
        tokens._path = original_path
        tokens._iter_book_ids = original_iter


if __name__ == "__main__":
    main()
