#!/usr/bin/env python3
"""Prepared Plot token retry and crash-window regression tests."""
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from libraries import plot_commit_tokens as tokens  # noqa: E402


def main():
    original_path = tokens._path
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tokens._path = lambda book_id: Path(tmp) / f"{book_id}.json"
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
    finally:
        tokens._path = original_path
    print("[OK] prepared Plot token retry and crash recovery")


if __name__ == "__main__":
    main()
