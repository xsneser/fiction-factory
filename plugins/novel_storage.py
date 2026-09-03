"""
小说存储管理 — 统一书库：每书一个文件夹，平台为 info 字段（不分平台）。

storage/novels/
├── 书名1/
│   ├── info.json    # 元数据(title, author, intro, cover, platform, book_id...)
│   ├── cover.jpg    # 封面（尽力）
│   └── chapters/
│       ├── 0001.json
│       └── ...
└── 书名2/
...

2026-09-01 起统一：平台不再作为目录层级。旧 platform 子目录（fanqie/web/...）用
migrate_unified() 迁移到统一目录；各函数仍保留 platform 参数签名（兼容 MCP schema 与
旧调用方），但路径一律以 folder 为准，platform 仅作字段/过滤/展示。
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

NOVELS_DIR = Path(__file__).parent.parent / "storage" / "novels"

# 旧平台子目录名（迁移源；统一后不再新建）
_LEGACY_PLATFORM_DIRS = ("fanqie", "web", "qidian", "jinjiang")


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


def _write_chapter(ch_dir: Path, idx: int, ch: dict) -> None:
    with open(ch_dir / f"{int(idx):04d}.json", "w", encoding="utf-8") as f:
        _ch = {
            "index": int(idx),
            "title": ch.get("title", f"第{int(idx)}章"),
            "content": ch.get("content", ""),
            "word_count": ch.get("word_count", 0),
        }
        if ch.get("source"):
            _ch["source"] = ch["source"]   # 本章获取来源（镜像站 site / 番茄 / 合并）
        json.dump(_ch, f, ensure_ascii=False, indent=2)


def save_novel(platform: str, info: dict, chapters: list[dict]) -> str:
    """
    保存一部小说到 storage/novels/{书名}/
    返回 folder (文件夹名)。platform 参数仅作字段兜底，不入路径。
    """
    ensure_dirs()
    safe_name = _safe_name(info.get("title", "unknown"))
    novel_dir = NOVELS_DIR / safe_name
    novel_dir.mkdir(parents=True, exist_ok=True)
    (novel_dir / "chapters").mkdir(exist_ok=True)

    meta = {
        "title": info.get("title", ""),
        "author": info.get("author", ""),
        "platform": info.get("platform") or platform,
        "book_id": str(info.get("book_id", "")),
        "url": info.get("url", ""),
        "genre": info.get("genre", ""),
        "chapter_count": info.get("chapter_count", 0),
        "cover": info.get("cover", ""),
        "intro": info.get("intro", ""),   # 简介（番茄权威，镜像回退）
        "site": info.get("site", ""),     # 镜像站来源（如 wodushu）
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 合并来源信息（合并抓取传入）
    for k in ("source_fanqie", "source_web", "fallback_fanqie",
              "head_verified", "head_site"):
        if info.get(k) is not None:
            meta[k] = info[k]
    with open(novel_dir / "info.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 封面本地缓存（尽力而为，失败不影响存书）
    if info.get("cover"):
        _download_cover(novel_dir, info["cover"])

    # 章节：文件名与 index 用真实章号，支持区间下载（如 100..130 → 0100..0130.json）
    for i, ch in enumerate(chapters):
        idx = int(ch.get("index") or (i + 1))
        _write_chapter(novel_dir / "chapters", idx, ch)
    return safe_name


def save_chapter(platform: str, folder: str, ch: dict) -> None:
    """增量保存单章到 storage/novels/{folder}/chapters/{index}.json
    （供下载中逐章落盘，书库实时可见；真实章号文件名，区间下载可续写）。"""
    ch_dir = NOVELS_DIR / folder / "chapters"
    ch_dir.mkdir(parents=True, exist_ok=True)
    _write_chapter(ch_dir, int(ch.get("index") or 0), ch)


def list_novels(platform: str = "") -> list[dict]:
    """列出已下载的小说（统一书库）。platform 非空时按 info.json 的 platform 字段过滤。"""
    ensure_dirs()
    novels = []
    for novel_dir in sorted(d for d in NOVELS_DIR.iterdir() if d.is_dir()):
        if novel_dir.name in _LEGACY_PLATFORM_DIRS:
            continue   # 旧平台子目录（迁移前残留）跳过
        info_file = novel_dir / "info.json"
        if not info_file.exists():
            continue
        with open(info_file, encoding="utf-8") as f:
            info = json.load(f)
        if platform and info.get("platform", "") != platform:
            continue
        info["path"] = str(novel_dir)
        ch_dir = novel_dir / "chapters"
        chapter_files = sorted(ch_dir.glob("*.json")) if ch_dir.exists() else []
        info["saved_chapters"] = len(chapter_files)
        info["folder"] = novel_dir.name
        if (novel_dir / "cover.jpg").exists():
            from urllib.parse import quote
            info["cover_url"] = "/api/scout/novels/cover?folder=%s" % quote(novel_dir.name)
        else:
            info["cover_url"] = ""
        novels.append(info)
    return novels


def load_novel(platform: str, novel_folder: str) -> Optional[dict]:
    """加载一本完整的小说数据（统一书库：路径以 folder 为准）。"""
    novel_dir = NOVELS_DIR / novel_folder
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
    """读单章（storage/novels/{folder}/chapters/{index:04d}.json，真实章号）。"""
    ch_file = NOVELS_DIR / novel_folder / "chapters" / f"{int(index):04d}.json"
    if not ch_file.exists():
        return None
    with open(ch_file, encoding="utf-8") as f:
        return json.load(f)


def delete_novel(platform: str, novel_folder: str) -> bool:
    """删除一部小说（统一书库：路径以 folder 为准）。"""
    novel_dir = NOVELS_DIR / novel_folder
    if novel_dir.exists():
        shutil.rmtree(novel_dir)
        return True
    return False


def migrate_unified() -> dict:
    """把旧平台子目录（fanqie/web/...）里的书迁移到统一目录 storage/novels/<书名>/。

    更新 info.json（platform 字段 + intro 空 + 合并元数据），移动章节文件（跳过已存在），
    清空空平台子目录。幂等，可重复执行。
    """
    ensure_dirs()
    moved, updated = [], []
    for plat in [d.name for d in NOVELS_DIR.iterdir()
                 if d.is_dir() and d.name in _LEGACY_PLATFORM_DIRS]:
        plat_dir = NOVELS_DIR / plat
        for novel_dir in sorted(plat_dir.iterdir()):
            info_file = novel_dir / "info.json"
            if not info_file.exists():
                continue
            with open(info_file, encoding="utf-8") as f:
                info = json.load(f)
            dest = NOVELS_DIR / _safe_name(info.get("title", novel_dir.name))
            dest.mkdir(parents=True, exist_ok=True)
            dest_info_file = dest / "info.json"
            dest_info = {}
            if dest_info_file.exists():
                with open(dest_info_file, encoding="utf-8") as f:
                    dest_info = json.load(f)
            # 合并元数据：platform 兜底、intro 缺省、其余缺则补
            if not dest_info.get("platform"):
                dest_info["platform"] = info.get("platform") or plat
            if not dest_info.get("intro"):
                dest_info["intro"] = info.get("intro", "")
            for k in ("title", "author", "book_id", "url", "genre",
                      "chapter_count", "cover", "site"):
                if not dest_info.get(k) and info.get(k):
                    dest_info[k] = info[k]
            dest_info.setdefault("downloaded_at",
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            with open(dest_info_file, "w", encoding="utf-8") as f:
                json.dump(dest_info, f, ensure_ascii=False, indent=2)
            # 移动章节文件（跳过已存在）
            src_ch = novel_dir / "chapters"
            dest_ch = dest / "chapters"
            n = 0
            if src_ch.is_dir():
                dest_ch.mkdir(parents=True, exist_ok=True)
                for ch_file in src_ch.glob("*.json"):
                    target = dest_ch / ch_file.name
                    if not target.exists():
                        shutil.move(str(ch_file), str(target))
                        n += 1
            updated.append({"title": info.get("title", novel_dir.name), "chapters_moved": n})
            moved.append({"from": str(novel_dir), "to": str(dest)})
        try:
            shutil.rmtree(plat_dir)
        except Exception:
            pass
    return {"moved": moved, "updated": updated}


def _safe_name(name: str) -> str:
    """将书名转为安全的文件夹名"""
    import re
    name = re.sub(r'[\\/:*?"<>|]', '_', name).strip()
    return name[:60] or "unknown"
