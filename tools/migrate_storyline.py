"""
一次性迁移脚本：timeline → storyline + 草稿并入书目录。

背景：故事线（storyline，旧名 timeline）改为每本书内置——`books/<book_id>/storyline.json`，
不再有独立的 `books/timelines/` 草稿目录与 `tl_*`/`gen_*` id。

执行动作（幂等，可重复运行）：
  1. 所有 `books/book_*/timeline.json` → 改名 `storyline.json`
  2. 处理 `books/timelines/*.json` 草稿：
       - 被某书 `source_timeline_id` 引用且书已有 storyline.json → 删除孤儿（书内有副本）
       - 被引用但书无副本 → 迁入该书目录
       - 真孤儿 → 升级为新 book_NNN 规划书（不丢数据）
  3. 删除 `books/timelines/` 目录
  4. 每本 book.json：去掉 `source_timeline_id`；`structure_template_id=="timeline"`→`"storyline"`

用法：
    python tools/migrate_storyline.py --dry-run   # 只打印计划
    python tools/migrate_storyline.py             # 执行
"""
import json
import re
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
BOOKS = ROOT / "books"
BOOK_ID_RE = re.compile(r"^book_\d{3,}$")

DRY_RUN = "--dry-run" in sys.argv


def next_book_id():
    existing = set()
    if BOOKS.is_dir():
        for d in BOOKS.iterdir():
            if d.is_dir() and BOOK_ID_RE.match(d.name):
                existing.add(d.name)
    n = 1
    while f"book_{n:03d}" in existing:
        n += 1
    return f"book_{n:03d}"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  读取失败 {path.relative_to(ROOT)}: {e}")
        return None


def write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def act(msg: str, fn):
    """dry-run 只打印；否则执行 fn。"""
    if DRY_RUN:
        print(f"  [计划] {msg}")
        return
    fn()
    print(f"  ✔ {msg}")


def main():
    if not BOOKS.exists():
        print(f"❌ books/ 目录不存在: {BOOKS}")
        return 1
    print(("─" * 56) + ("\nDRY-RUN 模式：仅预览，不改盘\n" if DRY_RUN else "\n执行迁移：\n"))

    renamed, deleted, promoted, adopted = 0, 0, 0, 0

    # 1. 书内 timeline.json → storyline.json
    for d in sorted(BOOKS.glob("book_*")):
        if not d.is_dir():
            continue
        tl = d / "timeline.json"
        if tl.exists():
            dst = d / "storyline.json"
            if dst.exists():
                act(f"{tl.relative_to(ROOT)} 与 storyline.json 并存 → 删除旧文件",
                    lambda: tl.unlink())
                deleted += 1
            else:
                act(f"{tl.relative_to(ROOT)} → storyline.json",
                    lambda tl=tl, dst=dst: tl.replace(dst))
                renamed += 1

    # 2. 草稿处理
    drafts_dir = BOOKS / "timelines"
    if drafts_dir.is_dir():
        for f in sorted(drafts_dir.glob("*.json")):
            draft_id = f.stem
            linked_dir = None
            for d in BOOKS.iterdir():
                if d.is_dir() and (d / "book.json").exists():
                    cfg = read_json(d / "book.json")
                    if cfg and cfg.get("source_timeline_id") == draft_id:
                        linked_dir = d
                        break
            if linked_dir is not None:
                sl = linked_dir / "storyline.json"
                if sl.exists():
                    act(f"草稿 {f.name} 被 {linked_dir.name} 引用且有副本 → 删除孤儿",
                        lambda f=f: f.unlink())
                    deleted += 1
                else:
                    act(f"草稿 {f.name} 被 {linked_dir.name} 引用且无副本 → 迁入",
                        lambda f=f, sl=sl: f.replace(sl))
                    adopted += 1
            else:
                # 真孤儿草稿 → 升级为新规划书
                tl = read_json(f)
                if not tl:
                    act(f"草稿 {f.name} 损坏 → 跳过", lambda: None)
                    continue
                new_id = next_book_id()
                new_dir = BOOKS / new_id
                outlines = tl.get("outlines", [])
                chapter_count = max((int(o.get("end_chapter", 0) or 0) for o in outlines),
                                    default=500) or 500
                cfg = {
                    "book_id": new_id,
                    "title": tl.get("book_title") or "(待定)",
                    "pen_name": tl.get("pen_name") or "",
                    "genre": tl.get("genre") or "",
                    "sub_genre": tl.get("sub_genre") or "",
                    "platform": tl.get("platform") or "fanqie",
                    "chapter_count": chapter_count,
                    "current_chapter": 0,
                    "words_per_chapter": int(tl.get("words_per_chapter") or 3000),
                    "total_words": 0,
                    "status": "planning",
                    "structure_template_id": "storyline",
                    "assigned_profiles": [],
                    "assigned_gags": [],
                    "assigned_themes": [],
                    "opening_template_id": "",
                    "first_three_chapters": {},
                    "style_profile_id": "",
                    "budget": 50.0,
                    "detector_frequency": 1,
                    "current_cost": 0.0,
                    "created_at": tl.get("generated_at") or "",
                    "updated_at": tl.get("updated_at") or "",
                    "finished_at": "",
                    "published_at": "",
                    "exported_at": "",
                    "publish_note": "",
                }
                act(f"草稿 {f.name} 真孤儿 → 升级为新书 {new_id}/（status=planning）",
                    lambda f=f, new_dir=new_dir, cfg=cfg, tl=tl: _promote(f, new_dir, cfg, tl))
                promoted += 1

        act(f"删除目录 books/timelines/", lambda: shutil.rmtree(drafts_dir, ignore_errors=True))

    # 3. book.json 清理
    for d in sorted(BOOKS.glob("book_*")):
        if not d.is_dir():
            continue
        cfg_path = d / "book.json"
        if not cfg_path.exists():
            continue
        cfg = read_json(cfg_path)
        if not cfg:
            continue
        changed = False
        if "source_timeline_id" in cfg:
            del cfg["source_timeline_id"]
            changed = True
        if cfg.get("structure_template_id") == "timeline":
            cfg["structure_template_id"] = "storyline"
            changed = True
        if changed:
            act(f"{cfg_path.relative_to(ROOT)} 清理 source_timeline_id / structure_template_id",
                lambda cfg_path=cfg_path, cfg=cfg: write_json(cfg_path, cfg))

    print("─" * 56)
    print(f"完成：改名 {renamed} · 删除孤儿 {deleted} · 升级新书 {promoted} · 迁入 {adopted}"
          + ("（DRY-RUN 未改动任何文件）" if DRY_RUN else ""))
    return 0


def _promote(f: Path, new_dir: Path, cfg: dict, tl: dict):
    new_dir.mkdir(exist_ok=True)
    (new_dir / "chapters").mkdir(exist_ok=True)
    (new_dir / "outline").mkdir(exist_ok=True)
    write_json(new_dir / "book.json", cfg)
    f.replace(new_dir / "storyline.json")


if __name__ == "__main__":
    raise SystemExit(main())
