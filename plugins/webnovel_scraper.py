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
import html as _html
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

# 主书章节标题：第N章（阿拉伯或中文数字都算），番外（如「张丽娟（一）」）不匹配
MAIN_TITLE_RE = r"^第(?:[0-9一二三四五六七八九十百千零两]+)章"
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_num_to_int(s: str):
    """中文数字 → int（支持 一~九千九百九十九；非法返回 None）。"""
    if not s or not re.fullmatch(r"[零一二三四五六七八九十百千两]+", s):
        return None
    total = section = num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        else:  # 零
            num = 0
    return total + section + num


def _parse_chapter_num(title: str):
    """章节标题 → 编号（第N章，阿拉伯/中文数字均可）；非第N章 → None（番外）。"""
    m = re.match(r"^第([0-9一二三四五六七八九十百千零两]+)章", title)
    if not m:
        return None
    s = m.group(1)
    return int(s) if s.isdigit() else _cn_num_to_int(s)

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
        "main_title_re": MAIN_TITLE_RE,   # 主书过滤（排除番外；阿拉伯/中文数字章号都算）
        "request_delay": 0.5,
    },
    # 零点看书（笔趣阁克隆）：`/{cat}/{bid}/` 两段数字 URL；正文在 <h1 class="title"> 后（非 div）
    "bookszw": {
        "name": "零点看书",
        "base_url": "http://www.bookszw.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        # 站内搜索（search_book_url 优先用，失败回退 Bing）：GET base+path?param=书名，
        # 结果链接正则组1=书页 href（相对路径，resolve 时拼 base）
        "site_search": {
            "path": "/ar.php", "param": "keyWord", "method": "get",
            "link_re": re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,40})</a>', re.S),
            "exact": True,   # 链接文本需与书名完全一致才命中
        },
        # 两段书号：book_id 存 "cat:bid"；章表分页 /index_{p}.html（p>1），每页 +20 章
        "book_id_re": r"/(\d+)/(\d+)/",
        "book_page": lambda b: f"/{b.split(':')[0]}/{b.split(':')[1]}/",
        "site_search": {
            "path": "/ar.php", "param": "keyWord", "method": "get",
            "link_re": re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,40})</a>', re.S),
            "exact": True,
        },
        "chapter_list_page": lambda b, p: (
            f"/{b.split(':')[0]}/{b.split(':')[1]}/index_{p}.html"
            if p > 1 else f"/{b.split(':')[0]}/{b.split(':')[1]}/"),
        "chapter_url": lambda b, cid, suf: f"/{b.split(':')[0]}/{b.split(':')[1]}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/\d+/\d+/(\d+)\.html)"[^>]*>(.*?)</a>', re.S),
        # 本章续页（正文分页 {cid}_2.html）：{cid} 替换为当前 chapter_id，组1=续页号
        "extra_page_re": r'href="[^"]*?/{cid}_(\d+)\.html"',
        "content_mode": "after_title",   # 正文在 h1.title 后
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # UU看书：书页只列最近几章，全目录在 /chapter/{bid}.html（页参忽略：第1页即全量，重复页无新 href 自动停）
    "uukan": {
        "name": "UU看书",
        "base_url": "https://www.uukan.org",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        # 站内搜索 /search?searchkey= 常被 Cloudflare 拦（401）；配置上以便偶发可用，失败自动回退 Bing
        "site_search": {
            "path": "/search", "param": "searchkey", "method": "get",
            "link_re": re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,40})</a>', re.S),
            "exact": True,
        },
        "book_id_re": r"/book/([A-Za-z0-9_-]+)\.html",
        "book_page": lambda b: f"/book/{b}.html",
        "chapter_list_page": lambda b, p: f"/chapter/{b}.html",
        "chapter_url": lambda b, cid, suf: f"/chapter/{b}/{cid}{suf}.html",
        # 章号不连续且目录含「开始阅读」等链接 → 仅匹配含编号标题的章锚点（组1=href 组2=chapter_id）
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/chapter/[^"]+/([A-Za-z0-9_-]+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "content",
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 零点看书镜像(m.chensiwx)：/{cat}/{bid}/ 章表分页 /{cat}/{bid}_{p}/；正文 id=content 内含章 h1/分页头行需清理
    "chensiwx": {
        "name": "零点看书·辰巳镜像",
        "base_url": "http://m.chensiwx.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/(\d+)/(\d+)/",
        "book_page": lambda b: f"/{b.split(':')[0]}/{b.split(':')[1]}/",
        "site_search": {
            "path": "/ar.php", "param": "keyWord", "method": "get",
            "link_re": re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,40})</a>', re.S),
            "exact": True,
        },
        "chapter_list_page": lambda b, p: (
            f"/{b.split(':')[0]}/{b.split(':')[1]}_{p}/" if p > 1
            else f"/{b.split(':')[0]}/{b.split(':')[1]}/"),
        "chapter_url": lambda b, cid, suf: f"/{b.split(':')[0]}/{b.split(':')[1]}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/\d+/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r'href="[^"]*?/{cid}_(\d+)\.html"',
        "content_div_id": "content",
        "main_title_re": MAIN_TITLE_RE,
        # 容器内章 h1 / (第x/y页) / 纯章标题行清理（正文段落不以「第N章」开头且多以句号结尾）
        "drop_line_re": [
            r"^第[0-9一二三四五六七八九十百千零两]+章[^\n。]*$",
            r"^第[0-9一二三四五六七八九十百千零两]+章.*?[（(]第\d+/\d+页[)）]$",
            r"^[（(]第\d+/\d+页[)）]$",
            r"^[（(]本章未完.*?[)）]$",
        ],
        "request_delay": 0.5,
    },
    # 无极小说：书页即全量目录（`/{bid}/`，bid 为 `cat_bid` 下划线格式），正文 <div id="content">
    # 静态内嵌；站内搜索 /search.html?name=（命中「书名」精确链接）。2026-09 实测《十日终焉》
    # 书页 1359 章、正文广告行可被 _AD_LINE_RE 过滤。
    "wujixsw": {
        "name": "无极小说",
        "base_url": "https://wujixsw.info",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        # 书号：`151_151404`（下划线两段）；book_page 用 `/{bid}/`
        "book_id_re": r"/(\d+_\d+)/",
        "site_search": {
            "path": "/search.html", "param": "name", "method": "get",
            "link_re": re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,40})</a>', re.S),
            "exact": True,
        },
        "book_page": lambda b: f"/{b}/",
        "chapter_list_page": lambda b, p: f"/{b}/",   # 书页即全量目录，忽略页参
        "chapter_url": lambda b, cid, suf: f"/{b}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/[^"]*?/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "content",
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 互书阁：书页/目录静态可抓（全目录在 /index/{bid}/），正文 <div id="article"> 由 JS 填充 →
    # content_render 走无头浏览器渲染后按 content_div_id=article 解析
    "hushuge": {
        "name": "互书阁",
        "base_url": "https://www.hushuge.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/book/(\d+)/",
        "book_page": lambda b: f"/book/{b}/",
        "chapter_list_page": lambda b, p: f"/index/{b}/",
        "chapter_url": lambda b, cid, suf: f"/read/{b}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/read/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r'href="[^"]*?/read/\d+/{cid}_(\d+)\.html"',
        "content_div_id": "article",
        "content_render": True,
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 飘花阅读(m 站)：章表分页 /piao/{bid}_{p}/；正文 /piaof/{bid}/{cid}.html 由 JS 填充（渲染后容器 #nr）
    "piaofeige": {
        "name": "飘花阅读",
        "base_url": "http://m.piaofeige.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/piao/(\d+)/",
        "book_page": lambda b: f"/piao/{b}/",
        "chapter_list_page": lambda b, p: (f"/piao/{b}_{p}/" if p > 1 else f"/piao/{b}/"),
        "chapter_url": lambda b, cid, suf: f"/piaof/{b}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/piaof/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r'href="[^"]*?/piaof/\d+/{cid}_(\d+)\.html"',
        "content_div_id": "nr",
        "content_render": True,
        "drop_line_re": [
            r"^第[0-9一二三四五六七八九十百千零两]+章.*?[（(]第\d+/\d+页[)）]$",
            r"^[（(]第\d+/\d+页[)）]$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 精彩小说网：书页只列近章，全目录 /book/{b}/{p}/（每页近 20 章），正文 /read/{b}/{cid}/ 容器 #novelbody
    "jcxs": {
        "name": "精彩小说网",
        "base_url": "https://www.jcxs.org",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/book/(\d+)/",
        "book_page": lambda b: f"/book/{b}/",
        "chapter_list_page": lambda b, p: (f"/book/{b}/{p}/" if p > 1 else f"/book/{b}/"),
        "chapter_url": lambda b, cid, suf: f"/read/{b}/{cid}{suf}/",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/read/\d+/(\d+)/)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "novelbody",
        # 容器头部导航噪音（字体/护眼/页签/上下章）与底部阅读模式提示过滤
        "drop_line_re": [
            r"^第[0-9一二三四五六七八九十百千零两]+章[（(]第\d+/\d+页[)）]-《[^》]+》$",
            r"^第[0-9一二三四五六七八九十百千零两]+章[（(]第\d+页[)）]-《[^》]+》$",
            r"^—{5,}$",
            r"^字体(大中小|设置).*$",
            r"^关灯护眼$",
            r"^第[0-9一二三四五六七八九十百千零两]+章[（(]第\d+/\d+页[)）]$",
            r"^第[0-9一二三四五六七八九十百千零两]+章[（(]第\d+页[)）]$",
            r"^[（(]第\d+/\d+页[)）]$",
            r"^[（(]第\d+页[)）]$",
            r"^(上一章|下一章|目录|记录|加入书签)$",
            r"^脑子寄存处.*$",
            r"^请(关闭浏览器阅读模式|关闭阅读模式|下载APP|安装客户端).*$",
            r"^如无法翻页.*$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 迷乐文学：书页全量目录（`/{cat}/{bid}/`），章链接为绝对 URL /{cat}_{bid}/{cid}.html，正文容器 #booktxt 含分页 {cid}_N.html
    "mele6": {
        "name": "迷乐文学",
        "base_url": "https://www.mele6.net",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/(\d+)/(\d+)/",
        "book_page": lambda b: f"/{b.split(':')[0]}/{b.split(':')[1]}/",
        "chapter_list_page": lambda b, p: f"/{b.split(':')[0]}/{b.split(':')[1]}/",  # 书页即全量目录
        "chapter_url": lambda b, cid, suf: f"/{b.split(':')[0]}_{b.split(':')[1]}/{cid}{suf}.html",
        # 章链接为绝对 URL 或相对路径：`/283_283487/{cid}.html`（书号段 `{cat}_{bid}`）
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(?:https?://[^"/]*)?(/\d+_\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r'href="[^"]*?/{cid}_(\d+)\.html"',
        "content_div_id": "booktxt",
        "drop_line_re": [
            r"^第[0-9一二三四五六七八九十百千零两]+章.*?（\d+ / \d+）$",
            r"^（\d+ / \d+）$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 三六零小说：目录 /mulu/{cat}/{bid}.html（静态 gbk，cat=bid 前3位；书页仅列近章），章 /mulu/{cat}/{bid}-{cid}.html，正文 #read_content
    "i360xs": {
        "name": "三六零小说",
        "base_url": "https://www.i360xs.com",
        "encoding": "gbk",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/book/(\d+)\.html",
        "book_page": lambda b: f"/book/{b}.html",
        "chapter_list_page": lambda b, p: f"/mulu/{b[:3]}/{b}.html",
        "chapter_url": lambda b, cid, suf: f"/mulu/{b[:3]}/{b}-{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(?:https?://[^"/]*)?(/mulu/[^"]*-(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "read_content",
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 甲骨文小说：书页即全量目录（1272 章一页），章 /chapter/{bid}/{n}，正文 class="text"
    "jgwxs": {
        "name": "甲骨文小说",
        "base_url": "https://www.jgwxs.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/novel/(\d+)",
        "book_page": lambda b: f"/novel/{b}",
        "chapter_list_page": lambda b, p: f"/novel/{b}",
        "chapter_url": lambda b, cid, suf: f"/chapter/{b}/{cid}{suf}",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/chapter/\d+/(\d+))"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_class": "content",
        # 头部书名行「《书名》—— 作者」过滤
        "drop_line_re": [
            r"^《[^》]+》——\s*$",
            r"^《[^》]+》—— [^ ]+$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 苦读书（www）：目录 /html/{cat}/{bid}/index.html 全量，章 /html/{cat}/{bid}/{cid}.html，正文 #content
    "kudushu": {
        "name": "苦读书",
        "base_url": "https://www.kudushu.org",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/html/(\d+)/(\d+)/",
        "book_page": lambda b: f"/html/{b.split(':')[0]}/{b.split(':')[1]}/index.html",
        "chapter_list_page": lambda b, p: f"/html/{b.split(':')[0]}/{b.split(':')[1]}/index.html",
        "chapter_url": lambda b, cid, suf: f"/html/{b.split(':')[0]}/{b.split(':')[1]}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="([^"]*/html/\d+/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "content",
        # 头部「(苦读书 www.kudushu.org)」站点广告（与正文同段，全局替换）
        "ad_replace": {
            r"[（(]苦读书 www\.kudushu\.org[)）]\s*": "",
            r"[（(]苦读书 www\.kudushu\.org[)）]": "",
        },
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 苍穹书社：目录 /book/{bid}/ 列近章，章 /read/{bid}/{cid}.html，正文 #chaptercontent（头部有站点广告残留需 drop）
    "cssqs": {
        "name": "苍穹书社",
        "base_url": "https://www.cssqs.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/book/(\d+)/",
        "book_page": lambda b: f"/book/{b}/",
        "chapter_list_page": lambda b, p: f"/book/{b}/",
        "chapter_url": lambda b, cid, suf: f"/read/{b}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/read/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "chaptercontent",
        # 头部站点广告行过滤
        "drop_line_re": [
            r"^天才一秒记住.*$",
            r"^请记住本书首发域名.*$",
            r"^https?://.*$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 希冀小说网（m）：目录 /info/{bid}/ 列近章，章 /info/{bid}/{cid}.html，正文 #chaptercontent，分页 {cid}_N.html
    "xiji": {
        "name": "希冀小说网",
        "base_url": "https://m.xi-ji.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/info/(\d+)/",
        "book_page": lambda b: f"/info/{b}/",
        "chapter_list_page": lambda b, p: f"/info/{b}/",
        "chapter_url": lambda b, cid, suf: f"/info/{b}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/info/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"/{cid}_(\d+)\.html",
        "content_div_id": "chaptercontent",
        # 章内页签（第N章 标题 1/4）与尾部本文链接过滤
        "drop_line_re": [
            r"^第[0-9一二三四五六七八九十百千零两]+章.*[（(]\d+/\d+[)）]$",
            r"^[（(]\d+/\d+[)）]$",
            r"^本文链接:.*$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 九若小说：目录 /book/{bid}/ 列近章，章 /book/{bid}/{cid}.html，正文 #chaptercontent（正文含全角广告字符，drop 过滤）
    "9rxs": {
        "name": "九若小说",
        "base_url": "https://www.9rxs.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/book/(\d+)/",
        "book_page": lambda b: f"/book/{b}/",
        "chapter_list_page": lambda b, p: f"/book/{b}/",
        "chapter_url": lambda b, cid, suf: f"/book/{b}/{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/book/\d+/(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_div_id": "chaptercontent",
        # 全局广告串替换：站内混淆 URL 广告嵌在正文行内（`ｌ=ａ_ｎｌａｎ`ｗ*ｅｎｘ$ｕ@ｅ.|ｃｏｍ`）
        "ad_replace": {
            r"[ｌＬ][＝=][ａＡ]_[ｎＮ][ｌＬ][ａＡ][ｎＮ][`｀][ｗＷ]\*[ｅＥ][ｎＮ][ｘＸ]\$[ｕＵ]@[ｅＥ][．.|。][|｜][ｃＣ][ｏＯ][ｍＭ]": "",
            r"^第[0-9一二三四五六七八九十百千零两]+章[^\n]{0,40}?(?=[\u4e00-\u9fff])": "",
        },
        "drop_line_re": [
            r"^[『【\[].*(点此报错|加入书签).*[』】\]]$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 爱下电子书（繁体）：书页列近章，章 /read/{bid}/p{n}.html，正文 class="page-content"
    "ixdzs": {
        "name": "爱下电子书",
        "base_url": "https://ixdzs.hk",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA},
        "book_id_re": r"/read/(\d+)/",
        "book_page": lambda b: f"/read/{b}/",
        "chapter_list_page": lambda b, p: f"/read/{b}/",
        "chapter_url": lambda b, cid, suf: f"/read/{b}/p{cid}{suf}.html",
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/read/\d+/p(\d+)\.html)"[^>]*>'
            r'([^<]*第[0-9一二三四五六七八九十百千零两]+章[^<]*)</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_class": "page-content",
        # 头部重复章节标题（h3 与正文首行）过滤
        "drop_line_re": [
            r"^第[0-9一二三四五六七八九十百千零两]+章.*$",
        ],
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.5,
    },
    # 土豆小说网：书页 /site/detail?id={bid} 全量目录（1400+ 章），章 /site/chapter?id={cid}（需 Referer），正文 class="entry-content"
    "spudnovel": {
        "name": "土豆小说网",
        "base_url": "https://spudnovel.com",
        "encoding": "utf-8",
        "headers": {"User-Agent": DEFAULT_UA,
                    "Referer": "https://spudnovel.com/"},
        "book_id_re": r"/site/detail\?id=(\d+)",
        "book_page": lambda b: f"/site/detail?id={b}",
        "chapter_list_page": lambda b, p: f"/site/detail?id={b}",
        "chapter_url": lambda b, cid, suf: f"/site/chapter?id={cid}{suf}",
        # 书页含全部 site/chapter?id= 链接（标题含缩进换行，放宽长度上限）
        "chapter_link_re": re.compile(
            r'<a[^>]*href="(/site/chapter\?id=(\d+))"[^>]*>([^<]{1,120})</a>', re.S),
        "extra_page_re": r"(?!)",
        "content_class": "entry-content",
        "main_title_re": MAIN_TITLE_RE,
        "request_delay": 0.6,
    },
}

# ─── 多镜像源注册表（下载时并行尝试） ──────────────────────────────────
# 静态可抓站收 requests；正文 JS 填充站标 content_render 走浏览器渲染（playwright，见 browser_render）。
MIRROR_SOURCES = {
    "wodushu": lambda: WebnovelCrawler("wodushu"),
    "bookszw": lambda: BookszwCrawler(),
    "uukan": lambda: WebnovelCrawler("uukan"),
    "chensiwx": lambda: WebnovelCrawler("chensiwx"),
    "wujixsw": lambda: WebnovelCrawler("wujixsw"),
    "hushuge": lambda: WebnovelCrawler("hushuge"),
    "piaofeige": lambda: WebnovelCrawler("piaofeige"),
    "jcxs": lambda: WebnovelCrawler("jcxs"),
    "mele6": lambda: WebnovelCrawler("mele6"),
    "i360xs": lambda: WebnovelCrawler("i360xs"),
    "jgwxs": lambda: WebnovelCrawler("jgwxs"),
    "kudushu": lambda: WebnovelCrawler("kudushu"),
    "cssqs": lambda: WebnovelCrawler("cssqs"),
    "xiji": lambda: WebnovelCrawler("xiji"),
    "9rxs": lambda: WebnovelCrawler("9rxs"),
    "ixdzs": lambda: WebnovelCrawler("ixdzs"),
    "spudnovel": lambda: WebnovelCrawler("spudnovel"),
}


class _ContentExtractor(HTMLParser):
    """提取指定 id 的 div 内纯文本：块级标签转行、剔除 script/style。"""

    _BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, div_id: str = None, div_class: str = None):
        super().__init__(convert_charrefs=True)
        self.div_id = div_id
        self.div_class = div_class
        self.in_target = False
        self.depth = 0
        self.skip = 0
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        d = dict(attrs)
        if tag in ("div", "article", "section") and not self.in_target:
            if self.div_id and d.get("id") == self.div_id:
                self.in_target = True
                self.depth = 1
                return
            if self.div_class:
                # 精确 token 匹配；无精确时退回「class 含该前缀 token」（如 jgwxs 的 text-subtitle1 定位正文）
                toks = (d.get("class") or "").split()
                if self.div_class in toks or any(
                        t.startswith(self.div_class) for t in toks):
                    self.in_target = True
                    self.depth = 1
                    return
        if self.in_target:
            if tag in ("div", "article", "section"):
                self.depth += 1
            if tag in self._BLOCK_TAGS:
                self._nl()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.in_target:
            return
        if tag in ("div", "article", "section"):
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
    def _fetch(self, path: str, timeout: int = 15, retries: int = 3) -> Optional[str]:
        """GET base_url+path，瞬态失败小退避重试 3 次（30 分钟大下载中途不因抖动崩掉）。

        retries=0：单次请求即返回（章表探测用；失败页由 get_chapter_list 判断后自行处理）。"""
        url = path if path.startswith("http") else self.cfg["base_url"] + path
        for attempt in range(max(1, retries)):
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
        """由书 URL 或 book_id 解析书页，取 og:novel:* meta → 元信息 dict。

        book_id_re 多段捕获时（如 bookszw `/{cat}/{bid}/`）用「:」拼接存 book_id。"""
        source = (url_or_book_id or "").strip()
        if not source:
            raise RuntimeError("请提供书籍 URL 或 book_id")
        m = re.search(self.cfg["book_id_re"], source)
        book_id = ":".join(m.groups()) if m else source
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

    # ── 书名 → 本站书页 URL（站内搜索优先；无搜索/搜不到时用 Bing） ──
    def search_book_url(self, title: str) -> Optional[str]:
        """按书名定位本站书页 URL。

        优先走站内搜索（SITES[site].site_search 配置：GET base+path?param=书名，
        link_re 组1=书页 href，exact=True 要求链接文本与书名完全一致）；
        未配置 / 无命中 → 回退 Bing「书名 site:本站」。
        返回书页 URL（相对路径或绝对路径均可，resolve_book 都能处理）。"""
        from urllib.parse import urlencode
        if not title:
            return None
        ss = self.cfg.get("site_search")
        if ss:
            try:
                url = self.cfg["base_url"] + ss["path"]
                if str(ss.get("method", "get")).lower() == "post":
                    r = self.session.post(url, data={ss["param"]: title}, timeout=12)
                else:
                    r = self.session.get(url, params={ss["param"]: title}, timeout=12)
                if r.status_code == 200:
                    r.encoding = self.cfg.get("encoding", "utf-8")
                    html = r.text
                    link_re = ss.get("link_re")
                    if link_re is None:
                        link_re = re.compile(
                            r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,40})</a>', re.S)
                    exact = bool(ss.get("exact", True))
                    for m in link_re.finditer(html):
                        href, text = m.group(1), (m.group(2) or "").strip()
                        if exact:
                            if text == title:
                                return href
                        else:
                            if title in text:
                                return href
            except Exception as e:
                logger.warning("site_search failed: %s (%s)", self.site, e)
        # 回退：Bing 搜「书名 site:本站」
        import base64
        from urllib.parse import urlparse
        netloc = urlparse(self.cfg["base_url"]).netloc
        try:
            r = self.session.get("https://cn.bing.com/search",
                                 params={"q": f'"{title}" site:{netloc}', "form": "QBRE"},
                                 timeout=12)
            if r.status_code != 200:
                return None
            r.encoding = "utf-8"
            html = r.text
            # 1) Bing 跳转参数 u=a1%3a{base64} → base64 解码出真实 URL
            for m in re.finditer(r"u=a1%3a([A-Za-z0-9+/=]+)", html):
                try:
                    u = base64.b64decode(m.group(1)).decode("utf-8", "ignore")
                    if re.search(self.cfg["book_id_re"], u):
                        return self._normalize_url(u)
                except Exception:
                    continue
            # 2) 直链
            for m in re.finditer(r"https?://[^\"'& <]+", html):
                u = m.group(0)
                if re.search(self.cfg["book_id_re"], u):
                    return self._normalize_url(u)
        except Exception as e:
            logger.warning(f"search_book_url failed: {title} {e}")
        return None

    def _normalize_url(self, url: str) -> str:
        """将搜索命中的 URL 域名归一化到本 site base_url（Bing 常索引到旧域名/镜像域名，
        如 uukan 的旧域 sto66.com；book_id 路径一致时替换 netloc 即可直达）。"""
        try:
            from urllib.parse import urlparse, urlunparse
            p = urlparse(url)
            b = urlparse(self.cfg["base_url"])
            if p.netloc and b.netloc and p.netloc != b.netloc:
                return urlunparse((p.scheme or b.scheme, b.netloc, p.path, p.params, p.query, p.fragment))
        except Exception:
            pass
        return url

    # ── 章表 ──
    def get_chapter_list(self, book_id: str, max_pages: int = 200) -> list[dict]:
        """遍历分页章表 → 跨页去重 → 带编号按数字升序、番外排尾。

        返回 [{chapter_id, title, href, num}]（未赋 index，由 download_webnovel 过滤后编号）。
        终止条件：某页 0 个新 href（实测末页后重复返回 HTTP 200，不 404）。

        性能：bookszw/chensiwx 等大书（3000+ 章）章表分 150 页、服务器单页响应 ~0.74s，
        串行拉全约 110-125s（并发必被 429 限流丢章，不可用）。此处用「第 1 页探测总页数
        → 只翻已知页 + 失败页补拉」，减少空翻且不丢章。非分页源走串行原逻辑。
        """
        link_re = self.cfg["chapter_link_re"]

        def _num_rank(c):
            return 0 if _parse_chapter_num(c["title"]) is not None else 1

        def _add_page(html, by_href):
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
            return new

        by_href: dict = {}   # 同 href 去重：页顶「开始阅读」按钮常与真正的「第1章」指向同一章，
        #                    无编号标题排后，重复时替换为带「第N章」编号的那条
        first = self._fetch(self.cfg["chapter_list_page"](book_id, 1))
        if not first:
            return []
        _add_page(first, by_href)
        # 探测总页数：bookszw/chensiwx 第 1 页含全部分页链接 → 只翻已知页，省去探测性空翻
        page_nums = []
        for m in re.finditer(r'index_(\d+)\.html', first):
            page_nums.append(int(m.group(1)))
        if ":" in book_id:
            bid = book_id.split(":")[1]
            for m in re.finditer(re.escape(bid) + r'_(\d+)/', first):
                page_nums.append(int(m.group(1)))
        if page_nums:
            pages = [p for p in range(2, min(max(page_nums), max_pages) + 1)]
            failed = []
            for p in pages:
                html = self._fetch(self.cfg["chapter_list_page"](book_id, p), retries=0)
                if html:
                    _add_page(html, by_href)
                else:
                    failed.append(p)
            # 失败页统一补拉（带短间隔，避限流）
            for p in failed:
                html = self._fetch(self.cfg["chapter_list_page"](book_id, p))
                if html:
                    _add_page(html, by_href)
        else:
            # 无分页线索 → 串行翻页（原逻辑，失败页重试一次不中断）
            for page in range(2, max_pages + 1):
                html = self._fetch(self.cfg["chapter_list_page"](book_id, page), retries=0)
                if not html:
                    html = self._fetch(self.cfg["chapter_list_page"](book_id, page))
                if not html:
                    break
                if _add_page(html, by_href) == 0:
                    break   # 末页后重复页 → 终止
        if not by_href:
            return []
        catalog = list(by_href.values())
        # 编号解析：第N章（阿拉伯/中文数字）→ num；番外 → None。带编号按数字升序，番外排尾。
        for c in catalog:
            c["num"] = _parse_chapter_num(c["title"])
        numbered = sorted([c for c in catalog if c["num"] is not None],
                          key=lambda c: c["num"])
        extras = [c for c in catalog if c["num"] is None]
        return numbered + extras

    # ── 单章正文 ──
    def _page_html(self, path: str) -> Optional[str]:
        """取一页 HTML：静态站用 requests；content_render 站（正文 JS 填充）用无头浏览器渲染。"""
        if self.cfg.get("content_render"):
            try:
                from plugins.browser_render import render_html
                url = path if path.startswith("http") else self.cfg["base_url"] + path
                return render_html(url, wait_sel=self.cfg.get("content_div_id") or "")
            except Exception as e:
                logger.warning("render fetch failed: %s (%s)", path, e)
                return None
        return self._fetch(path)

    def download_chapter(self, book_id: str, chapter_id: str) -> str:
        """抓单章正文：拼接 {cid}.html + 续页 {cid}_N.html（无续页链接即止），清洗返回。"""
        parts: list[str] = []
        suffix = ""
        max_pages = 30   # 安全上限：单章不可能有几十页
        extra_re = self.cfg["extra_page_re"].format(cid=re.escape(chapter_id))
        for _ in range(max_pages):
            path = self.cfg["chapter_url"](book_id, chapter_id, suffix)
            html = self._page_html(path)
            if not html:
                break
            text = self._extract_content(html)
            if text:
                parts.append(text)
            # 只跟随「页码更大」的续页（兼容 bookszw 双向分页：页2 有回 _1 的上一页链接，
            # 若匹配任意 _N 会在页1↔页2 死循环）
            cur = int(suffix.lstrip("_")) if suffix else 1
            nxt = None
            for m in re.finditer(extra_re, html):
                if int(m.group(1)) > cur:
                    nxt = m
                    break
            if not nxt:
                break
            suffix = f"_{nxt.group(1)}"
        text = self._clean_text("\n".join(parts))
        drop = self.cfg.get("drop_line_re")
        if drop:
            dr = re.compile("|".join(drop)) if isinstance(drop, (list, tuple)) else re.compile(drop)
            text = "\n\n".join(b for b in text.split("\n\n") if not dr.search(b))
        return text

    def _extract_content(self, html: str) -> str:
        if self.cfg.get("content_mode") == "after_title":
            return self._extract_after_title(html)
        parser = _ContentExtractor(
            self.cfg.get("content_div_id"),
            self.cfg.get("content_class"),
        )
        parser.feed(html)
        return parser.text()

    def _extract_after_title(self, html: str) -> str:
        """bookszw 等：<h1 class="title"> 后紧跟「第N章 标题 (第X/Y页)」+ <br> 分隔正文，
        直到页脚/上一章下一章/分页容器。正文静态内嵌（非 JS 填充）。"""
        m = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>', html, re.S)
        if not m:
            return ""
        seg = html[m.end():]
        # 截断点用「加入书签」（书签锚点文本，其前标签完整可剥离）；btn-addbs 是属性值会截在标签内留残片
        cut = re.search(r'(上一章|下一章|加入书签|section-opt'
                        r'|<div[^>]*id="[^"]*(footer|foot|page)[^"]*"'
                        r'|<div[^>]*class="[^"]*(page|chapterbar|bottem)[^"]*")', seg)
        if cut:
            seg = seg[:cut.start()]
        seg = re.sub(r'<script.*?</script>', '', seg, flags=re.S)
        txt = re.sub(r'<br\s*/?>', '\n', seg)
        txt = re.sub(r'<[^>]+>', '', txt)
        # 去「第N章 标题 (第X/Y页)」头行 + 「（本章未完…）」分页尾
        txt = re.sub(r'^\s*第[0-9一二三四五六七八九十百千零两]+章.*?\(第\d+/\d+页\)\s*', '', txt)
        txt = re.sub(r'[（(]本章未完.*?[）)]', '', txt)
        return _html.unescape(txt)

    # ── 清洗 ──
    def _clean_text(self, text: str) -> str:
        # 站点级全局广告串替换（9rxs 等站广告嵌在行内，逐行过滤无效）
        for pat, rep in (self.cfg.get("ad_replace") or {}).items():
            text = re.sub(pat, rep, text)
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


class BookszwCrawler(WebnovelCrawler):
    """零点看书（笔趣阁克隆）：`/{cat}/{bid}/` 数字两段 URL，正文在 <h1 class="title"> 后。

    配置（SITES["bookszw"]）已覆盖：章表分页 /index_{p}.html、正文分页 {cid}_2.html、
    content_mode=after_title。此类为 MIRROR_SOURCES 提供独立构造入口，后续如需特化可 override。
    """

    def __init__(self, verify: bool = True):
        super().__init__(site="bookszw", verify=verify)


def download_webnovel(site: str = "wodushu", url: str = "", book_id: str = "",
                      chapters: int = 0, start_chapter: int = 1, end_chapter: int = 0,
                      download_delay: float = 0.5, on_progress=None,
                      platform: str = "web") -> tuple:
    """下载网页镜像站小说 → storage/novels/{platform}/。MCP / Web / CLI 统一入口。

    chapters<=0（默认）→ 全文（从 start_chapter 到目录尾，可按 main_title_re 过滤番外）；
    chapters>0 → 按列表序号区间（第1章=1）。先建目录+info.json（/scout 立即显示），
    再逐章 save_chapter 渐进落盘（停止保留已抓）。返回 (info, {"folder", "chapters"})。
    """
    import json as _json
    import re as _re
    if not url and not book_id:
        raise RuntimeError("请提供书籍 URL 或 book_id")
    crawler = WebnovelCrawler(site=site)
    info = crawler.resolve_book(url or book_id)
    catalog = crawler.get_chapter_list(info["book_id"])
    main_re = crawler.cfg.get("main_title_re")
    if main_re:
        filtered = [c for c in catalog if _re.match(main_re, c["title"])]
        # 过滤回退：主书过滤把全书滤成 0 章（个别书章标题格式不符）→ 禁用过滤保留全部
        if filtered:
            catalog = filtered
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

    from plugins.novel_storage import save_novel, save_chapter, NOVELS_DIR, _safe_name
    # 查重：同平台同书名已存在 → 复用目录（保留首见源 info.json，避免 book_id/site 被覆盖），
    # 只补缺章；不同源同书名也复用目录不产生第二条目。
    folder = _safe_name(info["title"])
    existing_info = NOVELS_DIR / platform / folder / "info.json"
    if existing_info.exists():
        old_site = "?"
        try:
            old_site = _json.loads(existing_info.read_text(encoding="utf-8")).get("site", "?")
        except Exception:
            pass
        if old_site == site and on_progress:
            on_progress("search", 1, 1, f"已存在: {info['title']}（复用目录，补缺章）")
        elif old_site != site and on_progress:
            on_progress("search", 1, 1, f"同书名已存在(来源 {old_site})，复用目录补充缺章")
    else:
        save_novel(platform, {
            "title": info["title"], "author": info["author"],
            "book_id": f"{site}:{info['book_id']}", "url": info["url"],
            "genre": info.get("genre", ""), "chapter_count": total,
            "site": site,
        }, [])
    # 断点续下：跳过已落盘章节（下载中断后从缺章续抓，避免重下已完成的）
    ch_dir = NOVELS_DIR / platform / folder / "chapters"
    existing: set[int] = set()
    if ch_dir.is_dir():
        for f in ch_dir.glob("*.json"):
            try:
                existing.add(int(f.stem))
            except ValueError:
                pass
    pending = [c for c in selected if c["index"] not in existing]
    skipped = len(selected) - len(pending)
    if not pending:
        # 已是最新：无需下载，不产生重复条目
        if on_progress:
            on_progress("search", 1, 1, f"已是最新（{len(existing)} 章），无需下载")
        return info, {"folder": folder, "chapters": 0, "already": True,
                      "skipped": len(existing)}
    if on_progress and skipped:
        on_progress("search", 1, 1, f"已下载 {skipped} 章，续下 {len(pending)} 章")

    downloaded = 0
    for i, ch in enumerate(pending):
        content = crawler.download_chapter(info["book_id"], ch["chapter_id"])
        if content and content.strip():
            save_chapter(platform, folder, {
                "index": ch["index"], "title": ch["title"],
                "content": content,
                "word_count": len(_re.findall(r"[一-鿿]", content)),
            })
            downloaded += 1
        if on_progress:
            on_progress("download", i + 1, len(pending), ch["title"][:30])
        if i < len(pending) - 1:
            time.sleep(download_delay)

    if on_progress:
        on_progress("download", len(pending), len(pending),
                    f"下载完成 {downloaded}章（本次新增）")
    return info, {"folder": folder, "chapters": downloaded, "already": False,
                  "skipped": skipped}


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
