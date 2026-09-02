# -*- coding: utf-8 -*-
"""每源「剔除脚本」：从番茄 vs 镜像前十章正文的逐字符 diff 中自动学习该源的噪声
（站点广告 / 分页标记 / 多余字符等），生成可复用的清洗规则并持久化。

- 规则载体与 webnovel_scraper 既有 `ad_replace`（正则替换）一致，注入 crawler.cfg 后
  `_clean_text` / `drop_line_re` 在下载正文时自动应用。
- 持久化到 storage/site_clean_rules.json（site → {ad_replace, drop_line_re, learned_at}），
  下次抓取同一源直接复用，无需重新学习。

学习思路：
  番茄正文（干净）与镜像正文逐字符对齐（difflib.SequenceMatcher, autojunk=False），
  镜像中「番茄没有」的连续片段即候选噪声。多章样本里稳定出现的噪声 → 置信规则。
"""
import json
import os
import re
import time
import difflib
import logging

logger = logging.getLogger(__name__)

STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage")
RULES_FILE = os.path.join(STORAGE_DIR, "site_clean_rules.json")

# 噪声片段转规则的安全下限：太短的片段（如单字符）风险高（正文可能真有该字符），
# 仅当它在多章里以「孤立」形态出现才收；过长的片段（整段替换）降级为行级 drop。
MIN_NOISE_LEN = 2
MAX_ADREPLACE_LEN = 60        # 超过此长度的噪声不直接做 ad_replace（可能是整段差异，行级 drop 更稳）
CLEAN_NUM = 5                 # 默认用前 CLEAN_NUM 个可比对头章做噪声学习
_MIN_OCCUR = 2                # 至少在前几章里出现 ≥ 此次数才收为稳定规则

# 内建噪声特征：孤立标点 / 分页标记 / 书站书签语，与「正文合法用法」区分
_ISOLATED_RE = re.compile(
    r"^[^\w\u4e00-\u9fff]{1,4}$"        # 纯符号短串（如 『 、 》 、 】
)
_PAGING_RE = re.compile(
    r"^[（(]?\s*第?\d+\s*[/／]\s*\d+\s*页?[)）]?$"          # （第1/2页）
)
_CHAPTER_TAIL_RE = re.compile(
    r"^第[0-9一二三四五六七八九十百千零两]+章.*[）)]$"        # 第N章 … 标题)
)


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def _noise_fragments(f_body, m_body):
    """番茄 vs 镜像单章正文 → 镜像多出的噪声片段列表（逐字符对齐）。"""
    fn, mn = _norm(f_body), _norm(m_body)
    if not fn or not mn:
        return []
    sm = difflib.SequenceMatcher(None, fn, mn, autojunk=False)
    frags = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            frag = mn[j1:j2]
            if frag:
                frags.append(frag)
    return frags


def _to_rule(frag):
    """噪声片段 → (kind, pattern) 候选规则。kind: ad_replace / drop_line。

    只有「形态上像噪声」的片段才生成规则：
      - 纯符号短串（孤立标点）→ ad_replace 精确删除
      - 分页标记（第x/y页）→ ad_replace 精确删除
      - 含典型广告词的句子 → ad_replace 删除
      - 长片段但含广告词 → drop_line（整行丢弃）
      其余（可能是正文差异/番外残留）不生成规则，避免误删正文。"""
    frag = frag.strip()
    if not frag:
        return None
    # 分页标记
    if _PAGING_RE.match(frag):
        return ("ad_replace", re.escape(frag), "")
    # 孤立标点短串
    if 1 <= len(frag) <= 4 and _ISOLATED_RE.match(frag):
        return ("ad_replace", re.escape(frag), "")
    # 广告词特征
    ad_words = ["请收藏", "记住本站", "最快更新", "天才一秒", "笔趣阁",
                "最新网址", "加入书签", "章节错误", "点击报错", "一秒记住",
                "手机用户请浏览", "阅读最新章节", "首发", "地址", "域名"]
    has_ad = any(w in frag for w in ad_words)
    if not has_ad:
        return None
    if len(frag) <= MAX_ADREPLACE_LEN:
        return ("ad_replace", re.escape(frag), "")
    return ("drop_line", re.escape(frag), "")


def learn_noise_rules(fanqie_bodies, src, head_nums, f_by_num, existing=None,
                      body_cache=None):
    """对单源学习噪声规则：用前 CLEAN_NUM 个「番茄有全文且镜像覆盖」的头章做 diff。

    fanqie_bodies: {章号: 番茄正文}
    src: 源 dict（含 crawler / wmap）
    existing: 该源已持久化的规则（增量合并，已学过的章节跳过）
    body_cache: {章号: 镜像正文}（前十章核对阶段已下载的镜像正文，复用避免重复请求）
    返回 {"ad_replace": {...}, "drop_line": [...]}（可注入 crawler.cfg）。"""
    from plugins.book_fetch import _cjk_len, FREE_FULL_MIN_CHARS
    frag_counts = {}
    samples = 0
    for n in head_nums:
        if samples >= CLEAN_NUM:
            break
        f_body = fanqie_bodies.get(n)
        mch = src["wmap"].get(n)
        if not f_body or not mch or _cjk_len(f_body) < FREE_FULL_MIN_CHARS:
            continue
        if body_cache and body_cache.get(n):
            m_body = body_cache[n]
        else:
            try:
                m_body = src["crawler"].download_chapter(
                    src["w_meta"]["book_id"], mch["chapter_id"])
            except Exception as e:
                logger.warning("noise learn %s ch%d fetch failed: %s", src["site"], n, e)
                continue
        if not m_body or _cjk_len(m_body) < FREE_FULL_MIN_CHARS:
            continue
        samples += 1
        for frag in _noise_fragments(f_body, m_body):
            frag_counts[frag] = frag_counts.get(frag, 0) + 1

    ad_replace = {}
    drop_lines = []
    for frag, cnt in frag_counts.items():
        if cnt < _MIN_OCCUR:
            continue   # 只在单章出现的片段可能是该章特有，不构成站点级规则
        rule = _to_rule(frag)
        if not rule:
            continue
        kind, pat, rep = rule
        if kind == "ad_replace":
            ad_replace[pat] = rep
        else:
            drop_lines.append(pat)

    # 合并既有规则（保留上次学习的，避免每次覆盖丢失多书共学）
    if existing:
        ex_ad = existing.get("ad_replace") or {}
        ex_dl = existing.get("drop_line") or []
        ad_replace = {**ex_ad, **ad_replace}
        drop_lines = list(dict.fromkeys(list(ex_dl) + drop_lines))
    if not ad_replace and not drop_lines:
        return None
    return {"ad_replace": ad_replace, "drop_line": drop_lines,
            "learned_at": time.time(), "samples": samples}


def load_rules(site=None):
    """加载持久化的规则库；site 为空返回全部。"""
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {} if site is None else None
    if site is None:
        return data
    return data.get(site)


def persist_rules(site, rules):
    """持久化某源规则到 site_clean_rules.json。"""
    try:
        os.makedirs(STORAGE_DIR, exist_ok=True)
        data = load_rules() or {}
        data[site] = rules
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning("persist rules %s failed: %s", site, e)
        return False


def apply_rules_to_crawler(site, crawler):
    """把持久化的该源规则注入 crawler.cfg（ad_replace / drop_line_re），下载时自动生效。"""
    rules = load_rules(site)
    if not rules:
        return False
    return inject_rules(crawler, rules)


def inject_rules(crawler, rules):
    """把内存中的规则集直接注入 crawler.cfg（不经过磁盘），供「刚学到就立刻生效」场景。"""
    if not rules:
        return False
    ad = rules.get("ad_replace") or {}
    dl = rules.get("drop_line") or []
    if ad:
        cfg_ad = dict(crawler.cfg.get("ad_replace") or {})
        cfg_ad.update({k: v for k, v in ad.items()})
        crawler.cfg["ad_replace"] = cfg_ad
    if dl:
        cfg_dl = list(crawler.cfg.get("drop_line_re") or [])
        cfg_dl = list(dict.fromkeys(cfg_dl + dl))
        crawler.cfg["drop_line_re"] = cfg_dl
    return True
