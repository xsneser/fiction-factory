"""合并抓取：番茄元数据+权威目录 + 镜像站全文 → 统一书库一本。

`download_book_merged` 是 MCP `fetch_book` / `/api/scout/run` 默认 / CLI 的统一入口。

流程：
1. 输入判定：url（fanqie 域→番茄，否则镜像站）/ book_id（数字→番茄）/ 书名
2. 番茄元数据 + 权威目录（尽力；失败回退镜像站）
3. 镜像站全文（URL 直给，或 Bing 搜书名取本站书页）
4. 合并落盘：番茄目录为骨架（title/index 番茄为准），镜像按章节号补全文，
   番茄比镜像多的章节落 title-only 占位（content 空）；断点续下。

番茄解析不到 → 回退镜像站元数据（platform=web + fallback_fanqie）。
"""
import json
import logging
import re
import time

from plugins.webnovel_scraper import _parse_chapter_num

logger = logging.getLogger("novel-engine.book_fetch")


def download_book_merged(title: str = "", url: str = "", book_id: str = "",
                         site: str = "wodushu", chapters: int = 0,
                         start_chapter: int = 1, end_chapter: int = 0,
                         download_delay: float = 0.5, on_progress=None,
                         platform: str = "merged"):
    """合并抓取 → storage/novels/{书名}/。返回 (meta, dl)；dl 含 folder/chapters/already/sources。"""
    source = url or book_id or title
    if not source:
        raise RuntimeError("请提供书名 / 番茄 book_id / 镜像站 URL")
    from plugins.fanqie_scout import FanqieCrawler
    from plugins.webnovel_scraper import WebnovelCrawler

    is_http = source.startswith("http")
    is_fanqie_url = is_http and ("fanqie" in source or "novel.snssdk" in source)
    mirror_url = url if (is_http and not is_fanqie_url) else ""
    fanqie_rid = (book_id or "") if not is_http else ""
    if not fanqie_rid and not is_http and source.isdigit() and len(source) >= 8:
        fanqie_rid = source
    query_title = title or (source if (not is_http and not fanqie_rid) else "")

    fanqie = FanqieCrawler()

    def _fmeta_from(novel):
        nonlocal f_meta, f_catalog
        cc = int(novel.chapter_count or 0)
        f_meta = {
            "book_id": novel.book_id, "title": novel.title, "author": novel.author,
            "intro": novel.intro or "", "cover": novel.cover or "",
            "url": novel.url or f"https://fanqienovel.com/page/{novel.book_id}",
            "genre": novel.genre or "", "chapter_count": cc,
        }
        f_catalog = fanqie.get_chapter_list(novel.book_id, cc or 100000)
        if on_progress:
            on_progress("search", 1, 1, f"番茄: {novel.title}（目录 {len(f_catalog)} 章）")

    # ── 番茄元数据 + 权威目录（尽力） ──
    f_meta, f_catalog = None, []
    try:
        if fanqie_rid:
            n = fanqie._get_novel_from_page(fanqie_rid)
            if n:
                _fmeta_from(n)
        elif query_title:
            n = fanqie.search_novel(query_title)
            if n:
                _fmeta_from(n)
    except Exception as e:
        logger.warning(f"fanqie resolve failed: {e}")

    # ── 多镜像源并行解析（每个源独立 requests.Session，并发 2-3 防封） ──
    from plugins.webnovel_scraper import MIRROR_SOURCES
    import concurrent.futures as _cf
    from urllib.parse import urlparse as _up
    fanqie_nums = {c["num"] for c in f_catalog if c.get("num") is not None}
    target_title = (f_meta or {}).get("title") or query_title
    mirror_host = _up(mirror_url).netloc if mirror_url else ""

    def _resolve_source(sn):
        try:
            crawler = MIRROR_SOURCES[sn]()
            w_meta = None
            if mirror_host and mirror_host == _up(crawler.cfg["base_url"]).netloc:
                w_meta = crawler.resolve_book(mirror_url)   # 用户给的该源 URL
            elif target_title:
                w_url = crawler.search_book_url(target_title)
                if w_url:
                    w_meta = crawler.resolve_book(w_url)
            if not w_meta:
                return None
            w_cat = crawler.get_chapter_list(w_meta["book_id"])
            main_re = crawler.cfg.get("main_title_re")
            if main_re:
                filt = [c for c in w_cat if re.match(main_re, c["title"])]
                if filt:
                    w_cat = filt
            wmap = {c["num"]: c for c in w_cat if c["num"] is not None}
            coverage = (len(set(wmap) & fanqie_nums) if fanqie_nums else len(wmap))
            return {"site": sn, "crawler": crawler, "w_meta": w_meta,
                    "wmap": wmap, "coverage": coverage}
        except Exception as e:
            logger.warning(f"mirror {sn} resolve failed: {e}")
            return None

    sources = {}
    with _cf.ThreadPoolExecutor(max_workers=min(3, len(MIRROR_SOURCES))) as _ex:
        for _r in _ex.map(_resolve_source, list(MIRROR_SOURCES)):
            if _r and _r["wmap"]:
                sources[_r["site"]] = _r
                if on_progress:
                    on_progress("search", 1, 1,
                                f"镜像源 {_r['site']}: 主书 {len(_r['wmap'])} 章可用")

    # 选主源：coverage（覆盖番茄章节号数）最高
    primary = max(sources.values(), key=lambda s: s["coverage"]) if sources else None

    # ── 给镜像 URL 时反向补番茄元数据（用镜像书名） ──
    if not f_meta and primary:
        try:
            n = fanqie.search_novel(primary["w_meta"].get("title", ""))
            if n:
                _fmeta_from(n)
        except Exception as e:
            logger.warning(f"fanqie backfill by mirror title failed: {e}")

    if not f_meta and not primary:
        raise RuntimeError(f"未找到该书（番茄与镜像站均解析失败）: {source}")

    if f_meta:
        return _save_merged(f_meta, f_catalog, sources, primary, site, chapters,
                            start_chapter, end_chapter, download_delay, on_progress, platform)
    return _save_mirror_only(primary, site, chapters, start_chapter,
                             end_chapter, download_delay, on_progress)


def _save_merged(f_meta, f_catalog, sources, primary, site, chapters, start, end,
                 delay, on_progress, platform):
    """番茄元数据 + 番茄目录权威 + 多镜像源按章节号补全文（主源优先、缺章从其他源补）。"""
    from plugins.novel_storage import save_novel, save_chapter, NOVELS_DIR, _safe_name
    total = len(f_catalog)
    if total == 0:
        raise RuntimeError(f"番茄目录为空: {f_meta['title']}")
    start = max(1, int(start or 1))
    if int(chapters or 0) > 0:
        e = int(end or 0)
        e = (start + int(chapters) - 1) if e <= 0 else e
    else:
        e = int(end or 0) or total
    e = min(e, total)
    selected = [c for c in f_catalog if start <= int(c.get("index") or 0) <= e]
    if not selected:
        raise RuntimeError(f"起始章 {start} 超出番茄目录范围（共 {total} 章）")

    # 主源：primary 优先；无则 site 参数；再回退任一源
    main_src = primary or (sources.get(site) if sources else None) or \
        (next(iter(sources.values())) if sources else None)
    main_site = main_src["site"] if main_src else (site or "wodushu")
    folder = _safe_name(f_meta["title"])
    meta = {
        "title": f_meta["title"], "author": f_meta["author"],
        "platform": platform, "book_id": f"fanqie:{f_meta['book_id']}",
        "url": f_meta["url"], "genre": f_meta.get("genre", ""),
        "chapter_count": total, "cover": f_meta.get("cover", ""),
        "intro": f_meta.get("intro", ""), "site": main_site,
        "source_fanqie": {"book_id": f_meta["book_id"], "url": f_meta["url"]},
        "fallback_fanqie": False,
    }
    if main_src:
        meta["source_web"] = {"site": main_src["site"],
                              "book_id": main_src["w_meta"].get("book_id", ""),
                              "url": main_src["w_meta"].get("url", "")}

    # 断点续下：只把**有正文**的章节算作已下载（空占位不阻断补全）
    ch_dir = NOVELS_DIR / folder / "chapters"
    existing = set()
    if ch_dir.is_dir():
        for f in ch_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("content"):
                    existing.add(int(f.stem))
            except Exception:
                pass
    pending = [c for c in selected if c["index"] not in existing]
    if not pending:
        if on_progress:
            on_progress("search", 1, 1, f"已是最新（{len(existing)} 章），无需下载")
        return meta, {"folder": folder, "chapters": 0, "already": True, "sources": "merged"}
    if on_progress:
        on_progress("search", 1, 1,
                    f"合并抓取: {f_meta['title']}（番茄目录 {total} 章，本次 {len(pending)} 章）")

    # 多源顺序：主源在前，其余按 coverage 降序
    source_order = ([main_src] if main_src else []) + \
        sorted([s for s in sources.values() if s is not main_src],
               key=lambda s: s["coverage"], reverse=True)

    save_novel(platform, meta, [])   # 先建目录+info.json（/scout 立即显示）
    downloaded = 0
    for i, ch in enumerate(pending):
        idx = ch["index"]
        content = ""
        # 镜像按标题「第N章」号匹配（番茄目录 index=realChapterOrder ≠ 标题章号）；多源补缺
        fnum = _parse_chapter_num(ch["title"])
        if fnum is not None:
            for src in source_order:
                mch = src["wmap"].get(fnum)
                if mch:
                    content = src["crawler"].download_chapter(
                        src["w_meta"]["book_id"], mch["chapter_id"])
                    if content:
                        break
        # 番茄目录权威：镜像无此章（番茄比镜像多/镜像缺号）也落 title-only 占位
        save_chapter(platform, folder, {
            "index": idx, "title": ch["title"], "content": content or "",
            "word_count": len(re.findall(r"[一-鿿]", content or "")),
        })
        if content:
            downloaded += 1
        if on_progress:
            on_progress("download", i + 1, len(pending), ch["title"][:30])
        if i < len(pending) - 1:
            time.sleep(delay)
    if on_progress:
        on_progress("download", len(pending), len(pending),
                    f"下载完成 {downloaded}章（本次新增）")
    return meta, {"folder": folder, "chapters": downloaded, "already": False, "sources": "merged"}


def _save_mirror_only(primary, site, chapters, start, end, delay, on_progress):
    """番茄解析失败 → 回退覆盖最全的镜像源元数据 + 镜像主书。"""
    from plugins.webnovel_scraper import download_webnovel
    src_site = primary["site"] if primary else (site or "wodushu")
    w_meta = primary["w_meta"] if primary else {}
    info, dl = download_webnovel(
        site=src_site, url=w_meta.get("url", ""), book_id=w_meta.get("book_id", ""),
        chapters=chapters, start_chapter=start, end_chapter=end,
        download_delay=delay, on_progress=on_progress, platform="web")
    # 标记番茄回退
    try:
        from plugins.novel_storage import NOVELS_DIR
        info_file = NOVELS_DIR / dl["folder"] / "info.json"
        d = json.loads(info_file.read_text(encoding="utf-8"))
        d["fallback_fanqie"] = True
        info_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"mark fallback_fanqie failed: {e}")
    return info, {**dl, "sources": "mirror-fallback"}


def enrich_merged_from_disk(folder: str, site: str = "wodushu", on_progress=None) -> dict:
    """把已下载的镜像站正文目录，按番茄权威目录重建（番茄 title/index + 已有正文，缺的留空占位）。

    用于迁移后把旧镜像站目录富集成 merged：复用已落盘正文不重新下载；番茄比镜像多的章节
    落 title-only 占位（content 空）。更新 info.json（intro/cover/番茄 book_id/sources）。
    """
    import shutil
    import json as _json
    from plugins.novel_storage import NOVELS_DIR, _safe_name, save_chapter
    from plugins.fanqie_scout import FanqieCrawler
    from plugins.webnovel_scraper import _parse_chapter_num

    folder = _safe_name(folder)
    novel_dir = NOVELS_DIR / folder
    info_file = novel_dir / "info.json"
    if not info_file.exists():
        raise RuntimeError(f"未找到书: {folder}")
    info = _json.loads(info_file.read_text(encoding="utf-8"))
    ch_dir = novel_dir / "chapters"

    # 1) 现有章节按标题「第N章」号映射正文（不重下载）
    existing = {}
    if ch_dir.is_dir():
        for f in ch_dir.glob("*.json"):
            ch = _json.loads(f.read_text(encoding="utf-8"))
            num = _parse_chapter_num(ch.get("title", ""))
            if num is not None and ch.get("content"):
                existing.setdefault(num, ch["content"])

    # 2) 番茄权威目录
    fanqie = FanqieCrawler()
    n = fanqie.search_novel(info.get("title", ""))
    if not n:
        raise RuntimeError(f"番茄未找到: {info.get('title')}")
    catalog = fanqie.get_chapter_list(n.book_id, int(n.chapter_count or 0) or 100000)
    if not catalog:
        raise RuntimeError(f"番茄目录为空: {info.get('title')}")
    if on_progress:
        on_progress("search", 1, 1, f"番茄: {n.title}（权威目录 {len(catalog)} 章，复用已有正文重建）")

    # 3) 重建章节（番茄 index=realChapterOrder 为保存号；正文按标题「第N章」号匹配已有）
    shutil.rmtree(ch_dir, ignore_errors=True)
    ch_dir.mkdir(parents=True)
    downloaded = 0
    for i, ch in enumerate(catalog):
        idx = int(ch.get("index") or (i + 1))
        fnum = _parse_chapter_num(ch["title"])
        content = existing.get(fnum, "") if fnum is not None else ""
        save_chapter("merged", folder, {
            "index": idx, "title": ch["title"], "content": content,
            "word_count": len(re.findall(r"[一-鿿]", content)),
        })
        if content:
            downloaded += 1
        if on_progress:
            on_progress("download", i + 1, len(catalog), ch["title"][:30])

    # 4) 更新 info.json（番茄元数据 + sources）
    info["title"] = n.title
    info["author"] = n.author
    info["platform"] = "merged"
    info["book_id"] = f"fanqie:{n.book_id}"
    info["url"] = n.url or f"https://fanqienovel.com/page/{n.book_id}"
    info["intro"] = n.intro or info.get("intro", "")
    info["cover"] = n.cover or info.get("cover", "")
    info["chapter_count"] = len(catalog)
    info["source_fanqie"] = {"book_id": n.book_id, "url": info["url"]}
    info["fallback_fanqie"] = False
    info.setdefault("site", site)
    if not info.get("source_web"):
        info["source_web"] = {"site": site, "book_id": "", "url": ""}
    info_file.write_text(_json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"title": n.title, "catalog": len(catalog), "with_text": downloaded, "folder": folder}


def main():
    import argparse
    p = argparse.ArgumentParser(description="合并抓取（番茄元数据+镜像全文）")
    p.add_argument("--title", default="")
    p.add_argument("--url", default="")
    p.add_argument("--book_id", default="")
    p.add_argument("--site", default="wodushu")
    p.add_argument("--chapters", type=int, default=0)
    p.add_argument("--delay", type=float, default=0.4)
    args = p.parse_args()

    def prog(phase, cur, total, msg):
        print(f"[{phase}] {cur}/{total} {msg}", flush=True)

    try:
        meta, dl = download_book_merged(
            title=args.title, url=args.url, book_id=args.book_id, site=args.site,
            chapters=args.chapters, download_delay=args.delay, on_progress=prog)
        print(f"✅ {meta['title']} 合并完成 {dl['chapters']}章 → storage/novels/{dl['folder']}/")
    except Exception as e:
        print(f"❌ {e}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
