"""
番茄小说侦察兵（Fanqie Scout Agent）
从番茄平台爬取热榜小说 → LLM拆解 → 沉淀到四大库

流程：
  热榜发现 → 下载前N章 → 逐书分析 → 提取桥段/大纲/笑点/内涵 → 入库
"""
import json
import os
import re
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

from plugins import BasePlugin, ScrapedMaterial

logger = logging.getLogger("fanqie-scout")


# ═══════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════

@dataclass
class NovelInfo:
    """小说基本信息"""
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


@dataclass
class ScoutResult:
    """一次侦察的完整结果"""
    source_books: list[NovelInfo] = field(default_factory=list)
    new_plots: list[dict] = field(default_factory=list)
    new_structures: list[dict] = field(default_factory=list)
    new_gags: list[dict] = field(default_factory=list)
    new_themes: list[dict] = field(default_factory=list)
    downloaded_chapters: int = 0
    analysis_cost: float = 0.0


# ═══════════════════════════════════════
# 爬虫核心
# ═══════════════════════════════════════

class FanqieCrawler:
    """番茄小说爬虫"""

    BASE_URL = "https://fanqienovel.com"
    API_BASE = "https://fanqienovel.com/api"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://fanqienovel.com/",
    }

    # 番茄的品类映射
    GENRE_MAP = {
        1: "玄幻", 2: "都市", 3: "历史", 4: "武侠",
        5: "科幻", 6: "悬疑", 7: "游戏", 8: "轻小说",
        9: "短篇", 10: "现实",
    }

    def __init__(self, cache_dir: str = "storage/fanqie_cache"):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.session.verify = False  # 跳过SSL验证（Windows旧证书兼容）
        self.session.trust_env = False  # 不用系统代理，直连访问
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._decoder = None  # lazy init

    def _init_decoder(self):
        if self._decoder is None:
            from plugins.font_decoder import FanqieDecoder, load_mapping
            self._decoder = FanqieDecoder()
            self._cached_mapping = {}
            # 加载预生成的映射表（可能有多个字体）
            for mp in Path("storage").glob("font_mapping*.json"):
                try:
                    self._cached_mapping.update(load_mapping(str(mp)))
                except Exception:
                    pass

    def discover_hot(self, genre_id: int = 0, count: int = 10) -> list[NovelInfo]:
        """发现热榜小说"""
        novels = []

        # 先尝试 API
        try:
            novels = self._api_hot_list(genre_id, count)
        except Exception as e:
            logger.warning(f"API hot list failed: {e}")

        # 如果 API 失败，尝试网页抓取
        if not novels:
            try:
                novels = self._web_hot_list(genre_id, count)
            except Exception as e:
                logger.warning(f"Web hot list failed: {e}")

        return novels[:count]

    def search_novel(self, title: str) -> Optional[NovelInfo]:
        """按书名搜索——Bing搜索 + 页面解析 + fanqie搜索页兜底"""
        import urllib.parse, unicodedata
        
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

        print(f"[搜索] 开始搜索 '{title[:30]}' -> {len(queries)}种查询" + str([q[:30] for q in queries]))
        for q in queries:
            try:
                bing_hosts = ["https://cn.bing.com", "https://www.bing.com"]
                r = None
                last_err = None
                for host in bing_hosts:
                    try:
                        r = self.session.get(
                            f"{host}/search?q={urllib.parse.quote(q)}",
                            timeout=10,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                        if r.status_code == 200:
                            break
                    except Exception as e:
                        last_err = e
                        continue
                if r is None:
                    print(f"[搜索] 无法连接Bing: {last_err}")
                    continue
                ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
                if ids:
                    seen = set()
                    unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
                    info = self._get_novel_from_page(unique_ids[0])
                    if info and info.title:
                        logger.info(f"搜到: {info.title} (ID={info.book_id})")
                        return info
            except Exception as e:
                logger.debug(f"搜索 '{q[:30]}': {e}")
                continue

        # 兜底：直接用番茄搜索页 URL（浏览器能搜到的都行）
        try:
            import urllib.parse as _up
            search_url = f"https://fanqienovel.com/search/{_up.quote(title)}"
            r = self.session.get(search_url, timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            ids = re.findall(r'fanqienovel\.com/page/(\d+)', r.text)
            if ids:
                seen = set()
                unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
                info = self._get_novel_from_page(unique_ids[0])
                if info and info.title:
                    logger.info(f"番茄搜索页兜底成功: {info.title} (ID={info.book_id})")
                    return info
        except Exception as e:
            logger.debug(f"番茄搜索页兜底: {e}")

        logger.warning(f"搜索失败: {title}")
        return None
        return None

    def _get_novel_from_page(self, book_id: str) -> Optional[NovelInfo]:
        """从番茄书籍页面提取信息（解析SSR数据）"""
        try:
            r = self.session.get(
                f"{self.BASE_URL}/page/{book_id}",
                timeout=15,
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
        )

    def _api_hot_list(self, genre_id: int, count: int) -> list[NovelInfo]:
        """通过 API 获取热榜"""
        url = f"{self.API_BASE}/author/library/book_list/v0"
        params = {
            "page_index": 0,
            "page_size": min(count, 30),
            "filter_type": 3,  # 3 = 热榜
            "order": 1,        # 1 = 按热度
        }
        if genre_id > 0:
            params["genre_type"] = genre_id

        resp = self.session.get(url, params=params, timeout=15)
        data = resp.json()

        novels = []
        items = data.get("data", {}).get("book_list", [])
        for item in items:
            info = item.get("book_info", item)
            novels.append(NovelInfo(
                book_id=str(info.get("book_id", "")),
                title=info.get("book_name", ""),
                author=info.get("author", ""),
                genre=self.GENRE_MAP.get(info.get("genre_type", 0), ""),
                word_count=info.get("all_word_count", 0),
                chapter_count=info.get("all_chapter_count", 0),
                hot_score=info.get("read_count", 0),
                intro=info.get("abstract", ""),
                url=f"{self.BASE_URL}/page/{info.get('book_id','')}",
            ))
        return novels

    def _web_hot_list(self, genre_id: int, count: int) -> list[NovelInfo]:
        """网页抓取热榜（备用方案）"""
        url = f"{self.BASE_URL}/rank/hot"
        resp = self.session.get(url, timeout=15)
        text = resp.text

        novels = []
        # 从页面中提取小说信息
        pattern = r'book_id["\']?\s*[:=]\s*["\']?(\d+)'
        ids = re.findall(pattern, text)

        for bid in ids[:count]:
            info = self.get_novel_info(bid)
            if info:
                novels.append(info)

        return novels

    def get_novel_info(self, book_id: str) -> Optional[NovelInfo]:
        """获取单本书详细信息"""
        try:
            url = f"{self.API_BASE}/reader/book_info/v0"
            resp = self.session.get(url, params={"book_id": book_id}, timeout=10)
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
            )
        except Exception as e:
            logger.warning(f"Failed to get info for {book_id}: {e}")
            return None

    def get_chapter_list(self, book_id: str, max_count: int = 100) -> list[dict]:
        """获取章节目录 — 从书籍页SSR或API"""
        import json as _json

        # 方法1: 书籍页SSR → page.chapterListWithVolume
        try:
            r = self.session.get(f"{self.BASE_URL}/page/{book_id}", timeout=15,
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
                timeout=15,
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
            return cache_file.read_text(encoding="utf-8")

        try:
            r = self.session.get(
                f"{self.BASE_URL}/reader/{chapter_id}",
                timeout=15,
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
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\n{3,}', '\n\n', content)

                # PUA 字体解码
                self._init_decoder()
                if any(0xE000 <= ord(c) <= 0xF8FF for c in content[:100]):
                    if self._cached_mapping:
                        content = __import__('plugins.font_decoder', fromlist=['decode_with_mapping']).decode_with_mapping(
                            content, self._cached_mapping)
                    else:
                        content = self._decoder.decode_page(r.text)
                        # 剔除 HTML 标签（解码后可能残留）
                        content = re.sub(r'<[^>]+>', '', content)

                if content.strip():
                    cache_file.write_text(content, encoding="utf-8")

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
            return {"plots": [], "structures": [], "gags": [], "themes": []}

        samples = self._select_samples(chapters)

        result = {}

        if on_progress:
            on_progress("analyze", 1, 4, "提取桥段...")
        result["plots"] = self.extract_plots(novel, samples)

        if on_progress:
            on_progress("analyze", 2, 4, "提取大纲...")
        result["structures"] = self.extract_structure(novel, samples)

        if on_progress:
            on_progress("analyze", 3, 4, "提取笑点...")
        result["gags"] = self.extract_gags(novel, samples)

        if on_progress:
            on_progress("analyze", 4, 4, "提取内涵...")
        result["themes"] = self.extract_themes(novel, samples)

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
  {{"name":"桥段名", "category":"爽文/开篇/战斗/...",
   "structure":"[步骤1]→[步骤2]→...",
   "slots":[{{"name":"变量名","options":["选项1","选项2"]}}],
   "notes":"使用注意", "word_range":[800,2500]}}
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
        """提取大纲结构模式"""
        text = self._build_sample_text(samples, 2000)
        ch_count = novel.chapter_count or len(samples) * 10

        prompt = f"""分析番茄小说《{novel.title}》（{novel.genre}，约{ch_count}章）的章节结构，
提取出该流派的大纲骨架模式。

大纲骨架应包含：
1. 卷（Volume）划分：全书分几个大卷，每卷的核心任务
2. 弧（Arc）划分：每卷内的叙事弧线
3. 每段的关键事件列表

【小说内容样本】
{text}

返回 JSON：
{{"structures": [
  {{"name":"{novel.genre}标准结构",
   "total_chapters":{ch_count},
   "stages":[
     {{"name":"阶段名","description":"这个阶段做什么",
       "min_chapters":10,"max_chapters":20,
       "key_events":["事件1","事件2"]}}
   ]
  }}
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
   "scene_fit":["日常","战斗","对话"],
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

    def extract_themes(self, novel: NovelInfo, samples: list[dict]) -> list[dict]:
        """提取母题/内涵"""
        text = self._build_sample_text(samples, 1500)

        prompt = f"""分析以下小说的深层母题和内涵表达手法。

每个母题包括：
1. 母题名称（如"底层逆袭的尊严""知识改变命运"）
2. 母题描述
3. 在小说中的具体体现方式
4. 写作建议（如何在其他小说中复用）

【小说内容样本】
{text}

返回 JSON：
{{"themes": [
  {{"name":"母题名称","description":"描述",
   "expression":"在小说中的体现方式",
   "writing_tips":["写作建议1","写作建议2"],
   "compatible_genres":["玄幻","都市"]}}
]}}"""
        try:
            raw = self.llm.call("你是一位专业的文学分析学者。只返回JSON。",
                                prompt, temperature=0.5, max_tokens=4096)
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            return data.get("themes", [])
        except Exception as e:
            logger.warning(f"Theme extraction failed: {e}")
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
    """将分析结果导入四大库"""

    def __init__(self, plot_lib=None, struct_lib=None, gag_lib=None, theme_lib=None):
        self.plot_lib = plot_lib
        self.struct_lib = struct_lib
        self.gag_lib = gag_lib
        self.theme_lib = theme_lib

    def ingest(self, analysis: dict, source: str = "fanqie") -> dict:
        """导入分析结果到各库"""
        stats = {"plots": 0, "structures": 0, "gags": 0, "themes": 0}

        for plot in analysis.get("plots", []):
            if self.plot_lib:
                self._add_plot(plot, source)
                stats["plots"] += 1

        for struct in analysis.get("structures", []):
            if self.struct_lib:
                self._add_structure(struct, source)
                stats["structures"] += 1

        for gag in analysis.get("gags", []):
            if self.gag_lib:
                self._add_gag(gag, source)
                stats["gags"] += 1

        for theme in analysis.get("themes", []):
            if self.theme_lib:
                self._add_theme(theme, source)
                stats["themes"] += 1

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
            category=data.get("category",""), source=source,
            template_structure=data.get("structure",""),
            slots=slots,
            usage_notes=data.get("notes",""),
            word_range=tuple(data.get("word_range", [800, 2500])),
        )
        self.plot_lib.templates.append(template)

    def _add_structure(self, data: dict, source: str):
        from libraries.structure import StageNode, StructureTemplate
        sid = f"scout_{source}_{data.get('name','unknown')}"
        for t in self.struct_lib.templates:
            if t.id == sid:
                return

        stages = []
        for s in data.get("stages", []):
            if isinstance(s, str):
                stages.append(StageNode(name=s, description=""))
            else:
                stages.append(StageNode(
                    name=s.get("name",""), description=s.get("description",""),
                    min_chapters=s.get("min_chapters",10),
                    max_chapters=s.get("max_chapters",20),
                    key_events=s.get("key_events",[]),
                ))
        template = StructureTemplate(
            id=sid, name=data.get("name",""),
            genre="", sub_genre="",
            total_chapters=data.get("total_chapters",500),
            stages=stages,
        )
        self.struct_lib.templates.append(template)

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
            fit_scenes=data.get("scene_fit",[]),
            examples=data.get("examples",[]),
        )
        self.gag_lib.patterns.append(pattern)

    def _add_theme(self, data: dict, source: str):
        from libraries.theme import ThemeEntry
        tid = f"scout_{source}_{data.get('name','unknown')}"
        for t in self.theme_lib.entries:
            if t.id == tid:
                return

        entry = ThemeEntry(
            id=tid, name=data.get("name",""),
            description=data.get("description",""),
            techniques=data.get("writing_tips",data.get("techniques",[])),
        )
        self.theme_lib.entries.append(entry)


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
                 gag_lib=None, theme_lib=None):
        self.crawler = FanqieCrawler()
        self.analyzer = NovelAnalyzer(llm_client)
        self.plot_lib = plot_lib
        self.struct_lib = struct_lib
        self.gag_lib = gag_lib
        self.theme_lib = theme_lib
        self.ingestor = LibraryIngestor(plot_lib, struct_lib, gag_lib, theme_lib)

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
            result.new_themes.extend(analysis.get("themes", []))

            # 入库
            stats = self.ingestor.ingest(analysis, "fanqie")
            logger.info(f"  Ingested: {stats}")

            # 礼貌延迟
            time.sleep(delay)

        result.downloaded_chapters = total_downloaded
        logger.info(f"Scout complete: {len(result.new_plots)} plots, "
                     f"{len(result.new_structures)} structures, "
                     f"{len(result.new_gags)} gags, "
                     f"{len(result.new_themes)} themes")

        return result

    def scout_single_book(self, title: str, chapters: int = 50,
                          on_progress=None) -> ScoutResult:
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
                imported_re = __import__('re')
                downloaded.append({
                    "index": ch["index"], "title": ch["title"],
                    "content": content,
                    "word_count": len(imported_re.findall(r'[\u4e00-\u9fff]', content)),
                })
            if on_progress:
                on_progress("download", i+1, total_ch, ch["title"][:30])
            time.sleep(1.0)

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
        result.new_themes = analysis.get("themes", [])

        if on_progress:
            on_progress("analysis_done", 4, 4, "分析完成，等待入库")

        return result

    def ingest_selected(self, plots: list = None, structures: list = None,
                        gags: list = None, themes: list = None,
                        source: str = "fanqie", on_progress=None) -> dict:
        """选择性入库"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        stats = {"plots": 0, "structures": 0, "gags": 0, "themes": 0}

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
            for item in structures:
                item["source"] = source
                item["created_at"] = now
                self.ingestor._add_structure(item, source)
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

        if themes and self.theme_lib:
            for item in themes:
                item["source"] = source
                item["created_at"] = now
                self.ingestor._add_theme(item, source)
                stats["themes"] += 1
            if on_progress:
                on_progress("ingest", 1, 1, f"内涵已入库 {stats['themes']}个")
            self.theme_lib._save()

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
    from libraries.theme import ThemeLibrary

    scout = FanqieScoutAgent(llm, PlotLibrary(), StructureLibrary(),
                              GagLibrary(), ThemeLibrary())
    result = scout.run(genre=genre, book_count=book_count,
                       chapters_per_book=chapters)

    print(f"\n=== Scout Complete ===")
    print(f"Books analyzed: {len(result.source_books)}")
    print(f"Chapters downloaded: {result.downloaded_chapters}")
    print(f"New plots: {len(result.new_plots)}")
    print(f"New structures: {len(result.new_structures)}")
    print(f"New gags: {len(result.new_gags)}")
    print(f"New themes: {len(result.new_themes)}")
