"""合并抓取：番茄元数据+权威目录 + 镜像站全文 → 统一书库一本。

`download_book_merged` 是 MCP `fetch_book` / `/api/scout/run` 默认 / CLI 的统一入口。

流程（综合抓取；merged 且有番茄基准时，分步上报 on_step）：
1. 输入判定：url（fanqie 域→番茄，否则镜像站）/ book_id（数字→番茄）/ 书名
2. 番茄元数据 + 权威目录（封面/简介/目录；尽力）→ **立即创建书目 info.json**（/scout 即时显示）
3. 并行探查镜像源（mirrors 限定集合；缺省全部 MIRROR_SOURCES）
4. **前十章核对**：目录级标题对齐（按「第N章」号）+ 正文全量比对——番茄前 HEAD_N 章免费
   正文**逐一**与镜像对应章经广告行过滤后的正文比相似度（非抽样）；判定镜像是否为该番茄书
   的忠实移植
5. 选源：有「完全一致」源优先用；全不一致 → 回退最佳源（镜像正文天然经广告行过滤）并
   head_verified=false 落 info.json
6. 填充正文：番茄目录为骨架（title/index 番茄为准）；**头章免费部分存番茄权威正文**（无广告），
   锁定/后段章按镜像章节号补全文；番茄比镜像多的章落 title-only 占位（content 空）；断点续下。

镜像正文始终经 webnovel_scraper.download_chapter → _clean_text 做 `_AD_LINE_RE` 广告**行**过滤；
核对不一致时的「过滤筛查」即依赖此既有链路。行内注入式广告（非独立广告行）不在行级覆盖范围，
作已知局限。

番茄解析不到 → 回退镜像站元数据（platform=web + fallback_fanqie）。
"""
import difflib
import json
import logging
import re
import time
from typing import Optional

from plugins.webnovel_scraper import _parse_chapter_num

logger = logging.getLogger("novel-engine.book_fetch")

# ── 前十章核对参数 ────────────────────────────────────────────────
HEAD_N = 10                 # 头章数：取前 HEAD_N 个可编号「第N章」的番茄目录项（跳过序章/楔子）
# 前十章核对 = 正文全量比对（非抽样）：番茄头章正文逐一与镜像对应章比对
FREE_FULL_MIN_CHARS = 300   # 番茄正文视为「免费全文」的最少 CJK 字数（锁章预览常低于此）
TITLE_OK_RATIO = 0.8        # 标题对齐阈值（=通过/缺章计未通过的期望头章数；须 ≥0.8 才算一致）
CONTENT_OK_RATIO = 0.7      # 正文归一化后 difflib 相似度阈值（镜像正文已去广告行再比）
FULL_LEN_RATIO = 0.85       # 头章存番茄全文前提：番茄 CJK 字数 ≥ 镜像正文 × 此比率（防锁章预览当全文）
_CHAPTER_PREFIX_RE = re.compile(r"^第[0-9一二三四五六七八九十百千零两]+章")


def _cjk_len(s):
    """中文字符数（与 novel_storage 字数口径一致）。"""
    return len(re.findall(r"[一-鿿]", s or ""))


def _norm(text):
    """正文归一化：去全部空白（含换行），供相似度比对。"""
    return re.sub(r"\s+", "", text or "")


def _chapter_suffix(title):
    """去「第N章」前缀后的标题名（阿拉伯/中文数字格式差异不误判标题对齐）。"""
    return _CHAPTER_PREFIX_RE.sub("", title or "").strip()


def _fanqie_head(f_catalog, head_n=HEAD_N):
    """取前 head_n 个可编号「第N章」的番茄目录项（跳过序章/楔子等无法解析章号者）。

    返回 (head_nums, head_index_set, f_by_num)：
      head_nums      头章标题章号 list[int]（通常 1..N）
      head_index_set 头章目录 index 集合（保存键，番茄 index=realChapterOrder）
      f_by_num       章号 → 番茄目录条目 dict（连接键；镜像 wmap 同以章号为键）
    """
    head_nums, f_by_num = [], {}
    for ch in f_catalog:
        num = _parse_chapter_num(ch.get("title", ""))
        if num is None:
            continue
        f_by_num[num] = ch
        head_nums.append(num)
        if len(head_nums) >= head_n:
            break
    head_index_set = {f_by_num[n]["index"] for n in head_nums if n in f_by_num}
    return head_nums, head_index_set, f_by_num


def _title_alignment(head_nums, f_by_num, wmap):
    """前十章标题对齐：以「期望头章数」（=前 HEAD_N 个番茄编号章）为分母，缺章计未通过。

    判一致需满足 镜像携带头章占比 ≥ TITLE_OK_RATIO **且** 标题通过占比 ≥ TITLE_OK_RATIO——
    镜像只覆盖部分头章（如从第 6 章起、缺第 1-5 章）会因分母大而判不一致。
    双侧标题都为空（如目录仅「第N章」无章名）视为相等；一侧空一侧有不算通过。"""
    expected = max(1, len(head_nums))
    both = [n for n in head_nums if n in f_by_num and n in wmap]
    passed = 0
    for n in both:
        ft = _chapter_suffix(f_by_num[n].get("title", ""))
        mt = _chapter_suffix(wmap[n].get("title", ""))
        if not ft and not mt:
            passed += 1
            continue
        if not ft or not mt:
            continue
        if difflib.SequenceMatcher(None, ft, mt).ratio() >= TITLE_OK_RATIO:
            passed += 1
    ok = (len(both) / expected >= TITLE_OK_RATIO
          and passed / expected >= TITLE_OK_RATIO)
    return {"both": len(both), "passed": passed, "expected": expected, "ok": ok}


def _fetch_full_fanqie(fanqie, rid, f_by_num, head_nums, fanqie_bodies, limit=HEAD_N):
    """抓番茄头章正文做核对（前十章全量，非抽样）；≥FREE_FULL_MIN_CHARS 才算全文，收满即停。

    fanqie_bodies 跨源复用（num → 正文），避免对多个源重复抓同一章。"""
    for n in head_nums:
        if len(fanqie_bodies) >= limit:
            break
        if n in fanqie_bodies or n not in f_by_num:
            continue
        try:
            body = fanqie.download_chapter(rid, f_by_num[n]["id"])
            if _cjk_len(body) >= FREE_FULL_MIN_CHARS:
                fanqie_bodies[n] = body
        except Exception as e:
            logger.warning(f"fanqie head ch {n} fetch failed: {e}")
    return fanqie_bodies


def _content_sample(fanqie_bodies, src, mirror_cache):
    """对「番茄有全文 且 镜像 wmap 有该章号」的头章做正文全量比对（前十章，非抽样）。

    镜像正文经 crawler.download_chapter → _clean_text 已做广告行筛查，比对即「筛后一致」。
    相似度 ≥CONTENT_OK_RATIO 计 matched；已比对正文写入 mirror_cache（填充阶段复用，避免重下）。
    返回 (matched, total)；total==0（番茄头章也全锁、无法比对正文）由调用方退化为标题对齐信任。"""
    matched = total = 0
    for n, f_body in fanqie_bodies.items():
        mch = src["wmap"].get(n)
        if not mch:
            continue
        try:
            m_body = src["crawler"].download_chapter(
                src["w_meta"]["book_id"], mch["chapter_id"])
        except Exception as e:
            logger.warning(f"mirror {src['site']} head ch {n} fetch failed: {e}")
            continue
        if not m_body:
            continue
        mirror_cache[n] = m_body
        total += 1
        ratio = difflib.SequenceMatcher(None, _norm(f_body), _norm(m_body)).ratio()
        if ratio >= CONTENT_OK_RATIO:
            matched += 1
    return matched, total


def _verify_source_head(fanqie, rid, head_nums, f_by_num, src, fanqie_bodies, mirror_cache):
    """前十章核对单源 → {"site","consistent","align","matched","total"}。

    consistent = 标题对齐 ok 且（无法正文比对则只看标题 / 前十章正文全部通过）。"""
    align = _title_alignment(head_nums, f_by_num, src["wmap"])
    _fetch_full_fanqie(fanqie, rid, f_by_num, head_nums, fanqie_bodies)
    matched, total = _content_sample(fanqie_bodies, src, mirror_cache)
    consistent = align["ok"] and (total == 0 or matched == total)
    return {"site": src["site"], "consistent": consistent, "align": align,
            "matched": matched, "total": total}


def _candidate_sources(sources, prefer_site="", site=""):
    """核对候选源顺序：用户显式源（prefer_site，其次 site）置顶，其余按 coverage 降序。"""
    items = list(sources.values())
    head = []
    if prefer_site and prefer_site in sources:
        head.append(sources[prefer_site])
    elif site and site in sources:
        head.append(sources[site])
    rest = [s for s in items if s not in head]
    rest.sort(key=lambda s: s["coverage"], reverse=True)
    return head + rest


def _best_mirror(sources):
    """coverage 最高的镜像源记录（选源前旧口径用；核对流程在其后按需覆写）。"""
    return max(sources.values(), key=lambda s: s["coverage"]) if sources else None


def _merged_meta(f_meta, total, platform, main_site="", main_src=None, verdict=None):
    """构建 merged 书目 info.json 的 meta（含前十章核对结论字段）。

    verdict：{head_verified, head_site}（None/缺省 → False/""）。"""
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
    meta["head_verified"] = bool(verdict and verdict.get("head_verified"))
    meta["head_site"] = (verdict or {}).get("head_site", "") or ""
    return meta


def download_book_merged(title: str = "", url: str = "", book_id: str = "",
                         site: str = "wodushu", chapters: int = 0,
                         start_chapter: int = 1, end_chapter: int = 0,
                         download_delay: float = 0.5, on_progress=None,
                         platform: str = "merged", prefer_site: str = "",
                         mirrors: Optional[list] = None, on_step=None):
    """综合抓取 → storage/novels/{书名}/。返回 (meta, dl)；dl 含 folder/chapters/already/sources。

    on_progress(phase,current,total,message)  走进度条/滚动消息；
    on_step(label,status,detail)              走分步清单（status: running|ok|warn|error）。

    prefer_site：用户显式下载源（'wodushu'/'bookszw'）时优先该源排主源（仍按章号多源补缺）；
    空串=自动择优。不影响单源/番茄路径，纯 merged 主源偏好。
    mirrors：勾选的下载源清单（如 ['wodushu']）→ 只探测这些源，缺章在勾选源间补；
    None/空 = 全部 MIRROR_SOURCES 自动探测（与旧行为一致）。
    """
    source = url or book_id or title
    if not source:
        raise RuntimeError("请提供书名 / 番茄 book_id / 镜像站 URL")
    from plugins.fanqie_scout import FanqieCrawler
    from plugins.webnovel_scraper import WebnovelCrawler

    def _step(label, status="running", detail=""):
        if on_step:
            try:
                on_step(label, status, detail)
            except Exception as e:
                logger.warning(f"on_step failed: {e}")
                raise

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

    # ── 1) 番茄元数据 + 权威目录（封面/简介/目录；尽力） ──
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
    if f_meta and f_catalog:
        _step("解析番茄", "ok", f"{f_meta['title']} · 封面/简介/目录 {len(f_catalog)} 章")

    # ── 2) 书目提前建：番茄元数据一到即建 info.json（含封面；/scout 立即显示书卡）。
    #      已有书（增量/断点）绝不重置；最终 meta 在 _save_merged 里再覆写。 ──
    if platform == "merged" and f_meta and f_catalog:
        try:
            from plugins.novel_storage import save_novel, NOVELS_DIR, _safe_name
            _folder = _safe_name(f_meta["title"])
            if not (NOVELS_DIR / _folder / "info.json").exists():
                save_novel(platform, _merged_meta(
                    f_meta, len(f_catalog), platform,
                    main_site=(prefer_site or site or ""), main_src=None), [])
                _step("创建书目", "ok", _folder)
        except Exception as e:
            logger.warning(f"early create novel entry failed: {e}")
            _step("创建书目", "warn", "提前建目录失败（随保存补齐）")

    # ── 3) 多镜像源并行探查（mirrors 限定探测集合；每个源独立 Session） ──
    from plugins.webnovel_scraper import MIRROR_SOURCES
    import concurrent.futures as _cf
    from urllib.parse import urlparse as _up
    # 番茄目录条目无 num 键 → 用标题「第N章」号（与镜像 wmap 同口径），修复旧 coverage 恒空
    fanqie_nums = {_parse_chapter_num(c.get("title", "")) for c in f_catalog}
    fanqie_nums.discard(None)
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

    probe_keys = list(MIRROR_SOURCES)
    if mirrors:
        wanted = [k for k in MIRROR_SOURCES if k in mirrors]
        if wanted:
            probe_keys = wanted
    sources = {}
    with _cf.ThreadPoolExecutor(max_workers=min(3, max(1, len(probe_keys)))) as _ex:
        # 每个被探查的源都回报（找到→章数；未找到/失败→warn），方便下载框看全各源结果
        for sn, _r in zip(probe_keys, _ex.map(_resolve_source, probe_keys)):
            if _r and _r["wmap"]:
                sources[_r["site"]] = _r
                _step(f"镜像解析:{_r['site']}", "ok", f"主书 {len(_r['wmap'])} 章可候选")
                if on_progress:
                    on_progress("search", 1, 1,
                                f"镜像源 {_r['site']}: 主书 {len(_r['wmap'])} 章可用")
            else:
                _step(f"镜像解析:{sn}", "warn", "未找到该书 / 解析失败")

    # ── 给镜像 URL 时反向补番茄元数据（用镜像书名；番茄失败但镜像命中的回补） ──
    if not f_meta and sources:
        best = _best_mirror(sources)
        if best:
            try:
                n = fanqie.search_novel(best["w_meta"].get("title", ""))
                if n:
                    _fmeta_from(n)
                    _step("解析番茄", "ok",
                          f"{f_meta['title']} · 目录 {len(f_catalog)} 章（镜像书名回补）")
            except Exception as e:
                logger.warning(f"fanqie backfill by mirror title failed: {e}")

    if not f_meta and not sources:
        raise RuntimeError(f"未找到该书（番茄与镜像站均解析失败）: {source}")

    if f_meta:
        return _save_merged(f_meta, f_catalog, sources, site, chapters,
                            start_chapter, end_chapter, download_delay, on_progress,
                            platform, prefer_site,
                            fanqie=fanqie, fanqie_rid=f_meta["book_id"], on_step=on_step)
    return _save_mirror_only(_best_mirror(sources), site, chapters, start_chapter,
                             end_chapter, download_delay, on_progress, on_step=on_step)


def _save_merged(f_meta, f_catalog, sources, site, chapters, start, end, delay,
                 on_progress, platform, prefer_site="", fanqie=None,
                 fanqie_rid="", on_step=None):
    """番茄目录权威 + 前十章核对选源 + 镜像按章号补全文；头章免费部分存番茄权威正文。

    prefer_site：用户显式源优先排主源（偏好；未解析/缺章时回退核对结论并多源补缺）。"""
    from plugins.novel_storage import save_novel, save_chapter, NOVELS_DIR, _safe_name

    def _step(label, status="running", detail=""):
        if on_step:
            try:
                on_step(label, status, detail)
            except Exception as e:
                logger.warning(f"on_step failed: {e}")
                raise

    total = len(f_catalog)
    if total == 0:
        raise RuntimeError(f"番茄目录为空: {f_meta['title']}")

    # 免费头章区域：前 HEAD_N 个「第N章」及其之前的序章/楔子（番茄免费正文一般在此范围）
    head_span = set()
    numbered = 0
    for c in sorted(f_catalog, key=lambda c: int(c.get("index") or 0)):
        head_span.add(int(c["index"]))
        if _parse_chapter_num(c.get("title", "")) is not None:
            numbered += 1
        if numbered >= HEAD_N:
            break

    # 无镜像源（找不到可用镜像）：只下载番茄免费头章（前十章），不整本落空占位——
    # 番茄后段锁定、无镜像正文可填，继续往后下载没有意义。
    no_mirror = not sources
    if no_mirror:
        selected = [c for c in f_catalog if int(c.get("index") or 0) in head_span]
    else:
        start = max(1, int(start or 1))
        if int(chapters or 0) > 0:
            e = int(end or 0)
            e = (start + int(chapters) - 1) if e <= 0 else e
        else:
            e = int(end or 0) or total
        e = min(e, total)
        selected = [c for c in f_catalog if start <= int(c.get("index") or 0) <= e]
    if not selected:
        raise RuntimeError(f"无可下载章节: {f_meta['title']}（目录 {total} 章）")

    # 断点续下：只把**有正文**的章节算作已下载（空占位不阻断补全）
    folder = _safe_name(f_meta["title"])
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
        return _merged_meta(f_meta, total, platform, main_site=site), \
            {"folder": folder, "chapters": 0, "already": True, "sources": "merged"}
    if on_progress:
        if no_mirror:
            on_progress("search", 1, 1,
                        f"无镜像源：仅下载番茄免费头章 {len(pending)} 章（不整本落占位）")
        else:
            on_progress("search", 1, 1,
                        f"合并抓取: {f_meta['title']}（番茄目录 {total} 章，本次 {len(pending)} 章）")

    # 头章元数据：连接键=标题「第N章」号（跳过序章/楔子）；供前十章核对用
    head_nums, head_index_set, f_by_num = _fanqie_head(f_catalog)

    # ── 4) 前十章核对选源：每个已找到的源都做全量核对并上报 → 有「完全一致」源取第一个；
    #      全不一致才回退 matched/coverage 最佳源（镜像正文广告行过滤天然生效） ──
    main_src = None
    head_verified, head_site = False, ""
    mirror_cache, fanqie_bodies = {}, {}
    if sources:
        cand = _candidate_sources(sources, prefer_site, site)
        scored = []
        first_consistent = None
        for src in cand:
            mc = {}
            ver = _verify_source_head(fanqie, fanqie_rid, head_nums, f_by_num,
                                      src, fanqie_bodies, mc)
            scored.append((src, ver, mc))
            detail = (f"标题 {ver['align']['passed']}/{ver['align']['expected']} · "
                      f"正文比对 {ver['matched']}/{ver['total']}")
            if ver["consistent"]:
                if first_consistent is None:
                    first_consistent = (src, ver, mc)
                _step(f"校验:{src['site']}", "ok", f"前十章一致（{detail}）")
            else:
                _step(f"校验:{src['site']}", "error", f"不一致（{detail}）")
        if first_consistent:
            src, _v, mc = first_consistent
            main_src, head_verified, head_site = src, True, src["site"]
            mirror_cache = mc
            _step("选择来源", "ok", f"用一致源 {main_src['site']}")
        elif scored:
            # 全不一致 → 回退：matched 最多、其次 coverage 最高（镜像正文已广告行过滤）
            src, _v, mc = max(scored, key=lambda t: (t[1]["matched"], t[0]["coverage"]))
            main_src, head_site = src, src["site"]
            mirror_cache = mc
            _step("选择来源", "warn",
                  f"无完全一致源，回退 {src['site']}（镜像正文已广告行过滤）")
    else:
        _step("选择来源", "warn", "无镜像源：仅下载番茄免费头章（前十章），不整本落占位")

    main_site = main_src["site"] if main_src else site
    meta = _merged_meta(f_meta, total, platform, main_site=main_site, main_src=main_src,
                        verdict={"head_verified": head_verified, "head_site": head_site})

    # 多源顺序：主源在前，其余按 coverage 降序（缺章跨源补）
    source_order = ([main_src] if main_src else []) + \
        sorted([s for s in sources.values() if s is not main_src],
               key=lambda s: s["coverage"], reverse=True)

    save_novel(platform, meta, [])   # 重建 info.json（书目先前已建则原地更新 + head 字段）
    downloaded = 0
    _step("下载正文", "running", f"{len(pending)} 章")
    for i, ch in enumerate(pending):
        idx = ch["index"]
        fnum = _parse_chapter_num(ch.get("title", ""))
        content = ""
        # 镜像正文：先试核对阶段已比对缓存，否则按源顺序下载（内部 _clean_text 广告行过滤）
        if fnum is not None:
            if mirror_cache.get(fnum):
                content = mirror_cache[fnum]
            else:
                for src in source_order:
                    mch = src["wmap"].get(fnum)
                    if mch:
                        content = src["crawler"].download_chapter(
                            src["w_meta"]["book_id"], mch["chapter_id"])
                        if content:
                            break
        # 头章（番茄免费区域，含前导序章/楔子）→ 番茄权威全文优先（无广告）：仅当番茄正文
        # 本身 ≥ FREE_FULL_MIN_CHARS（是全文、非锁章预览）且 ≥ 镜像正文 × FULL_LEN_RATIO
        # 才取番茄正文；否则用镜像正文（核对一致即与番茄同源全文）。
        if idx in head_span and fanqie and fanqie_rid and ch.get("id"):
            f_body = fanqie_bodies.get(fnum, "") if fnum is not None else ""
            if not f_body:
                try:
                    f_body = fanqie.download_chapter(fanqie_rid, ch["id"])
                    if fnum is not None and f_body \
                            and _cjk_len(f_body) >= FREE_FULL_MIN_CHARS:
                        fanqie_bodies[fnum] = f_body
                except Exception as ex:
                    logger.warning(f"fanqie head fill ch {idx} failed: {ex}")
                    f_body = ""
            if f_body and _cjk_len(f_body) >= FREE_FULL_MIN_CHARS \
                    and _cjk_len(f_body) >= _cjk_len(content) * FULL_LEN_RATIO:
                content = f_body
        # 番茄目录权威：镜像无此章也落 title-only 占位（无镜像源时只取头章、空正文不落盘）
        if content or not no_mirror:
            save_chapter(platform, folder, {
                "index": idx, "title": ch["title"], "content": content or "",
                "word_count": _cjk_len(content or ""),
            })
        if content:
            downloaded += 1
        if on_progress:
            on_progress("download", i + 1, len(pending), ch["title"][:30])
        if i < len(pending) - 1:
            time.sleep(delay)
    _step("下载正文", "ok", f"{downloaded} 章（本次新增）")
    if on_progress:
        on_progress("download", len(pending), len(pending),
                    f"下载完成 {downloaded}章（本次新增）")
    return meta, {"folder": folder, "chapters": downloaded, "already": False, "sources": "merged"}


def _save_mirror_only(primary, site, chapters, start, end, delay, on_progress,
                      on_step=None):
    """番茄解析失败 → 回退覆盖最全的镜像源元数据 + 镜像主书。"""
    if on_step:
        try:
            on_step("回退镜像站", "warn", f"番茄解析失败，改用镜像源 {primary['site']}")
        except Exception as e:
            logger.warning(f"on_step failed: {e}")
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
            "word_count": _cjk_len(content),
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

    def step(label, status="running", detail=""):
        print(f"  · [{status}] {label} {detail}".strip(), flush=True)

    try:
        meta, dl = download_book_merged(
            title=args.title, url=args.url, book_id=args.book_id, site=args.site,
            chapters=args.chapters, download_delay=args.delay,
            on_progress=prog, on_step=step)
        print(f"✅ {meta['title']} 合并完成 {dl['chapters']}章 → storage/novels/{dl['folder']}/")
    except Exception as e:
        print(f"❌ {e}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
