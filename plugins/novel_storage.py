"""
小说存储管理 — 将下载的小说按平台/书名整理保存

storage/novels/
├── fanqie/
│   ├── 书名1/
│   │   ├── info.json    # 元数据(title, author, link, platform...)
│   │   ├── chapters/    # 章节内容
│   │   │   ├── 0001.json
│   │   │   └── ...
│   ├── 书名2/
│   └── ...
├── qidian/  (future)
├── jinjiang/  (future)
└── web/  (future)
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

NOVELS_DIR = Path(__file__).parent.parent / "storage" / "novels"


def ensure_dirs():
    NOVELS_DIR.mkdir(parents=True, exist_ok=True)


def save_novel(platform: str, info: dict, chapters: list[dict]) -> str:
    """
    保存一部小说到 storage/novels/{platform}/{书名}/
    返回 novel_id (文件夹名)
    """
    ensure_dirs()
    safe_name = _safe_name(info.get("title", "unknown"))
    novel_dir = NOVELS_DIR / platform / safe_name
    novel_dir.mkdir(parents=True, exist_ok=True)

    ch_dir = novel_dir / "chapters"
    ch_dir.mkdir(exist_ok=True)

    # 保存元数据
    meta = {
        "title": info.get("title", ""),
        "author": info.get("author", ""),
        "platform": platform,
        "book_id": str(info.get("book_id", "")),
        "url": info.get("url", ""),
        "genre": info.get("genre", ""),
        "chapter_count": info.get("chapter_count", 0),
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(novel_dir / "info.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 保存章节
    for i, ch in enumerate(chapters):
        ch_file = ch_dir / f"{i+1:04d}.json"
        with open(ch_file, "w", encoding="utf-8") as f:
            json.dump({
                "index": i + 1,
                "title": ch.get("title", f"第{i+1}章"),
                "content": ch.get("content", ""),
                "word_count": ch.get("word_count", 0),
            }, f, ensure_ascii=False, indent=2)

    return safe_name


def list_novels(platform: str = "") -> list[dict]:
    """列出已下载的小说"""
    ensure_dirs()
    novels = []
    platforms = [platform] if platform else [d.name for d in NOVELS_DIR.iterdir() if d.is_dir()]
    for plat in platforms:
        plat_dir = NOVELS_DIR / plat
        if not plat_dir.exists():
            continue
        for novel_dir in sorted(plat_dir.iterdir()):
            info_file = novel_dir / "info.json"
            if info_file.exists():
                with open(info_file, encoding="utf-8") as f:
                    info = json.load(f)
                info["path"] = str(novel_dir)
                ch_dir = novel_dir / "chapters"
                chapter_files = sorted(ch_dir.glob("*.json")) if ch_dir.exists() else []
                info["saved_chapters"] = len(chapter_files)
                info["folder"] = novel_dir.name
                novels.append(info)
    return novels


def load_novel(platform: str, novel_folder: str) -> Optional[dict]:
    """加载一本完整的小说数据"""
    novel_dir = NOVELS_DIR / platform / novel_folder
    info_file = novel_dir / "info.json"
    if not info_file.exists():
        return None
    with open(info_file, encoding="utf-8") as f:
        info = json.load(f)

    ch_dir = novel_dir / "chapters"
    chapters = []
    if ch_dir.exists():
        for ch_file in sorted(ch_dir.glob("*.json")):
            with open(ch_file, encoding="utf-8") as f:
                chapters.append(json.load(f))

    return {"info": info, "chapters": chapters}


def delete_novel(platform: str, novel_folder: str) -> bool:
    """删除一部小说"""
    novel_dir = NOVELS_DIR / platform / novel_folder
    if novel_dir.exists():
        shutil.rmtree(novel_dir)
        return True
    return False


def _safe_name(name: str) -> str:
    """将书名转为安全的文件夹名"""
    import re
    name = re.sub(r'[\\/:*?"<>|]', '_', name).strip()
    return name[:60] or "unknown"
