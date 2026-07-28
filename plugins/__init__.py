"""
外部采集插件系统
从小说平台、社交媒体获取桥段/笑点/梗素材
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ScrapedMaterial:
    """采集到的素材"""
    source: str              # 来源平台：fanqie/qidian/weibo/bilibili/...
    material_type: str       # 类型：plot/gag/meme/phrase/trend
    raw_content: str         # 原始文本
    cleaned_content: str     # 清洗后可用于库的内容
    url: str = ""            # 来源链接
    title: str = ""          # 来源标题/用户名
    engagement: int = 0      # 热度/互动量
    tags: list[str] = None   # 标签


class BasePlugin(ABC):
    """采集插件基类"""

    name: str = ""
    description: str = ""

    @abstractmethod
    def scrape(self, keyword: str = "", max_items: int = 20) -> list[ScrapedMaterial]:
        """执行采集"""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查插件是否可用"""
        ...

    def extract_plots(self, materials: list[ScrapedMaterial], llm_client=None) -> list[dict]:
        """从采集素材中提取桥段模式"""
        # 纯规则提取 → 后续可接 LLM 分析
        plots = []
        for m in materials:
            if m.material_type == "plot":
                plots.append({
                    "source": m.source,
                    "title": m.title,
                    "raw": m.cleaned_content,
                    "tags": m.tags or [],
                })
        return plots

    def extract_gags(self, materials: list[ScrapedMaterial], llm_client=None) -> list[dict]:
        """从采集素材中提取笑点模式"""
        gags = []
        for m in materials:
            if m.material_type == "gag":
                gags.append({
                    "source": m.source,
                    "title": m.title,
                    "raw": m.cleaned_content,
                    "tags": m.tags or [],
                })
        return gags

    def extract_memes(self, materials: list[ScrapedMaterial]) -> list[dict]:
        """从采集素材中提取最新的梗"""
        return [
            {"source": m.source, "phrase": m.cleaned_content,
             "engagement": m.engagement, "tags": m.tags or []}
            for m in materials if m.material_type in ("meme", "trend")
        ]


# ─── 骨架插件（待后续实现具体平台） ───

class FanqiePlugin(BasePlugin):
    """番茄小说采集插件"""
    name = "fanqie"
    description = "从番茄小说平台采集热门桥段模式和章节结构"

    def is_available(self) -> bool:
        try:
            import requests
            return True
        except ImportError:
            return False

    def scrape(self, keyword: str = "", max_items: int = 20) -> list[ScrapedMaterial]:
        # TODO: 实现番茄小说爬虫
        return []


class QidianPlugin(BasePlugin):
    """起点中文网采集插件"""
    name = "qidian"
    description = "从起点中文网采集热门桥段和章节结构"

    def is_available(self) -> bool:
        return False  # 需要反爬手段

    def scrape(self, keyword: str = "", max_items: int = 20) -> list[ScrapedMaterial]:
        return []


class WeiboPlugin(BasePlugin):
    """微博热搜/热梗采集"""
    name = "weibo"
    description = "从微博热搜采集最新网络热梗和流行语"

    def is_available(self) -> bool:
        try:
            import requests
            return True
        except ImportError:
            return False

    def scrape(self, keyword: str = "", max_items: int = 20) -> list[ScrapedMaterial]:
        # TODO: 实现微博热搜采集
        return []


class BilibiliPlugin(BasePlugin):
    """B站弹幕/热词采集"""
    name = "bilibili"
    description = "从B站采集弹幕热词和流行文化梗"

    def is_available(self) -> bool:
        try:
            import requests
            return True
        except ImportError:
            return False

    def scrape(self, keyword: str = "", max_items: int = 20) -> list[ScrapedMaterial]:
        return []


# ─── 插件注册表 ───

PLUGIN_REGISTRY: dict[str, type[BasePlugin]] = {
    "fanqie": FanqiePlugin,
    "qidian": QidianPlugin,
    "weibo": WeiboPlugin,
    "bilibili": BilibiliPlugin,
}


def get_plugin(name: str) -> BasePlugin | None:
    cls = PLUGIN_REGISTRY.get(name)
    return cls() if cls else None


def list_plugins() -> list[dict]:
    result = []
    for name, cls in PLUGIN_REGISTRY.items():
        p = cls()
        result.append({
            "name": name,
            "description": p.description,
            "available": p.is_available(),
        })
    return result
