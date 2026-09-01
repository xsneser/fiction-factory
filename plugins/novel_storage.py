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


def _download_cover(novel_dir: Path, cover_url: str) -> bool:
    """尽力下载封面到 novel_dir/cover.jpg（失败不影响存书）。"""
    if not cover_url:
        return False
    if (novel_dir / "cover.jpg").exists():
        return True   # 已缓存，避免增量更新重复拉取
    try:
        import requests
        r = requests.get(cover_url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://fanqienovel.com/",
        })
        if r.status_code == 200 and r.content:
            (novel_dir / "cover.jpg").write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


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
        "cover": info.get("cover", ""),
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(novel_dir / "info.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 封面本地缓存（尽力而为，失败不影响存书）
    if info.get("cover"):
        _download_cover(novel_dir, info["cover"])

    # 保存章节：文件名与 index 用真实章号（ch["index"]），支持区间下载（如 100..130 → 0100..0130.json）；
    # 全量从第 1 章下载时 idx==i+1，与旧行为一致。
    for i, ch in enumerate(chapters):
        idx = int(ch.get("index") or (i + 1))
        ch_file = ch_dir / f"{idx:04d}.json"
        with open(ch_file, "w", encoding="utf-8") as f:
            json.dump({
                "index": idx,
                "title": ch.get("title", f"第{idx}章"),
                "content": ch.get("content", ""),
                "word_count": ch.get("word_count", 0),
            }, f, ensure_ascii=False, indent=2)

    return safe_name


def save_chapter(platform: str, folder: str, ch: dict) -> None:
    """增量保存单章到 storage/novels/{platform}/{folder}/chapters/{index}.json
    （供下载中逐章落盘，书库实时可见；真实章号文件名，区间下载可续写）。"""
    ch_dir = NOVELS_DIR / platform / folder / "chapters"
    ch_dir.mkdir(parents=True, exist_ok=True)
    idx = int(ch.get("index") or 0)
    with open(ch_dir / f"{idx:04d}.json", "w", encoding="utf-8") as f:
        json.dump({
            "index": idx,
            "title": ch.get("title", f"第{idx}章"),
            "content": ch.get("content", ""),
            "word_count": ch.get("word_count", 0),
        }, f, ensure_ascii=False, indent=2)


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
                if (novel_dir / "cover.jpg").exists():
                    from urllib.parse import quote
                    info["cover_url"] = "/api/scout/novels/cover?platform=%s&folder=%s" % (
                        plat, quote(novel_dir.name))
                else:
                    info["cover_url"] = ""
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


def read_chapter(platform: str, novel_folder: str, index: int) -> Optional[dict]:
    """读单章（chapters/{index:04d}.json，真实章号），不整本重读。"""
    ch_file = NOVELS_DIR / platform / novel_folder / "chapters" / f"{int(index):04d}.json"
    if not ch_file.exists():
        return None
    with open(ch_file, encoding="utf-8") as f:
        return json.load(f)


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
