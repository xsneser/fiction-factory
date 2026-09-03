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
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.json_store import write_json_atomic
from core.safe_paths import ensure_child_path

logger = logging.getLogger("novel_storage")

NOVELS_DIR = Path(__file__).parent.parent / "storage" / "novels"

# 旧平台子目录名（迁移源；统一后不再新建）
_LEGACY_PLATFORM_DIRS = ("fanqie", "web", "qidian", "jinjiang")


def ensure_dirs():
    NOVELS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_novel_dir(novel_folder: str) -> Path:
    """把客户端/外部给的 folder 安全解析成书目录：必须是 NOVELS_DIR 的直接子目录名。

    拒绝空、`.`/`..`、含路径分隔或绝对路径（防 `folder=..`/绝对路径越权 rmtree 或
    读任意文件）。越界抛 ValueError，调用方（端点）转 400。
    """
    if not novel_folder:
        raise ValueError("folder 为空")
    if novel_folder in (".", ".."):
        raise ValueError(f"非法 folder: {novel_folder!r}")
    f = Path(novel_folder)
    if f.name != novel_folder:   # 含 / 或 \ 路径分隔 / 绝对路径
        raise ValueError(f"非法 folder: {novel_folder!r}")
    return ensure_child_path(NOVELS_DIR, NOVELS_DIR / novel_folder)


def _count_saved_chapters(ch_dir: Path) -> int:
    """数已落盘且 content 非空的章节数（占位/空正文不算，与合并路径 content-aware 一致）。"""
    if not ch_dir.exists():
        return 0
    n = 0
    for ch_file in ch_dir.glob("*.json"):
        try:
            with ch_file.open(encoding="utf-8") as f:
                if json.load(f).get("content"):
                    n += 1
        except Exception:
            logger.warning("跳过无法解析的章节 %s", ch_file)
            continue
    return n


def _download_cover(novel_dir: Path, cover_url: str) -> bool:
    """尽力下载封面到 novel_dir/cover.jpg（失败不影响存书）。

    临时文件 + os.replace 原子落盘；已存在且非 0 字节视为已缓存（0 字节残留可重下）。
    """
    if not cover_url:
        return False
    cover_file = novel_dir / "cover.jpg"
    if cover_file.exists() and cover_file.stat().st_size > 0:
        return True   # 已缓存，避免增量更新重复拉取
    try:
        import requests
        r = requests.get(cover_url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://fanqienovel.com/",
        })
        if r.status_code == 200 and r.content:
            tmp = novel_dir / ".cover.jpg.tmp"
            tmp.write_bytes(r.content)
            os.replace(str(tmp), str(cover_file))
            return True
    except Exception:
        logger.warning("封面下载失败: %s", cover_url)
    return False


def _write_chapter(ch_dir: Path, idx: int, ch: dict) -> None:
    _ch = {
        "index": int(idx),
        "title": ch.get("title", f"第{int(idx)}章"),
        "content": ch.get("content", ""),
        "word_count": ch.get("word_count", 0),
    }
    if ch.get("source"):
        _ch["source"] = ch["source"]   # 本章获取来源（镜像站 site / 番茄 / 合并）
    write_json_atomic(ch_dir / f"{int(idx):04d}.json", _ch)


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
    write_json_atomic(novel_dir / "info.json", meta)

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
    try:
        novel_dir = resolve_novel_dir(folder)
    except ValueError as exc:
        raise ValueError(f"save_chapter 非法 folder: {folder!r}") from exc
    ch_dir = novel_dir / "chapters"
    ch_dir.mkdir(parents=True, exist_ok=True)
    _write_chapter(ch_dir, int(ch.get("index") or 0), ch)


_LIST_TTL = 2.0
_list_cache = {"ts": 0.0, "platform": "", "data": None}   # 书库列表短 TTL（下载中新增书 ≤2s 可见）


def list_novels(platform: str = "") -> list[dict]:
    """列出已下载的小说（统一书库）。platform 非空时按 info.json 的 platform 字段过滤。

    短 TTL(2s) 缓存整库目录扫描（书籍元数据变化不频繁）；每次返回浅拷贝，避免调用方
    改写污染缓存。
    """
    now = time.time()
    if (_list_cache["data"] is not None
            and _list_cache["platform"] == platform
            and now - _list_cache["ts"] < _LIST_TTL):
        return [dict(i) for i in _list_cache["data"]]
    ensure_dirs()
    novels = []
    for novel_dir in sorted(d for d in NOVELS_DIR.iterdir() if d.is_dir()):
        if novel_dir.name in _LEGACY_PLATFORM_DIRS:
            continue   # 旧平台子目录（迁移前残留）跳过
        info_file = novel_dir / "info.json"
        if not info_file.exists():
            continue
        try:
            with open(info_file, encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            logger.warning("跳过无法解析的 info %s", info_file)
            continue
        if platform and info.get("platform", "") != platform:
            continue
        info["path"] = str(novel_dir)
        info["saved_chapters"] = _count_saved_chapters(novel_dir / "chapters")
        info["folder"] = novel_dir.name
        if (novel_dir / "cover.jpg").exists():
            from urllib.parse import quote
            info["cover_url"] = "/api/scout/novels/cover?folder=%s" % quote(novel_dir.name)
        else:
            info["cover_url"] = ""
        novels.append(info)
    _list_cache.update(ts=now, platform=platform, data=list(novels))
    return novels


def load_novel(platform: str, novel_folder: str,
               with_content: bool = True) -> Optional[dict]:
    """加载一本小说数据（统一书库：路径以 folder 为准）。

    with_content=False：只读章元数据（index/title/word_count/source），不保留正文——
    供目录/阅读器列表用，避免几千章把整本正文全部载入内存（正文按章走 read_chapter 懒加载）。
    """
    try:
        novel_dir = resolve_novel_dir(novel_folder)
    except ValueError:
        logger.warning("load_novel 非法 folder: %r", novel_folder)
        return None
    info_file = novel_dir / "info.json"
    if not info_file.exists():
        return None
    try:
        with open(info_file, encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        logger.warning("跳过无法解析的 info %s", info_file)
        return None

    ch_dir = novel_dir / "chapters"
    chapters = []
    if ch_dir.exists():
        for ch_file in sorted(ch_dir.glob("*.json")):
            try:
                with open(ch_file, encoding="utf-8") as f:
                    ch = json.load(f)
            except Exception:
                logger.warning("跳过无法解析的章节 %s", ch_file)
                continue
            if with_content:
                chapters.append(ch)
            else:
                chapters.append({
                    "index": ch.get("index", 0),
                    "title": ch.get("title", ""),
                    "word_count": ch.get("word_count", 0),
                })

    return {"info": info, "chapters": chapters}


def read_chapter(platform: str, novel_folder: str, index: int) -> Optional[dict]:
    """读单章（storage/novels/{folder}/chapters/{index:04d}.json，真实章号）。"""
    try:
        novel_dir = resolve_novel_dir(novel_folder)
    except ValueError:
        logger.warning("read_chapter 非法 folder: %r", novel_folder)
        return None
    ch_file = novel_dir / "chapters" / f"{int(index):04d}.json"
    if not ch_file.exists():
        return None
    try:
        with open(ch_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("跳过无法解析的章节 %s", ch_file)
        return None


def read_novel_info(novel_folder: str) -> Optional[dict]:
    """只读书元数据（info.json）——不扫章节目录。

    供单章 / 成批顺序读免整本目录开销（load_novel 即使 with_content=False 也会遍历
    全部章节文件建目录）。非法 folder 返回 None。
    """
    try:
        novel_dir = resolve_novel_dir(novel_folder)
    except ValueError:
        logger.warning("read_novel_info 非法 folder: %r", novel_folder)
        return None
    info_file = novel_dir / "info.json"
    if not info_file.exists():
        return None
    try:
        with open(info_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("跳过无法解析的 info %s", info_file)
        return None


def read_chapter_range(novel_folder: str, start: int, end: int) -> list[dict]:
    """成批顺序读 [start, end] 真实章号区间（含两端）正文——只读该区间文件，不整本扫盘。

    章节文件按 `####.json` 真实章号命名；缺失章（镜像合并留白/占位）跳过。返回按
    index 升序、含正文的章节 dict 列表（{index,title,content,word_count[,source]}）。
    供整本扫读分窗一次取若干章，免每章一次工具往返。
    """
    if int(end) < int(start):
        end = start
    try:
        novel_dir = resolve_novel_dir(novel_folder)
    except ValueError:
        logger.warning("read_chapter_range 非法 folder: %r", novel_folder)
        return []
    ch_dir = novel_dir / "chapters"
    out = []
    for idx in range(int(start), int(end) + 1):
        ch_file = ch_dir / f"{idx:04d}.json"
        if not ch_file.exists():
            continue
        try:
            with open(ch_file, encoding="utf-8") as f:
                ch = json.load(f)
            if ch.get("content"):
                out.append(ch)
        except Exception:
            logger.warning("跳过无法解析的章节 %s", ch_file)
            continue
    return out


def delete_novel(platform: str, novel_folder: str) -> bool:
    """删除一部小说（统一书库：路径以 folder 为准；非法 folder 拒绝不删）。"""
    try:
        novel_dir = resolve_novel_dir(novel_folder)
    except ValueError:
        logger.warning("delete_novel 非法 folder: %r", novel_folder)
        return False
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


def parse_book_id(info: Optional[dict]) -> tuple[str, str]:
    """归一 info['book_id'] → (平台/源, 数字 id)。

    各平台历史写法不一：merged=`fanqie:{id}`、web=`{site}:{id}`、fanqie=裸数字。
    返回首个为源（fanqie/web 或镜像 site），第二个为剥离前缀的 id；空返回 ("", "")。
    只读归一，不改写存量 info（管线 only）。
    """
    raw = str((info or {}).get("book_id", "") or "").strip()
    if not raw:
        return "", ""
    if ":" in raw:
        src, _, rest = raw.partition(":")
        return src, rest
    return "fanqie", raw
