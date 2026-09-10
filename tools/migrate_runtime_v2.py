"""保守迁移：仅重算可确定的运行态字段，不从正文推断任何剧情语义。

示例：
  python tools/migrate_runtime_v2.py --book book_002 --dry-run
  python tools/migrate_runtime_v2.py --book book_002
  python tools/migrate_runtime_v2.py --rollback backups/runtime_v2/book_002
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prose_units(text: str) -> int:
    from core.text_utils import count_prose_units
    return count_prose_units(text or "")


def rebuild_spans(bridges: list) -> list:
    offset, spans = 0, []
    for bridge in bridges:
        text = str((bridge or {}).get("text") or "")
        spans.append({"plot_id": (bridge or {}).get("plot_id"), "plot_name": (bridge or {}).get("plot_name"),
                      "run_id": (bridge or {}).get("run_id") or "", "start": offset, "end": offset + len(text),
                      "actual_prose_units": prose_units(text), "raw_codepoints": len(text)})
        offset += len(text) + 2
    return spans


def migrate(book_id: str, dry_run: bool, backup_root: Path) -> dict:
    directory = ROOT / "books" / book_id
    if not directory.exists():
        raise SystemExit(f"书不存在：{book_id}")
    changed, review, total = [], [], 0
    for path in sorted((directory / "chapters").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        content = str(data.get("content") or "")
        metrics = {"actual_prose_units": prose_units(content), "raw_codepoints": len(content)}
        total += metrics["actual_prose_units"]
        bridges = data.get("bridges") or []
        if bridges:
            for b in bridges:
                b.update({"actual_prose_units": prose_units(str(b.get("text") or "")),
                          "raw_codepoints": len(str(b.get("text") or ""))})
            data["plot_spans"] = rebuild_spans(bridges)
        elif data.get("plot_spans") is None:
            review.append(f"{path.name}: 无 bridges，未猜测 plot_spans")
        data.update(metrics)
        changed.append(path)
        if not dry_run:
            target = backup_root / book_id / "chapters" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    book_path = directory / "book.json"
    book = json.loads(book_path.read_text(encoding="utf-8"))
    book["total_words"] = total
    if not dry_run:
        target = backup_root / book_id / "book.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(book_path, target)
        book_path.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"book_id": book_id, "dry_run": dry_run, "chapters": len(changed), "total_words": total,
            "needs_review": review, "backup": str(backup_root / book_id) if not dry_run else None}


def rollback(backup: Path) -> None:
    if not backup.exists():
        raise SystemExit(f"备份不存在：{backup}")
    book_id = backup.name
    target = ROOT / "books" / book_id
    for src in backup.rglob("*.json"):
        dst = target / src.relative_to(backup)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(json.dumps({"ok": True, "rolled_back": book_id}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", required=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-root", default=str(ROOT / "backups" / "runtime_v2"))
    parser.add_argument("--rollback")
    args = parser.parse_args()
    if args.rollback:
        rollback(Path(args.rollback))
    elif args.book:
        print(json.dumps(migrate(args.book, args.dry_run, Path(args.backup_root)), ensure_ascii=False, indent=2))
    else:
        parser.error("--book 或 --rollback 必填")
