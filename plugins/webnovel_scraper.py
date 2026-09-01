"""网页镜像站小说爬虫（通用、配置驱动）— 番茄等官方站锁定章需 VIP 时的替代全文源。

Bing 搜索能找到大量无需 SVIP 的全文镜像站（广告多）。本模块用「站点适配器 dict +
泛化爬虫」一次性支持这类站：新增站点只需在 SITES 里加一条配置（书页/章表/正文选择器/
编码/主书过滤正则），无需改爬虫逻辑。首个适配器 = wodushu（我的书城网，实测《十日终焉》
全书可抓、无登录/无 Cookie/无反爬限制）。

MCP / Web / CLI 统一入口：`download_webnovel`。进度复写 storage/crawl_progress.json
（/scout 页轮询）。存储复用 plugins/novel_storage（platform="web"）。

实测要点（wodushu）：
- 章表分页 `/book/{bid}/{page}/`；页面顶部有「最近更新」块（番外重复）会污染顺序，
  必须按标题里的 `第N章` 数字排序、番外排尾，不能取首见顺序
- 分页不会 404 结束（末页后重复返回 HTTP 200）→ 以「某页 0 个新 href」终止 + 页数上限
- 长章拆续页 `{cid}_2.html`、`_3.html`，正文 `<div class="content" id="content">`，UTF-8
- 站点声称章数 ≠ 实际（1556 vs 1386）→ chapter_count / 进度 total 一律用去重后的目录长度
"""
import logging
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

logger = logging.getLogger("novel-engine.webnovel")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 广告行黑名单（轻量，仅过滤明显广告；正文合法引用不会被误伤）
_AD_LINE_RE = re.compile(
    r"请收藏|记住本站|最快更新|天才一秒|永久.{0,6}域名|笔趣阁.{0,6}(网址|地址)|"
    r"^https?://\S+$|^www\.\S+$"
)

# ─── 站点适配器注册表 ────────────────────────────────────────────────
# 新增站点：照抄一条，覆盖必要键即可（其余用默认）。{cid}/{bid} 等模板在调用处替换。
SITES = {
    "wodushu": {
        "name": "我的书城网",
        "base_url": "https://www.wodushu.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        # 从 URL 解析 book_id（book_id_re 不匹配时整串视为 book_id）
        "book_id_re": r"/book/(\d+)/",
        "book_page": lambda b: f"/book/{b}/",
        "chapter_list_page": lambda b, p: f"/book/{b}/{p}/",
        "chapter_url": lambda b, cid, suf: f"/read/{b}/{cid}{suf}.html",
        # 章表链接：组1=href（去重用），组2=chapter_id，组3=链接文本（章节标题，已含「第N章」前缀）。
        # 注意 {cid}_N.html 续页不会被此正则匹配（数字与 .html 之间隔下划线+数字）。
        "chapter_link_re": re.compile(
            r'<a[^>]*href="([^"]*?/read/\d+/(\d+)\.html)"[^>]*>(.*?)</a>', re.S),
        # 续页链接（模板，{cid} 替换为当前 chapter_id），组1=续页号
        "extra_page_re": r'href="[^"]*?/read/\d+/{cid}_(\d+)\.html"',
        "content_div_id": "content",
        "main_title_re": r"^第\d+章",   # 主书过滤（排除番外）；None=全下
        "request_delay": 0.5,
    },
}


class _ContentExtractor(HTMLParser):
    """提取指定 id 的 div 内纯文本：块级标签转行、剔除 script/style。"""

    _BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, div_id: str):
        super().__init__(convert_charrefs=True)
        self.div_id = div_id
        self.in_target = False
        self.depth = 0
        self.skip = 0
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        d = dict(attrs)
        if tag == "div" and d.get("id") == self.div_id and not self.in_target:
            self.in_target = True
            self.depth = 1
            return
        if self.in_target:
            if tag == "div":
                self.depth += 1
            if tag in self._BLOCK_TAGS:
                self._nl()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.in_target:
            return
        if tag == "div":
            self.depth -= 1
            if self.depth <= 0:
                self.in_target = False
        elif tag in self._BLOCK_TAGS:
            self._nl()

    def handle_data(self, data):
        if self.in_target and self.skip <= 0:
            self.out.append(data)

    def _nl(self):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def text(self) -> str:
        return "".join(self.out)


def _meta_content(html: str, key: str) -> str:
    """取 og:novel:<key> meta 的 content（property= 或 name= 均可）。"""
    m = re.search(
        r'<meta[^>]*(?:property|name)="og:novel:%s"[^>]*content="([^"]*)"' % key, html)
    return m.group(1).strip() if m else ""


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


class WebnovelCrawler:
    """通用网页镜像站爬虫（站点行为由 SITES[site] 配置驱动）。"""

    def __init__(self, site: str = "wodushu", verify: bool = True,
                 cache_dir: str = ""):
        if site not in SITES:
            raise ValueError(f"未知站点: {site}（可用: {', '.join(SITES)}）")
        self.site = site
        self.cfg = SITES[site]
        import requests
        self.session = requests.Session()
        self.session.headers.update(self.cfg.get("headers", {"User-Agent": DEFAULT_UA}))
        self.session.verify = verify
        self.session.trust_env = False  # 不用系统代理，直连访问
        if not verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.cache_dir = Path(cache_dir or f"storage/web_cache/{site}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ── 基础请求 ──
    def _fetch(self, path: str, timeout: int = 15) -> Optional[str]:
        """GET base_url+path，瞬态失败小退避重试 3 次（30 分钟大下载中途不因抖动崩掉）。"""
        url = path if path.startswith("http") else self.cfg["base_url"] + path
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200:
                    r.encoding = self.cfg.get("encoding", "utf-8")
                    return r.text
                logger.warning(f"fetch status={r.status_code}: {url}")
            except Exception as e:
                logger.warning(f"fetch error({attempt}): {url} {e}")
            time.sleep(1 + attempt * 2)
        return None

    # ── 书元信息 ──
    def resolve_book(self, url_or_book_id: str) -> dict:
        """由书 URL 或 book_id 解析书页，取 og:novel:* meta → 元信息 dict。"""
        source = (url_or_book_id or "").strip()
        if not source:
            raise RuntimeError("请提供书籍 URL 或 book_id")
        m = re.search(self.cfg["book_id_re"], source)
        book_id = m.group(1) if m else source
        book_url = (source if source.startswith("http")
                    else self.cfg["base_url"] + self.cfg["book_page"](book_id))
        html = self._fetch(book_url)
        if not html:
            raise RuntimeError(f"无法访问书页: {book_url}")
        title = _meta_content(html, "book_name")
        if not title:
            t = re.search(r"<title>([^<]{1,60})</title>", html)
            title = t.group(1).split("_")[0].strip() if t else ""
        return {
            "book_id": book_id,
            "title": title or "未知",
            "author": _meta_content(html, "author"),
            "genre": _meta_content(html, "category"),
            "url": book_url,
            "site": self.site,
        }

    # ── 章表 ──
    def get_chapter_list(self, book_id: str, max_pages: int = 200) -> list[dict]:
        """遍历分页章表 → 跨页去重 → 带编号按数字升序、番外排尾。

        返回 [{chapter_id, title, href, num}]（未赋 index，由 download_webnovel 过滤后编号）。
        终止条件：某页 0 个新 href（实测末页后重复返回 HTTP 200，不 404）。
        """
        link_re = self.cfg["chapter_link_re"]

        def _num_rank(c):
            return 0 if re.match(r"^第\d+章", c["title"]) else 1

        by_href: dict = {}   # 同 href 去重：页顶「开始阅读」按钮常与真正的「第1章」指向同一章，
        #                    无编号标题排后，重复时替换为带「第N章」编号的那条
        for page in range(1, max_pages + 1):
            path = self.cfg["chapter_list_page"](book_id, page)
            html = self._fetch(path)
            if not html:
                break
            new = 0
            for m in link_re.finditer(html):
                href, cid, inner = m.group(1), m.group(2), m.group(3)
                entry = {"chapter_id": cid, "title": _strip_tags(inner), "href": href}
                if href in by_href:
                    if _num_rank(entry) < _num_rank(by_href[href]):
                        by_href[href] = entry
                else:
                    by_href[href] = entry
                    new += 1
            if new == 0:
                break   # 末页后重复页 → 终止
        if not by_href:
            return []
        catalog = list(by_href.values())
        # 编号解析：第N章 → num；番外 → None。带编号按数字升序，番外排尾。
        for c in catalog:
            mm = re.match(r"^第(\d+)章", c["title"])
            c["num"] = int(mm.group(1)) if mm else None
        numbered = sorted([c for c in catalog if c["num"] is not None],
                          key=lambda c: c["num"])
        extras = [c for c in catalog if c["num"] is None]
        return numbered + extras

    # ── 单章正文 ──
    def download_chapter(self, book_id: str, chapter_id: str) -> str:
        """抓单章正文：拼接 {cid}.html + 续页 {cid}_N.html（无续页链接即止），清洗返回。"""
        parts: list[str] = []
        suffix = ""
        max_pages = 30   # 安全上限：单章不可能有几十页
        for _ in range(max_pages):
            path = self.cfg["chapter_url"](book_id, chapter_id, suffix)
            html = self._fetch(path)
            if not html:
                break
            text = self._extract_content(html)
            if text:
                parts.append(text)
            nxt = re.search(
                self.cfg["extra_page_re"].format(cid=re.escape(chapter_id)), html)
            if not nxt:
                break
            suffix = f"_{nxt.group(1)}"
        return self._clean_text("\n".join(parts))

    def _extract_content(self, html: str) -> str:
        parser = _ContentExtractor(self.cfg["content_div_id"])
        parser.feed(html)
        return parser.text()

    # ── 清洗 ──
    @staticmethod
    def _clean_text(text: str) -> str:
        lines = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or _AD_LINE_RE.search(line):
                continue
            lines.append(line)
        return "\n\n".join(lines)

    # ── 批量下载 ──
    def download_book(self, book_id: str, start: int = 1, end: int = 0,
                      delay: Optional[float] = None,
                      on_progress=None) -> list[dict]:
        """下载区间 [start, end]（end<=0 → 目录尾），逐章返回 [{index,title,content,word_count}]。
        （download_webnovel 是统一入口，此方法供独立使用/调试。）"""
        catalog = self.get_chapter_list(book_id)
        if not catalog:
            return []
        total = len(catalog)
        start = max(1, int(start or 1))
        end = min(int(end or total), total)
        selected = catalog[start - 1:end]
        chapters = []
        delay = self.cfg.get("request_delay", 0.5) if delay is None else delay
        for i, ch in enumerate(selected):
            content = self.download_chapter(book_id, ch["chapter_id"])
            if content and content.strip():
                chapters.append({
                    "index": start + i,
                    "title": ch["title"],
                    "content": content,
                    "word_count": len(re.findall(r"[一-鿿]", content)),
                })
            if on_progress:
                on_progress("download", i + 1, len(selected), ch["title"][:30])
            if i < len(selected) - 1:
                time.sleep(delay)
        return chapters


def download_webnovel(site: str = "wodushu", url: str = "", book_id: str = "",
                      chapters: int = 0, start_chapter: int = 1, end_chapter: int = 0,
                      download_delay: float = 0.5, on_progress=None,
                      platform: str = "web") -> tuple:
    """下载网页镜像站小说 → storage/novels/{platform}/。MCP / Web / CLI 统一入口。

    chapters<=0（默认）→ 全文（从 start_chapter 到目录尾，可按 main_title_re 过滤番外）；
    chapters>0 → 按列表序号区间（第1章=1）。先建目录+info.json（/scout 立即显示），
    再逐章 save_chapter 渐进落盘（停止保留已抓）。返回 (info, {"folder", "chapters"})。
    """
    import re as _re
    if not url and not book_id:
        raise RuntimeError("请提供书籍 URL 或 book_id")
    crawler = WebnovelCrawler(site=site)
    info = crawler.resolve_book(url or book_id)
    catalog = crawler.get_chapter_list(info["book_id"])
    main_re = crawler.cfg.get("main_title_re")
    if main_re:
        catalog = [c for c in catalog if _re.match(main_re, c["title"])]
    total = len(catalog)
    if total == 0:
        raise RuntimeError(f"未解析到章节列表: {info['title']}")
    # 过滤主书后按列表位置编号（规避站点缺号/重号导致 {index:04d}.json 互相覆盖）
    for i, c in enumerate(catalog, 1):
        c["index"] = i

    start = max(1, int(start_chapter or 1))
    if int(chapters or 0) > 0:
        end = int(end_chapter or 0)
        if end <= 0:
            end = start + int(chapters) - 1
    else:
        end = int(end_chapter or 0) or total
    end = min(end, total)
    selected = [c for c in catalog if start <= c["index"] <= end]
    if not selected:
        raise RuntimeError(f"起始章 {start} 超出可下载范围（目录 {total} 章）")

    if on_progress:
        on_progress("search", 1, 1, f"找到: {info['title']}（共{total}章，下载{len(selected)}章）")

    from plugins.novel_storage import save_novel, save_chapter
    folder = save_novel(platform, {
        "title": info["title"], "author": info["author"],
        "book_id": f"{site}:{info['book_id']}", "url": info["url"],
        "genre": info.get("genre", ""), "chapter_count": total,
        "site": site,
    }, [])

    downloaded = 0
    for i, ch in enumerate(selected):
        content = crawler.download_chapter(info["book_id"], ch["chapter_id"])
        if content and content.strip():
            save_chapter(platform, folder, {
                "index": ch["index"], "title": ch["title"],
                "content": content,
                "word_count": len(_re.findall(r"[一-鿿]", content)),
            })
            downloaded += 1
        if on_progress:
            on_progress("download", i + 1, len(selected), ch["title"][:30])
        if i < len(selected) - 1:
            time.sleep(download_delay)

    if on_progress:
        on_progress("download", len(selected), len(selected),
                    f"下载完成 {downloaded}章")
    return info, {"folder": folder, "chapters": downloaded}


def main():
    import argparse
    p = argparse.ArgumentParser(description="网页镜像站小说下载（默认 wodushu）")
    p.add_argument("--site", default="wodushu")
    p.add_argument("--url", default="")
    p.add_argument("--book_id", default="")
    p.add_argument("--chapters", type=int, default=0, help="0=全文（默认）")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=0)
    p.add_argument("--delay", type=float, default=0.4)
    args = p.parse_args()

    def prog(phase, cur, total, msg):
        print(f"[{phase}] {cur}/{total} {msg}", flush=True)

    try:
        info, dl = download_webnovel(
            site=args.site, url=args.url, book_id=args.book_id,
            chapters=args.chapters, start_chapter=args.start,
            end_chapter=args.end, download_delay=args.delay, on_progress=prog)
        print(f"✅ {info['title']} 下载完成 {dl['chapters']}章 → storage/novels/web/{dl['folder']}/")
    except Exception as e:
        print(f"❌ {e}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
