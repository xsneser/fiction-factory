"""多平台热榜注册表 — 统一契约（list_rankings / discover）。

平台适配器实现统一入口，Web 端点与 MCP 工具都经 `discover` / `list_rankings` 分发。
条目为统一字典（HotRankItem）：
  {platform, rank, book_id, title, author, category, word_count, chapter_count,
   hot_score, intro, url}（可选 raw 存平台原始字段）。

平台实现现状：
- fanqie：已实现（plugins/fanqie_scout.FanqieCrawler）——榜单页 /rank/{gender}_{rankMold}_{catId}
  SSR __INITIAL_STATE__.rank.book_list 解析 + PUA 字体解码。
- qidian（起点）：骨架待接。参考 d3nnywong/qidian-mcp-server（14 榜 × 品类，Playwright）；
  或 staysharp1104/WebCrawler（requests + m.qidian.com 移动站 SSR）。注意月票/推荐数字
  是 woff 字体混淆，需解析 cmap 映射还原，否则保留排名/标题/作者、hot_score 置 0。
- jjwxc（晋江）：骨架待接。参考 dev-chenxing/jjwxc-charts / story-long-scan 规范：
  榜单页 https://www.jjwxc.net/topten.php?orderstr={榜ID}&t={频道ID}（t=0 全站），
  页面是 **gb18030** 编码需特殊解码；列表页取 novelid 后进 onebook.php?novelid= 详情
  补收藏/营养液/积分/字数（itemprop 微数据，无需登录）。榜 ID：收入金榜12/月榜7/季度榜8/
  完结金榜14/新手金榜15/千字金榜17。

字段契约与接入方式见 docs/设计文档-多平台热榜接口.md。
"""
from plugins.fanqie_scout import FanqieCrawler


class BaseHotRanker:
    """热榜适配器契约。子类实现 PLATFORM / list_rankings / discover。"""
    PLATFORM = ""

    def list_rankings(self, **kw) -> list[dict]:
        """榜单/分类清单 → [{id, name}]（供 UI 下拉 / 未来动态 chips）。"""
        raise NotImplementedError

    def discover(self, key: str = "", count: int = 10, **kw) -> list[dict]:
        """拉榜单书 → [HotRankItem dict]（key 空=平台默认/聚合）。"""
        raise NotImplementedError


class FanqieRanker(BaseHotRanker):
    """番茄热榜（榜单页 SSR + 字体解码）。key：分类 id 或题材中文名；空/'全部' → 聚合。"""

    PLATFORM = "fanqie"

    def __init__(self):
        self._crawler = None

    def _c(self) -> FanqieCrawler:
        if self._crawler is None:
            self._crawler = FanqieCrawler()
        return self._crawler

    def list_rankings(self, gender: str = "male", **kw) -> list[dict]:
        return self._c().list_rankings(gender=gender)

    def discover(self, key: str = "", count: int = 10, gender: str = "male",
                 rank_mold: int = 2, **kw) -> list[dict]:
        # 番茄榜单 gender 数值：1=男频 2=女频（原传 0 是潜伏 bug，女频从未拉成功）
        novels = self._c().discover_hot(
            key=key, count=count,
            gender=(2 if gender == "female" else 1),
            rank_mold=rank_mold)
        return [n.__dict__ for n in novels]


REGISTRY = {
    "fanqie": FanqieRanker(),
    # 起点/晋江：接口契约已定，爬虫适配器待接（见模块头注记）
    "qidian": None,
    "jjwxc": None,
}


def get_ranker(platform: str = "fanqie") -> BaseHotRanker:
    """取平台适配器；未知平台返回 None（调用方自行兜底空列表）。"""
    return REGISTRY.get(platform or "fanqie")


def discover(platform: str = "fanqie", key: str = "", count: int = 10, **kw) -> list[dict]:
    """经注册表拉热榜（统一 dict 条目）；平台未实现/失败返回空列表。"""
    r = get_ranker(platform)
    if not r:
        return []
    try:
        return r.discover(key=key, count=count, **kw) or []
    except Exception:
        return []


def list_rankings(platform: str = "fanqie", **kw) -> list[dict]:
    """经注册表查榜单/分类清单；平台未实现返回空列表。"""
    r = get_ranker(platform)
    if not r:
        return []
    try:
        return r.list_rankings(**kw) or []
    except Exception:
        return []
