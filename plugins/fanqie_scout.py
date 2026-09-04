"""
番茄小说侦察兵（Fanqie Scout Agent）
从番茄平台爬取热榜小说 → LLM拆解 → 沉淀到四大库

流程：
  热榜发现 → 下载前N章 → 逐书分析 → 提取桥段/大纲/笑点/内涵 → 入库

⚠️ 合规声明：
  本模块仅供个人学习、研究网文结构技巧使用。请遵守目标网站的服务条款与
  相关法律法规：
  - 番茄小说等内容平台的服务协议普遍禁止自动化数据采集，请勿用于商业用途
  - 请勿大量下载并二次传播受著作权保护的正文内容，分析应以「模式/结构/
    桥段」等抽象技巧为主，避免全文存储与转载
  - PUA 字体解码属于对技术保护措施的绕过，请仅用于个人学习研究
  使用本模块产生的任何法律风险由使用者自行承担。
"""
import json
import os
import re
import time
import logging
from plugins import font_decoder
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import requests

logger = logging.getLogger("fanqie-scout")


# ═══════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════

@dataclass
class NovelInfo:
    """小说基本信息（多平台热榜统一字段；platform/rank 为热榜条目扩展，默认值兼容旧调用）"""
    book_id: str
    title: str
    author: str
    genre: str = ""
    sub_genre: str = ""
    word_count: int = 0
    chapter_count: int = 0
    hot_score: int = 0
    intro: str = ""
    url: str = ""
    cover: str = ""
    platform: str = ""
    rank: int = 0


@dataclass
class ScoutResult:
    """一次侦察的完整结果"""
    source_books: list[NovelInfo] = field(default_factory=list)
    new_plots: list[dict] = field(default_factory=list)
    new_structures: list[dict] = field(default_factory=list)
    new_gags: list[dict] = field(default_factory=list)
    downloaded_chapters: int = 0
    analysis_cost: float = 0.0


# ═══════════════════════════════════════
# 爬虫核心
# ═══════════════════════════════════════

# 封面提取：SSR/API 页面结构多变，递归找常见封面键（限深度，防误伤大对象）
_COVER_KEYS = ("thumbUri", "thumb_uri", "coverUrl", "book_cover", "cover")


def _find_cover_url(obj, depth=0):
    """防御式在 dict/list 里找封面 URL：命中 http 开头字符串即返回，未命中返回空串。"""
    if depth > 4:
        return ""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _COVER_KEYS and isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            r = _find_cover_url(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _find_cover_url(it, depth + 1)
            if r:
                return r
    return ""


def _load_fanqie_cookie() -> str:
    """读取番茄登录 Cookie（可空）：环境变量 FANQIE_COOKIE 非空优先，其次 storage/fanqie_cookie.txt。
    带登录 Cookie 请求时，锁定章节（isChapterLock）的 SSR 可能返回全文而非 200 字预览。
    记录注入来源，便于诊断「锁章预览」是缺 Cookie 还是用了过期 Cookie。"""
    env = (os.environ.get("FANQIE_COOKIE", "") or "").strip()
    if env:
        logger.info("番茄 Cookie 来源：FANQIE_COOKIE 环境变量（%d 字符）", len(env))
        return env
    try:
        p = Path("storage") / "fanqie_cookie.txt"
        if p.exists():
            cookie = p.read_text(encoding="utf-8").strip()
            if cookie:
                logger.info("番茄 Cookie 来源：storage/fanqie_cookie.txt")
            return cookie
    except Exception:
        pass
    return ""


# 「阅读榜·全品类」合成榜 key 别名：番茄网页榜单没有官方全品类/完本榜（只有 男频/女频 ×
# 阅读榜(30万字+)/新书榜(<30万字)，mold 1=新书、2=阅读），完整阅读榜 = 按性别拉全部品类
# 阅读榜（/api/rank/category/list，每品类官方 top，单次 limit=100 可回满）跨品类去重合并。
READ_ALL_KEYS = frozenset({
    "阅读榜全品类", "全品类阅读榜", "阅读榜·全品类", "全部阅读榜", "全品类", "阅读榜",
})
# 显式指定性别的全品类阅读榜别名 → 目标 gender 数值（番茄榜单 gender：1=男频 2=女频）
GENDER_READ_ALL_KEYS = {
    "男频阅读榜": 1, "男频": 1,
    "女频阅读榜": 2, "女频": 2,
}

# 番茄正文视为「免费全文」的最少字数（SVIP 锁定章预览常低于此 → 不当正文落盘，
# 与 book_fetch.FREE_FULL_MIN_CHARS 同口径；独立常量避免模块循环导入）
FANQIE_FREE_MIN_CHARS = 300

from core.text_utils import count_prose_units as _count_prose_units  # noqa: E402


class FanqieCrawler:
    """番茄小说爬虫"""

    BASE_URL = "https://fanqienovel.com"
    API_BASE = "https://fanqienovel.com/api"

    # 超时设置：搜索类请求允许较短重试，详情/下载类给足时间避免网络抖动误判
    SEARCH_TIMEOUT = 5
    DETAIL_TIMEOUT = 15

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://fanqienovel.com/",
    }

    # 番茄的品类映射（旧 book_list 接口的题材 id → 中文名，抓取链路仍在用）
    GENRE_MAP = {
        1: "玄幻", 2: "都市", 3: "历史", 4: "武侠",
        5: "科幻", 6: "悬疑", 7: "游戏", 8: "轻小说",
        9: "短篇", 10: "现实",
    }

    # 番茄榜单分类映射（榜单页 /rank/{gender}_{rankMold}_{category_id}；UI 题材中文名 → 榜单分类 id）
    GENRE_CATEGORY = {
        "玄幻": "258",    # 传统玄幻
        "都市": "261",    # 都市日常
        "科幻": "8",      # 科幻末世
        "历史": "273",    # 历史古代
        "仙侠": "1140",   # 东方仙侠
        "西方奇幻": "1141",
    }
    # 「全部」聚合使用的头部分类（各取前 N 合并按在读量排序）
    AGGREGATE_CATEGORIES = ["258", "261", "8", "273", "1140", "1141"]
    # 分类 id → 中文名（榜单页 rankCategoryTypeList 可动态取全量；此处兜底常用）
    CATEGORY_NAMES = {
        "258": "传统玄幻", "261": "都市日常", "8": "科幻末世",
        "273": "历史古代", "1140": "东方仙侠", "1141": "西方奇幻",
    }

    def __init__(self, cache_dir: str = "storage/fanqie_cache", verify: bool = True):
        # verify 默认校验 TLS 证书（安全）；旧证书环境可显式传 verify=False
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.session.verify = verify
        self.session.trust_env = False  # 不用系统代理，直连访问
        if not verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # 登录 Cookie 注入：带番茄账号 Cookie 时，锁定章节（isChapterLock）SSR 返回全文
        cookie = _load_fanqie_cookie()
        if cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    self.session.cookies.set(k.strip(), v.strip(), domain=".fanqienovel.com")

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._decoder = None  # lazy init

    def _init_decoder(self):
        if self._decoder is None:
            from plugins.font_decoder import FanqieDecoder
            self._decoder = FanqieDecoder(verify=self.session.verify)

    def discover_hot(self, key: str = "", count: int = 10, gender: int = 1,
                     rank_mold: int = 2) -> list[NovelInfo]:
        """番茄热榜。key：榜单分类 id（如 '258'）或题材中文名（如 '玄幻'）；空/'全部' → 聚合头部分类。

        榜单页 /rank/{gender}_{rankMold}_{key} 的 SSR __INITIAL_STATE__.rank.book_list 解析，
        书名/简介为 PUA 字体加密，经 FanqieDecoder.decode_content 还原为汉字。
        """
        key = (key or "").strip()
        g = gender
        if key in GENDER_READ_ALL_KEYS:        # 「男频阅读榜/女频」等显式性别别名 → 锁定性别后走全品类阅读榜
            g = GENDER_READ_ALL_KEYS[key]
            key = "阅读榜全品类"
        if key in READ_ALL_KEYS:               # 「阅读榜·全品类」→ 整性别全品类阅读榜合并（无官方单榜）
            try:
                return self.discover_read_rank_all(gender=g) or []
            except Exception as e:
                logger.warning(f"read-rank-all failed (gender={g}): {e}")
                return []
        if key and key != "全部":
            if not key.isdigit():
                key = self.GENRE_CATEGORY.get(key, "")
            if key:
                try:
                    return self._rank_by_category(key, count, gender, rank_mold) or []
                except Exception as e:
                    logger.warning(f"rank hot list failed (key={key}): {e}")
                    return []
        # 空/'全部'/未识别题材 → 聚合。男频沿用头部分类口径；女频无男频那套头部分类，
        # 且女频全品类书量本就不大 → 女频「全部」直接 = 女频全品类阅读榜
        try:
            if gender == 2:
                rows = self.discover_read_rank_all(gender=2)
                return rows[:count] if (count and count > 0) else rows
            return self._rank_aggregate(count, gender, rank_mold) or []
        except Exception as e:
            logger.warning(f"aggregate hot list failed: {e}")
            return []

    def _get_decoder(self) -> Optional[font_decoder.FanqieDecoder]:
        """懒初始化字体解码器（书名/简介/正文统一用它还原 PUA 密文）。"""
        self._init_decoder()
        return self._decoder

    def _extract_ssr(self, html: str) -> Optional[dict]:
        """提取页面 __INITIAL_STATE__ JSON。

        用 raw_decode 精确定位对象结尾（SSR 在 JS 函数里，`};` 后不一定紧跟 </script>）；
        页面偶发 undefined 字面量（无数据字段）先替换为 null 保证可解析。
        """
        i = html.find("window.__INITIAL_STATE__=")
        if i < 0:
            return None
        j = html.find("{", i)
        if j < 0:
            return None
        text = re.sub(r"\bundefined\b", "null", html[j:])
        try:
            obj, _ = json.JSONDecoder().raw_decode(text)
            return obj
        except Exception as e:
            logger.warning(f"SSR parse failed: {e}")
            return None

    def _rank_by_category(self, category_id: str, count: int, gender: int = 1,
                          rank_mold: int = 2) -> list[NovelInfo]:
        """拉单个榜单分类页，解析 rank.book_list → NovelInfo（排名/在读量/解码书名简介）。"""
        url = f"{self.BASE_URL}/rank/{gender}_{rank_mold}_{category_id}"
        resp = self.session.get(url, timeout=self.DETAIL_TIMEOUT,
                                headers={"Accept": "text/html,application/xhtml+xml"})
        if resp.status_code != 200:
            logger.warning(f"rank page {url} -> {resp.status_code}")
            return []
        ssr = self._extract_ssr(resp.text)
        if not ssr:
            return []
        rank = ssr.get("rank") or {}
        items = rank.get("book_list") or []
        # 从页面分类清单动态补全 id→name（新分类兜底）
        names = dict(self.CATEGORY_NAMES)
        for grp in ((rank.get("rankCategoryTypeList") or {}).get("male") or []) + \
                   ((rank.get("rankCategoryTypeList") or {}).get("female") or []):
            if grp.get("id"):
                names[str(grp["id"])] = grp.get("name", "")
        return [self._novel_from_rank(item, category_id, names) for item in items[:count]]

    def _rank_aggregate(self, count: int, gender: int = 1, rank_mold: int = 2) -> list[NovelInfo]:
        """「全部」聚合：头部分类各取前 5，去重后按在读量排序取 count（单分类失败不阻断）。"""
        merged: list[NovelInfo] = []
        seen: set[str] = set()
        for cat in self.AGGREGATE_CATEGORIES:
            try:
                for n in self._rank_by_category(cat, 5, gender, rank_mold):
                    if n.book_id in seen:
                        continue
                    seen.add(n.book_id)
                    merged.append(n)
            except Exception as e:
                logger.warning(f"aggregate category {cat} failed: {e}")
        merged.sort(key=lambda n: n.hot_score, reverse=True)
        # 跨分类合并后按在读量重排 rank（各分类内排名在此处无全局意义）
        for i, n in enumerate(merged[:count]):
            n.rank = i + 1
        return merged[:count]

    def discover_read_rank_all(self, gender: int = 1, per_cat_cap: int = 300) -> list[NovelInfo]:
        """整性别「阅读榜·全品类」合并榜（无官方单榜的合成口径）。

        番茄阅读榜 = rank_mold 2；榜单页按品类划分（男 19 品类 / 女 18 品类，每品类官方
        top100 左右）。此处遍历该性别 list_rankings 的全品类，逐类走
        /api/rank/category/list 翻页拉阅读榜（单次 limit=100 即可回满该品类），
        PUA 书名/作者/简介同 _novel_from_rank 解码；跨品类按 book_id 去重后按在读量
        (read_count) 降序重排为整频完整阅读榜。单分类失败跳过不阻断。
        """
        label = "male" if gender == 1 else "female"
        cats = self.list_rankings(gender=label) or []
        if not cats:
            logger.warning("discover_read_rank_all: 品类清单为空")
            return []
        names = {str(c.get("id")): c.get("name", "") for c in cats if c.get("id")}
        first_cat = next(iter(names), "258")
        api = f"{self.BASE_URL}/api/rank/category/list"
        hdr = {"Accept": "application/json, text/plain, */*",
               "Referer": f"{self.BASE_URL}/rank/{gender}_2_{first_cat}"}
        merged: dict[str, NovelInfo] = {}
        for cat in cats:
            cid = str(cat.get("id", ""))
            if not cid:
                continue
            try:
                params = dict(app_id=2503, rank_list_type=3, offset=0, limit=100,
                              category_id=cid, rank_version="", gender=gender, rankMold=2)
                offset, total, got = 0, 0, 0
                while True:
                    params["offset"] = offset
                    resp = self.session.get(api, params=params, timeout=self.DETAIL_TIMEOUT,
                                            headers=hdr)
                    if resp.status_code != 200:
                        break
                    try:
                        data = resp.json().get("data") or {}
                    except Exception:
                        break
                    items = data.get("book_list") or []
                    if not items:
                        break
                    total = int(data.get("total_num") or 0)
                    for it in items:
                        n = self._novel_from_rank(it, cid, names)
                        if n.book_id and n.book_id not in merged:
                            merged[n.book_id] = n
                    got += len(items)
                    offset += len(items)
                    # 翻到官方 total / 单品类上限 / 本页已不是满页(limit=100)即停
                    if (total and offset >= total) or got >= per_cat_cap or len(items) < 100:
                        break
                    if offset >= 2000:      # 硬护栏防失控翻页
                        break
            except Exception as e:
                logger.warning(f"read-rank-all category {cid} failed: {e}")
                continue
        rows = sorted(merged.values(), key=lambda n: n.hot_score, reverse=True)
        for i, n in enumerate(rows):
            n.rank = i + 1
        return rows

    def _novel_from_rank(self, item: dict, category_id: str, names: dict) -> NovelInfo:
        """榜单条目 → NovelInfo：书名/简介字体解码，read_count→hot_score，currentPos→rank。"""
        dec = self._get_decoder()
        name = item.get("bookName", "") or ""
        author = item.get("author", "") or ""
        abstract = item.get("abstract", "") or ""
        if dec:
            # 书名/作者/简介均可能为 PUA 字体加密，统一还原
            name = dec.decode_content(name)
            author = dec.decode_content(author)
            abstract = dec.decode_content(abstract)
        cat_id = str(item.get("curent_category_id") or category_id or "")
        return NovelInfo(
            book_id=str(item.get("bookId", "") or ""),
            title=name,
            author=author,
            genre=names.get(cat_id, item.get("categoryV2") or ""),
            word_count=int(item.get("wordNumber", 0) or 0),
            chapter_count=0,
            hot_score=int(item.get("read_count", 0) or 0),
            intro=abstract,
            url=f"{self.BASE_URL}/page/{item.get('bookId','')}",
            cover=item.get("thumbUri", "") or "",
            platform="fanqie",
            rank=int(item.get("currentPos", 0) or 0),
        )

    def list_rankings(self, gender: str = "male", rank_mold: int = 2) -> list[dict]:
        """番茄榜单分类清单（男频/女频），来自任一榜单页 SSR rank.rankCategoryTypeList（各页一致）。"""
        url = f"{self.BASE_URL}/rank/1_{rank_mold}_258"
        try:
            resp = self.session.get(url, timeout=self.DETAIL_TIMEOUT,
                                    headers={"Accept": "text/html,application/xhtml+xml"})
            if resp.status_code != 200:
                return []
            ssr = self._extract_ssr(resp.text)
            if not ssr:
                return []
            rcl = (ssr.get("rank") or {}).get("rankCategoryTypeList") or {}
            items = rcl.get(gender) or []
            return [{"id": str(c.get("id")), "name": c.get("name", "")} for c in items]
        except Exception as e:
            logger.warning(f"list_rankings failed: {e}")
            return []

    def search_novel(self, title: str) -> Optional[NovelInfo]:
        """按书名搜索——Bing搜索 + 页面解析 + fanqie搜索页兜底"""
        import unicodedata
        
        # 生成多级搜索查询
        queries = []
        # 1) 全书名
        queries.append(f"{title} site:fanqienovel.com")
        # 2) 归一化后取前几个词
        clean = unicodedata.normalize("NFKC", title)
        clean = re.sub(r'[：:，,。.！!？?～~··「」【】《》、\s]+', ' ', clean).strip()
        parts = [p for p in clean.split() if len(p) > 1]
        if parts:
            queries.append(f"{' '.join(parts[:2])} site:fanqienovel.com")
            queries.append(f"{parts[0]} site:fanqienovel.com")
            if len(parts) >= 4:
                queries.append(f"{' '.join(parts[:3])} site:fanqienovel.com")
        # 3) 冒号前的前缀词
        for sep in ['：', ':']:
            if sep in title:
                prefix = title.split(sep)[0].strip()
                if prefix and len(prefix) > 1 and (not parts or prefix != parts[0]):
                    queries.insert(1, f"{prefix} site:fanqienovel.com")
                    break

        # 4) 如果只有一个词，尝试不同的搜索策略
        if len(parts) <= 2:
            # 用书名直接搜（不加 site 限制，让Bing自己匹配）
            queries.append(f"{title} 番茄小说")
            # 截取前几个字加 site
            if len(title) >= 4:
                queries.append(f"{title[:4]} site:fanqienovel.com")
        
        # 5) 去重
        seen_q = set()
        unique_queries = []
        for q in queries:
            if q not in seen_q:
                seen_q.add(q)
                unique_queries.append(q)

        # 优先：直接用番茄搜索页（最快，不依赖Bing）
        try:
            import urllib.parse as _up
            search_url = f"https://fanqienovel.com/search/{_up.quote(title)}"
            r = self.session.get(search_url, timeout=self.SEARCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
            if ids:
                seen = set()
                unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
                info = self._get_novel_from_page(unique_ids[0])
                if info and info.title:
                    logger.info(f"番茄搜索页成功: {info.title} (ID={info.book_id})")
                    return info
        except Exception as e:
            logger.debug(f"番茄搜索页: {e}")

        # 并行搜索：所有查询同时发，谁先返回谁赢
        import concurrent.futures
        import urllib.parse as _up2

        def _bing_search(query: str) -> Optional[NovelInfo]:
            """单次 Bing 查询"""
            bing_hosts = ["https://cn.bing.com", "https://www.bing.com"]
            for host in bing_hosts:
                try:
                    r = self.session.get(
                        f"{host}/search?q={_up2.quote(query)}",
                        timeout=self.SEARCH_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                    if r.status_code != 200:
                        continue
                    ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
                    if ids:
                        seen = set()
                        unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
                        info = self._get_novel_from_page(unique_ids[0])
                        if info and info.title:
                            return info
                except Exception:
                    continue
            return None

        logger.info("并行搜索 '%s' -> %d种查询", str(title)[:30], len(unique_queries))
        
        # 先用最精确的查询串行试一次
        if unique_queries:
            result = _bing_search(unique_queries[0])
            if result:
                logger.info(f"搜到: {result.title} (ID={result.book_id})")
                return result
        
        # 失败则并行跑剩余查询（每个查询用独立 session，requests.Session 非线程安全）
        if len(unique_queries) > 1:
            def _parallel_search(query: str):
                import requests as _req
                import threading as _threading
                import urllib3 as _urllib3
                if not self.session.verify:
                    _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
                # 每个线程独立 session
                local_session = _req.Session()
                local_session.headers.update(self.HEADERS)
                local_session.verify = self.session.verify
                local_session.trust_env = False
                # 共享限流锁：并行查询互斥节流，降低对 Bing 的请求频率（防反爬/防滥用）
                search_lock = _threading.Lock()
                bing_hosts = ["https://cn.bing.com", "https://www.bing.com"]
                for host in bing_hosts:
                    with search_lock:
                        time.sleep(0.3)  # 每次 Bing 请求间隔 300ms
                    try:
                        r = local_session.get(
                            f"{host}/search?q={_up2.quote(query)}",
                            timeout=2,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                        if r.status_code != 200:
                            continue
                        ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
                        if ids:
                            seen = set()
                            unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
                            info = self._get_novel_from_page(unique_ids[0])
                            if info and info.title:
                                return info
                    except Exception:
                        continue
                return None
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(unique_queries)-1) as executor:
                futures = {executor.submit(_parallel_search, q): q for q in unique_queries[1:]}
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=5):
                        try:
                            result = future.result()
                            if result:
                                logger.info(f"搜到: {result.title} (ID={result.book_id})")
                                for f in futures:
                                    f.cancel()
                                return result
                        except Exception:
                            continue
                except concurrent.futures.TimeoutError:
                    logger.debug("并行搜索超时，进入兜底")
                    for f in futures:
                        f.cancel()

        # 全部失败则兜底番茄搜索页
        try:
            search_url = f"https://fanqienovel.com/search/{_up2.quote(title)}"
            r = self.session.get(search_url, timeout=self.SEARCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
            if ids:
                seen = set()
                unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
                info = self._get_novel_from_page(unique_ids[0])
                if info and info.title:
                    logger.info(f"番茄搜索页兜底: {info.title} (ID={info.book_id})")
                    return info
        except Exception as e:
            logger.debug(f"兜底搜索: {e}")

        logger.warning(f"搜索失败: {title}")
        return None

    def _get_novel_from_page(self, book_id: str) -> Optional[NovelInfo]:
        """从番茄书籍页面提取信息（解析SSR数据）"""
        try:
            r = self.session.get(
                f"{self.BASE_URL}/page/{book_id}",
                timeout=self.DETAIL_TIMEOUT,
                headers={"Accept": "text/html,application/xhtml+xml"})
            if r.status_code != 200:
                return None

            # 提取 window.__INITIAL_STATE__
            import json as _json
            m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, re.DOTALL)
            if not m:
                return None

            ssr = _json.loads(m.group(1))
            page = ssr.get("page", {})
            if not page or not page.get("bookName"):
                return None

            return NovelInfo(
                book_id=str(book_id),
                title=page.get("bookName", ""),
                author=page.get("author", ""),
                genre=self.GENRE_MAP.get(page.get("category", ""), page.get("category", "")),
                sub_genre=page.get("categoryV2", ""),
                word_count=page.get("wordNumber", 0),
                chapter_count=sum(
                    len(vol) for vol in page.get("chapterListWithVolume", [])
                    if isinstance(vol, list)),
                hot_score=page.get("readCount", 0),
                intro=page.get("abstract", ""),
                url=f"{self.BASE_URL}/page/{book_id}",
                cover=_find_cover_url(page),
            )
        except Exception as e:
            logger.warning(f"Page parse failed for {book_id}: {e}")
            return None

    def _parse_novel_info(self, info: dict) -> NovelInfo:
        """从API返回数据解析NovelInfo"""
        return NovelInfo(
            book_id=str(info.get("book_id", "")),
            title=info.get("book_name", ""),
            author=info.get("author", ""),
            genre=self.GENRE_MAP.get(info.get("genre_type", 0), ""),
            word_count=info.get("all_word_count", 0),
            chapter_count=info.get("all_chapter_count", 0),
            hot_score=info.get("read_count", 0),
            intro=info.get("abstract", ""),
            url=f"{self.BASE_URL}/page/{info.get('book_id','')}",
            cover=_find_cover_url(info),
        )

    def get_novel_info(self, book_id: str) -> Optional[NovelInfo]:
        """获取单本书详细信息"""
        try:
            url = f"{self.API_BASE}/reader/book_info/v0"
            resp = self.session.get(url, params={"book_id": book_id}, timeout=self.DETAIL_TIMEOUT)
            data = resp.json()
            info = data.get("data", {})

            return NovelInfo(
                book_id=str(book_id),
                title=info.get("book_name", ""),
                author=info.get("author", ""),
                genre=self.GENRE_MAP.get(info.get("genre_type", 0), ""),
                word_count=info.get("all_word_count", 0),
                chapter_count=info.get("all_chapter_count", 0),
                intro=info.get("abstract", ""),
                url=f"{self.BASE_URL}/page/{book_id}",
                cover=_find_cover_url(info),
            )
        except Exception as e:
            logger.warning(f"Failed to get info for {book_id}: {e}")
            return None

    def get_chapter_list(self, book_id: str, max_count: int = 100) -> list[dict]:
        """获取章节目录 — 从书籍页SSR或API"""
        import json as _json

        # 方法1: 书籍页SSR → page.chapterListWithVolume
        try:
            r = self.session.get(f"{self.BASE_URL}/page/{book_id}", timeout=self.DETAIL_TIMEOUT,
                headers={"Accept": "text/html,application/xhtml+xml"})
            if r.status_code == 200:
                m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, re.DOTALL)
                if m:
                    ssr = _json.loads(m.group(1))
                    ch_list = ssr.get("page", {}).get("chapterListWithVolume", [])
                    if ch_list:
                        chapters = []
                        # chapterListWithVolume 是 [ [卷1章节...], [卷2章节...], ... ]
                        for volume_chapters in ch_list:
                            if isinstance(volume_chapters, list):
                                for ch in volume_chapters:
                                    if isinstance(ch, dict):
                                        chapters.append({
                                            "id": ch.get("itemId", ""),
                                            "title": ch.get("title", ""),
                                            "index": int(ch.get("realChapterOrder", len(chapters)+1)),
                                            "volume": ch.get("volume_name", ""),
                                        })
                                        if len(chapters) >= max_count:
                                            break
                            if len(chapters) >= max_count:
                                break
                        return chapters
        except Exception:
            pass

        # 方法2: novel.snssdk.com API
        try:
            r = self.session.get(
                "https://novel.snssdk.com/api/novel/book/directory/list/v1/",
                params={"book_id": book_id, "offset": 0, "count": max_count},
                timeout=self.DETAIL_TIMEOUT,
                headers={"Referer": "https://novel.snssdk.com/"})
            if r.status_code == 200:
                data = r.json()
                item_ids = data.get("data", {}).get("allItemIds", [])
                if item_ids:
                    return [{"id": cid, "title": f"第{i+1}章", "index": i+1}
                            for i, cid in enumerate(item_ids[:max_count])]
        except Exception:
            pass

        return []

    def download_chapter(self, book_id: str, chapter_id: str) -> str:
        """下载单章 — 从阅读器页面SSR提取"""
        cache_file = self.cache_dir / f"{chapter_id}.txt"
        if cache_file.exists():
            try:
                cached = cache_file.read_text(encoding="utf-8")
            except Exception:
                cached = ""
            if cached:
                return cached
            # 空/损坏缓存（0 字节残留）视为未缓存，重新下载

        try:
            r = self.session.get(
                f"{self.BASE_URL}/reader/{chapter_id}",
                timeout=self.DETAIL_TIMEOUT,
                headers={"Accept": "text/html,application/xhtml+xml"})
            if r.status_code != 200:
                return ""

            import json as _json
            m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, re.DOTALL)
            if not m:
                return ""

            ssr = _json.loads(m.group(1))
            reader = ssr.get("reader", {})
            chapter_data = reader.get("chapterData", {})
            content = chapter_data.get("content", "")

            if not content:
                # 尝试其他路径
                for path in ["chapterData.content", "chapter.content", "content"]:
                    obj = ssr
                    for key in path.split("."):
                        obj = obj.get(key, {}) if isinstance(obj, dict) else {}
                    if isinstance(obj, str) and obj.strip():
                        content = obj
                        break

            if content:
                # 1) 去掉图片块（<img>...</img>，内含 {{image_domain}} 模板占位，无实际图）
                content = re.sub(r'<img[^>]*>.*?</img>', '', content, flags=re.DOTALL)
                content = re.sub(r'<img[^>]*>', '', content)
                # 2) 段落/换行标签 → 换行（<p></p><br><div> 都转，否则剥掉后整章挤成一段）
                content = re.sub(r'</?p[^>]*>|<br\s*/?>|</?div[^>]*>', '\n', content)
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\n{3,}', '\n\n', content)
                # 3) PUA 字体解码（全量检查——开头可能被长 <img> 模板占位挤出前 100 字）
                self._init_decoder()
                if any(0xE000 <= ord(c) <= 0xF8FF for c in content):
                    content = self._decoder.decode_content(content)

                if content.strip():
                    # 原子写缓存（临时文件 + os.replace），避免并发读/崩溃读到半写正文
                    import tempfile as _tf
                    import os as _os
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    _fd, _tmp = _tf.mkstemp(dir=str(self.cache_dir), suffix=".tmp")
                    try:
                        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                            _f.write(content)
                        _os.replace(_tmp, str(cache_file))
                    except Exception:
                        try:
                            _os.unlink(_tmp)
                        except Exception:
                            pass
                        raise

            return content or ""
        except Exception as e:
            logger.warning(f"Failed to download ch {chapter_id}: {e}")
            return ""

    def download_book(self, book_id: str, chapter_count: int = 30,
                      delay: float = 1.0) -> list[dict]:
        """下载一本书的前 N 章"""
        chapters = self.get_chapter_list(book_id, chapter_count)
        results = []

        for ch in chapters:
            content = self.download_chapter(book_id, ch["id"])
            if content.strip():
                results.append({
                    "index": ch["index"],
                    "title": ch["title"],
                    "content": content,
                    "word_count": len(re.findall(r'[\u4e00-\u9fff]', content)),
                })
            time.sleep(delay)  # 礼貌爬取

        return results


# ═══════════════════════════════════════
# LLM 分析器
# ═══════════════════════════════════════

class NovelAnalyzer:
    """用 LLM 分析小说内容，提取可复用的模式"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    def analyze_book(self, novel: NovelInfo, chapters: list[dict],
                      on_progress=None) -> dict:
        """分析一本小说，提取所有可复用元素"""
        if not self.llm:
            return {"plots": [], "structures": [], "gags": []}

        samples = self._select_samples(chapters)

        result = {}

        if on_progress:
            on_progress("analyze", 1, 4, "提取桥段...")
        result["plots"] = self.extract_plots(novel, samples)

        if on_progress:
            on_progress("analyze", 2, 4, "提取大纲...")
        result["structures"] = self.extract_structure(novel, samples)

        if on_progress:
            on_progress("analyze", 3, 3, "提取笑点...")
        result["gags"] = self.extract_gags(novel, samples)

        return result

    def _select_samples(self, chapters: list[dict]) -> list[dict]:
        """选择代表性章节样本"""
        if len(chapters) <= 10:
            return chapters
        indices = [0, 1, 2, len(chapters)//4, len(chapters)//2,
                   3*len(chapters)//4, -3, -2, -1]
        return [chapters[i] for i in indices if 0 <= i < len(chapters)]

    def extract_plots(self, novel: NovelInfo, samples: list[dict]) -> list[dict]:
        """提取桥段模式"""
        text = self._build_sample_text(samples, 3000)

        prompt = f"""分析以下番茄小说《{novel.title}》（{novel.genre}/{novel.sub_genre}）的前几章，
提取出 3-5 个可复用的桥段模式。

每个桥段需要：
1. 桥段名称（如"退婚打脸""系统激活""拍卖会捡漏"）
2. 桥段结构骨架（用箭头表示流程，如 [挑衅]→[隐忍]→[爆发]→[震惊全场]）
3. 关键变量槽位（如 主角身份、对手身份、冲突起因、反转方式）
4. 使用该桥段时的注意事项

【小说内容样本】
{text}

返回 JSON：
{{"plots": [
  {{"name":"桥段名", "category":"爽文", "sub_category":"打脸/反转/...",
   "structure":"[步骤1]→[步骤2]→...",
   "slots":[{{"name":"变量名","options":["选项1","选项2"]}}],
   "notes":"使用注意", "word_range":[800,2500], "quality_rating":4}}
]}}"""
        try:
            raw = self.llm.call("你是一位专业的网文拆书分析师。只返回JSON。",
                                prompt, temperature=0.5, max_tokens=4096)
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            return data.get("plots", [])
        except Exception as e:
            logger.warning(f"Plot extraction failed: {e}")
            return []

    def extract_structure(self, novel: NovelInfo, samples: list[dict]) -> list[dict]:
        """提取大纲结构模式（**平级独立弧**：structures 数组每行 = 一条可复用的独立弧模板，
        无父子层级；入库按行落盘）"""
        text = self._build_sample_text(samples, 2000)
        ch_count = novel.chapter_count or len(samples) * 10

        prompt = f"""分析番茄小说《{novel.title}》（{novel.genre}，约{ch_count}章）的章节结构，
从书中识别出**若干个典型的、可复用的叙事弧**（每个弧是一个有明确目标/方向的剧情单元，
如 重生复仇弧、试炼扬名弧、误会和解弧、末日囤货弧、误会和解小弧）。

**弧库是平级独立弧**：structures 数组里**每个元素 = 一条独立的弧模板**，相互之间**没有
父子/包含关系**，不要产出树层级、不要写 parent 类字段。

每条弧字段统一：
  - name: 弧名（一句话能讲清这弧干什么）
  - description: 这个弧做什么 / 本弧内情节怎么发展（关键：能被复用的内容主体）
  - min_words / max_words: 该弧在书里实际占用的字数区间（按每章约 3000 字估算；大弧
    （如跨十几章）与小弧（如几章的小目标）都可以收，粒度为真实可复用的那个「弧」）
  - key_events: 关键事件；foreshadow_opportunities: 埋坑机会；themes: 弧级内涵
  - tags: 题材/可复用场景标签（每弧都要给，便于入库后按题材检索）
长度/粒度按书里真实结构定、**不要求均匀**。

【小说内容样本】
{text}

返回 JSON（独立弧数组）：
{{"structures": [
  {{"name":"重生复仇弧","description":"被夺权者蛰伏反杀，当众清算并夺回一切的一整段弧：藏拙→串联旧部→在清算场合翻盘",
    "min_words":30000,"max_words":45000,
    "key_events":["蛰伏示弱","收买旧部","当众反杀"],"foreshadow_opportunities":["幕后黑手另有其人"],
    "themes":[],"tags":["复仇","爽文"]}},
  {{"name":"末日囤货开局","description":"灾变前用先知囤物资、抢住所，抢在秩序崩塌前站稳脚跟的小弧",
    "min_words":6000,"max_words":12000,
    "key_events":["变卖资产","扫货","加固住所"],"foreshadow_opportunities":[],
    "themes":[],"tags":["末世","求生"]}}
]}}"""
        try:
            raw = self.llm.call("你是一位专业的小说结构分析师。只返回JSON。",
                                prompt, temperature=0.5, max_tokens=4096)
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            return data.get("structures", [])
        except Exception as e:
            logger.warning(f"Structure extraction failed: {e}")
            return []

    def extract_gags(self, novel: NovelInfo, samples: list[dict]) -> list[dict]:
        """提取笑点模式"""
        text = self._build_sample_text(samples, 2000)

        prompt = f"""分析以下小说中的笑点/幽默段落，提取可复用的搞笑模式。

每个模式包括：
1. 模式名称（如"反差吐槽""凡尔赛装逼""沙雕对话"）
2. 模式描述和结构
3. 适合使用的场景
4. 1-2个例句

【小说内容样本】
{text}

返回 JSON：
{{"gags": [
  {{"name":"模式名","category":"吐槽/反差/误会/沙雕/...",
   "pattern_description":"详细描述这个搞笑模式的结构",
   "fit_scenes":["日常","战斗","对话"],
   "examples":["例句1","例句2"]}}
]}}"""
        try:
            raw = self.llm.call("你是一位专业的喜剧写作分析师。只返回JSON。",
                                prompt, temperature=0.5, max_tokens=4096)
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            return data.get("gags", [])
        except Exception as e:
            logger.warning(f"Gag extraction failed: {e}")
            return []

    def _build_sample_text(self, samples: list[dict], max_chars: int) -> str:
        """构建样本文本"""
        parts = []
        total = 0
        for ch in samples:
            content = ch.get("content", "")
            if total + len(content) > max_chars:
                remaining = max_chars - total
                parts.append(content[:remaining])
                break
            parts.append(f"【{ch.get('title', '')}】\n{content}\n")
            total += len(content)
        return "\n".join(parts)


# ═══════════════════════════════════════════
# 入库器
# ═══════════════════════════════════════════

class LibraryIngestor:
    """将分析结果导入各库（桥段/大纲/笑点）"""

    def __init__(self, plot_lib=None, struct_lib=None, gag_lib=None,
                 char_lib=None):
        self.plot_lib = plot_lib
        self.struct_lib = struct_lib
        self.gag_lib = gag_lib
        self.char_lib = char_lib

    def ingest(self, analysis: dict, source: str = "fanqie") -> dict:
        """导入分析结果到各库"""
        stats = {"plots": 0, "structures": 0, "gags": 0}

        for plot in analysis.get("plots", []):
            if self.plot_lib:
                self._add_plot(plot, source)
                stats["plots"] += 1

        for arc in analysis.get("structures", []):
            if self.struct_lib:
                self._add_structure(arc, source)
                stats["structures"] += 1

        for gag in analysis.get("gags", []):
            if self.gag_lib:
                self._add_gag(gag, source)
                stats["gags"] += 1

        return stats

    def _add_plot(self, data: dict, source: str):
        from libraries.plot import PlotTemplate, PlotSlot
        tid = f"scout_{source}_{data.get('name','unknown')}"
        # 去重
        for t in self.plot_lib.templates:
            if t.id == tid:
                return

        slots = [PlotSlot(name=s.get("name",""), description="",
                          options=s.get("options",[]), default="")
                 for s in data.get("slots", [])]
        template = PlotTemplate(
            id=tid, name=data.get("name",""),
            category=data.get("category",""),
            sub_category=data.get("sub_category",""),
            source=source,
            template_structure=data.get("structure",""),
            slots=slots,
            usage_notes=data.get("notes",""),
            word_range=tuple(data.get("word_range", [800, 2500])),
        )
        self.plot_lib.templates.append(template)

    def _add_structure(self, data: dict, source: str):
        """把**一条平级独立弧 dict** 写入情节弧库（每行一弧）。

        id = scout_{source}_{清洗名}（精确去重，已存在则跳过）。data 各字段
        （name/description/min/max_words/key_events/themes/tags/…）即 ArcNode 字段。
        """
        from datetime import datetime
        from libraries.structure import ArcNode, make_root_id
        sid = make_root_id(data.get("name", ""), source)
        for t in self.struct_lib.templates:
            if t.id == sid:
                return  # 已存在跳过

        created = str(data.get("created_at") or "") or datetime.now().strftime("%Y-%m-%d %H:%M")
        d = dict(data)
        d["id"] = sid
        d["source"] = source
        d["created_at"] = d.get("created_at") or created
        self.struct_lib.templates.append(ArcNode.from_dict(d))

    def _add_gag(self, data: dict, source: str):
        from libraries.gag import GagPattern
        gid = f"scout_{source}_{data.get('name','unknown')}"
        for g in self.gag_lib.patterns:
            if g.id == gid:
                return

        pattern = GagPattern(
            id=gid, name=data.get("name",""),
            category=data.get("category",""),
            pattern_description=data.get("pattern_description",""),
            template=data.get("pattern_description",""),
            fit_scenes=data.get("fit_scenes") or data.get("scene_fit") or [],
            examples=data.get("examples",[]),
        )
        self.gag_lib.patterns.append(pattern)

    def _add_character(self, data: dict, source: str):
        from libraries.character import CharacterArchetype
        if not self.char_lib:
            return
        cid = f"scout_{source}_{data.get('name','unknown')}"
        for c in self.char_lib.archetypes:
            if c.id == cid:
                return

        kwargs = dict(
            id=cid, name=data.get("name", ""),
            personality=data.get("personality", ""),
            description=data.get("description", ""),
            archetypes=data.get("archetypes", []),
            examples=data.get("examples", []),
            catchphrases=data.get("catchphrases", []),
            tags=data.get("tags", []),
            fit_tags=data.get("fit_tags", []),
            source=source,
        )
        if data.get("created_at"):
            kwargs["created_at"] = data["created_at"]
        self.char_lib.archetypes.append(CharacterArchetype(**kwargs))


# ═══════════════════════════════════════════
# 总调度
# ═══════════════════════════════════════════

class FanqieScoutAgent:
    """
    番茄侦察兵 — 完整侦察流程

    用法:
        scout = FanqieScoutAgent(llm_client)
        result = scout.run(genre="玄幻", book_count=5, chapters_per_book=30)
        # result.new_plots → 已导入 plot_lib
    """

    def __init__(self, llm_client=None, plot_lib=None, struct_lib=None,
                 gag_lib=None, char_lib=None, verify: bool = True):
        self.crawler = FanqieCrawler(verify=verify)
        self.analyzer = NovelAnalyzer(llm_client)
        self.plot_lib = plot_lib
        self.struct_lib = struct_lib
        self.gag_lib = gag_lib
        self.char_lib = char_lib
        self.ingestor = LibraryIngestor(plot_lib, struct_lib, gag_lib, char_lib)

    def run(self, genre: str = "", book_count: int = 5,
            chapters_per_book: int = 30, delay: float = 1.5,
            on_progress=None) -> ScoutResult:
        """
        执行一次完整侦察。

        on_progress(phase, current, total, message) — 进度回调
        """
        result = ScoutResult()

        # Step 1: 发现热榜
        logger.info(f"Discovering hot books (genre={genre or 'all'})...")
        genre_id = 0
        for gid, gname in FanqieCrawler.GENRE_MAP.items():
            if genre in gname:
                genre_id = gid
                break

        novels = self.crawler.discover_hot(genre_id, book_count)
        result.source_books = novels
        logger.info(f"Found {len(novels)} books")

        # Step 2: 逐书下载+分析
        total_downloaded = 0
        for i, novel in enumerate(novels):
            if on_progress:
                on_progress("analyze", i+1, len(novels),
                            f"[{i+1}/{len(novels)}] {novel.title}")
            logger.info(f"[{i+1}/{len(novels)}] Analyzing: {novel.title}")

            # 下载
            chapters = self.crawler.download_book(
                novel.book_id, chapters_per_book, delay)
            total_downloaded += len(chapters)
            logger.info(f"  Downloaded {len(chapters)} chapters")

            if not chapters:
                continue

            # 分析
            analysis = self.analyzer.analyze_book(novel, chapters)
            result.new_plots.extend(analysis.get("plots", []))
            result.new_structures.extend(analysis.get("structures", []))
            result.new_gags.extend(analysis.get("gags", []))

            # 入库
            stats = self.ingestor.ingest(analysis, "fanqie")
            logger.info(f"  Ingested: {stats}")

            # 礼貌延迟
            time.sleep(delay)

        result.downloaded_chapters = total_downloaded
        logger.info(f"Scout complete: {len(result.new_plots)} plots, "
                     f"{len(result.new_structures)} structures, "
                     f"{len(result.new_gags)} gags")

        return result

    def fetch_novel(self, title: str, chapters: int = 0,
                    start_chapter: int = 1, end_chapter: int = 0,
                    on_progress=None, download_delay: float = 1.0) -> tuple:
        """仅下载（不分析不入库），返回 (NovelInfo, downloaded_chapters)。

        chapters<=0（默认）→ 全文/增量下载：书未下载则从头下全文；已在本地则只补新章节
        （起始 = 已下载最大章 + 1，结束 = 全书总章数）。chapters>0 时按真实章号区间下载：
        start_chapter=100, end_chapter=130 下载 100~130 章；只给 chapters 时从 start_chapter(缺省 1) 起 N 章。
        """
        if on_progress:
            on_progress("search", 0, 1, f"搜索: {title}")
        novel = self.crawler.search_novel(title)
        if not novel:
            return None, None

        if on_progress:
            on_progress("search", 1, 1, f"找到: {novel.title}")

        full = int(chapters or 0) <= 0
        if full:
            # 全文/增量：起始 = 已下载最大章 + 1（无则 1），结束 = 全书总章数
            existing_max = self._existing_max_chapter(novel.title)
            total = int(novel.chapter_count or 0)
            if total <= 0:
                total = 10000   # 未知总章数兜底：SSR 目录按全部卷返回，此处仅作目录拉取上限
            start_chapter = existing_max + 1
            end_chapter = total
            if existing_max:
                if on_progress:
                    on_progress("search", 1, 1,
                                f"已下载至第 {existing_max} 章，增量更新 {existing_max+1}~{total} 章")
        else:
            start_chapter = max(1, int(start_chapter or 1))
            effective_end = int(end_chapter or 0)
            if effective_end <= 0:
                effective_end = start_chapter + int(chapters or 0) - 1
            end_chapter = effective_end

        catalog = self.crawler.get_chapter_list(novel.book_id, end_chapter)
        chapter_list = [c for c in catalog
                        if start_chapter <= int(c.get("index") or 0) <= end_chapter]
        total_ch = len(chapter_list)

        from plugins.novel_storage import (save_novel, save_chapter, NOVELS_DIR,
                                           _safe_name)
        folder = _safe_name(novel.title)

        # 已是最新（无需下载）：不重写存储，直接返回现有文件夹
        if not chapter_list:
            if on_progress:
                on_progress("download", 0, 0, "已是最新，无需下载")
            return novel, {"folder": folder, "chapters": 0, "already": True}

        if on_progress:
            on_progress("download", 0, total_ch, f"下载 {total_ch} 章...")

        # 建目录 + info.json（含封面）——仅当该书尚未落盘 info（增量/续传保留原来源元数据，
        # 不覆盖 downloaded_at / site / 番茄合并来源字段）
        if not (NOVELS_DIR / folder / "info.json").exists():
            save_novel("fanqie", {
                "title": novel.title, "author": novel.author,
                "book_id": novel.book_id, "url": novel.url,
                "genre": novel.genre, "chapter_count": novel.chapter_count,
                "cover": novel.cover,
            }, [])

        # 断点/重复请求幂等：跳过已落盘**且 content 完整**的章（短预览/空占位视为缺章重下）
        from plugins.novel_storage import existing_complete_chapters
        existing = existing_complete_chapters(folder, min_units=FANQIE_FREE_MIN_CHARS)
        pending = [c for c in chapter_list if int(c.get("index") or 0) not in existing]
        skipped_existing = total_ch - len(pending)
        downloaded = 0
        skipped_locked = 0
        from core.text_utils import cjk_char_count as _cjkc   # 锁章门用纯 CJK 口径
        for i, ch in enumerate(pending):
            content = self.crawler.download_chapter(novel.book_id, ch["id"])
            units = _count_prose_units(content or "")
            if content and content.strip() and _cjkc(content or "") >= FANQIE_FREE_MIN_CHARS:
                save_chapter("fanqie", folder, {
                    "index": ch["index"], "title": ch["title"],
                    "content": content,
                    "word_count": units,
                })
                downloaded += 1
            elif content and content.strip():
                # 锁章预览/占位：不当正文落盘，留待有镜像全文或解锁后再补（进度如实际报）
                skipped_locked += 1
            if on_progress:
                on_progress("download", i + 1, len(pending), ch["title"][:30])
            if i < len(pending) - 1:
                time.sleep(download_delay)  # 礼貌爬取间隔

        if on_progress:
            on_progress("download", len(pending), len(pending),
                        f"下载完成 {downloaded}章（本次新增）")

        return novel, {"folder": folder, "chapters": downloaded,
                       "already": False, "skipped_existing": skipped_existing,
                       "skipped_locked": skipped_locked}



    def _existing_max_chapter(self, title: str) -> int:
        """该书在本地书库已下载的**完整**最大章号（0 = 未下载过/全不完整）。

        只计 content 有效（≥ FREE 字数）的章：若最后落盘的是锁章短预览/空占位，视为缺章，
        增量起点会回退到它之前 → 下次重下可修复。统一书库：NOVELS_DIR/<书名>/。
        """
        from plugins.novel_storage import _safe_name, existing_complete_chapters
        idxs = existing_complete_chapters(_safe_name(title),
                                          min_units=FANQIE_FREE_MIN_CHARS)
        return max(idxs) if idxs else 0

    def scout_single_book(self, title: str, chapters: int = 50,
                          on_progress=None, download_delay: float = 1.0) -> ScoutResult:
        """
        侦察单本书——按书名搜索 → 下载 → 分析 → 入库

        on_progress(phase, current, total, message)
        """
        result = ScoutResult()

        if on_progress:
            on_progress("search", 0, 1, f"搜索: {title}")
        novel = self.crawler.search_novel(title)
        if not novel:
            return result

        result.source_books = [novel]
        if on_progress:
            on_progress("search", 1, 1,
                        f"找到: {novel.title} ({novel.chapter_count}章)")

        chapter_list = self.crawler.get_chapter_list(novel.book_id, chapters)
        total_ch = len(chapter_list)

        if on_progress:
            on_progress("download", 0, total_ch, f"下载 {total_ch} 章...")

        downloaded = []
        for i, ch in enumerate(chapter_list):
            content = self.crawler.download_chapter(novel.book_id, ch["id"])
            if content.strip():
                imported_re = re
                downloaded.append({
                    "index": ch["index"], "title": ch["title"],
                    "content": content,
                    "word_count": len(imported_re.findall(r'[\u4e00-\u9fff]', content)),
                })
            if on_progress:
                on_progress("download", i+1, total_ch, ch["title"][:30])
            time.sleep(download_delay)  # 礼貌爬取间隔

        result.downloaded_chapters = len(downloaded)
        if on_progress:
            on_progress("download", total_ch, total_ch, f"下载完成 {len(downloaded)}章")

        if not downloaded:
            return result

        if on_progress:
            on_progress("analyze", 0, 4, "LLM分析...")
        analysis = self.analyzer.analyze_book(novel, downloaded, on_progress=on_progress)

        result.new_plots = analysis.get("plots", [])
        result.new_structures = analysis.get("structures", [])
        result.new_gags = analysis.get("gags", [])

        if on_progress:
            on_progress("analysis_done", 3, 3, "分析完成，等待入库")

        return result

    def ingest_selected(self, plots: list = None, structures: list = None,
                        gags: list = None, characters: list = None,
                        source: str = "fanqie", on_progress=None) -> dict:
        """选择性入库（含角色原型库）"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        stats = {"plots": 0, "structures": 0, "gags": 0, "characters": 0}

        if plots and self.plot_lib:
            for item in plots:
                item["source"] = source
                item["created_at"] = now
                self.ingestor._add_plot(item, source)
                stats["plots"] += 1
            if on_progress:
                on_progress("ingest", stats["plots"], len(plots), f"桥段已入库 {stats['plots']}/{len(plots)}")
            self.plot_lib._save()

        if structures and self.struct_lib:
            for arc in structures:
                self.ingestor._add_structure(arc, source)
                stats["structures"] += 1
            if on_progress:
                on_progress("ingest", 1, 1, f"大纲已入库 {stats['structures']}个")
            self.struct_lib._save()

        if gags and self.gag_lib:
            for item in gags:
                item["source"] = source
                item["created_at"] = now
                self.ingestor._add_gag(item, source)
                stats["gags"] += 1
            if on_progress:
                on_progress("ingest", 1, 1, f"笑点已入库 {stats['gags']}个")
            self.gag_lib._save()

        if characters and self.char_lib:
            for item in characters:
                item["source"] = source
                item["created_at"] = now
                self.ingestor._add_character(item, source)
                stats["characters"] += 1
            if on_progress:
                on_progress("ingest", 1, 1, f"角色已入库 {stats['characters']}个")
            self.char_lib._save()

        return stats


# ═══════════════════════════════════════════
# 命令行入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("Fanqie Scout Agent")
    print("Usage: python -m plugins.fanqie_scout [genre] [book_count] [chapters_per_book]")
    print()

    genre = sys.argv[1] if len(sys.argv) > 1 else "玄幻"
    book_count = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    chapters = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    # 初始化 LLM
    api_path = Path("api.json")
    if api_path.exists():
        cfg = json.loads(api_path.read_text(encoding="utf-8"))
        from core.models import APIConfig
        from core.llm_client import LLMClient
        api_cfg = APIConfig(
            api_key=cfg.get("api_key",""),
            base_url=cfg.get("base_url","https://api.deepseek.com"),
            model=cfg.get("model","deepseek-chat"),
            http_timeout_seconds=cfg.get("http_timeout_seconds",300),
        )
        llm = LLMClient(api_cfg)
    else:
        llm = None
        print("No api.json found, running in download-only mode")

    # 初始化库
    from libraries.plot import PlotLibrary
    from libraries.structure import StructureLibrary
    from libraries.gag import GagLibrary

    scout = FanqieScoutAgent(llm, PlotLibrary(), StructureLibrary(),
                              GagLibrary())
    result = scout.run(genre=genre, book_count=book_count,
                       chapters_per_book=chapters)

    print(f"\n=== Scout Complete ===")
    print(f"Books analyzed: {len(result.source_books)}")
    print(f"Chapters downloaded: {result.downloaded_chapters}")
    print(f"New plots: {len(result.new_plots)}")
    print(f"New structures: {len(result.new_structures)}")
    print(f"New gags: {len(result.new_gags)}")
