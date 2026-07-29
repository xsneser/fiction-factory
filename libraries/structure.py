"""
大纲库（Structure Library）
各类网文流派的故事骨架结构模板
"""
from dataclasses import dataclass, field
from pathlib import Path
import json


_DEFAULT_DATA_DIR = Path(__file__).parent / "data"


@dataclass
class StageNode:
    """大纲阶段节点"""
    name: str            # 阶段名，如 "入门"
    description: str     # 描述
    min_chapters: int = 3
    max_chapters: int = 10
    key_events: list[str] = field(default_factory=list)
    foreshadow_opportunities: list[str] = field(default_factory=list)  # 埋坑机会


@dataclass
class StructureTemplate:
    """大纲结构模板"""
    id: str
    name: str
    genre: str                     # 流派：玄幻/都市/言情/悬疑/...
    sub_genre: str = ""            # 子流派：升级流/系统流/重生/...
    description: str = ""
    total_chapters: int = 500
    stages: list[StageNode] = field(default_factory=list)
    opening_patterns: list[str] = field(default_factory=list)
    climax_patterns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = ""                 # 来源
    created_at: str = "2026-05-01"   # 收录时间
    enabled: bool = True              # 启用状态

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "genre": self.genre, "sub_genre": self.sub_genre,
            "description": self.description,
            "total_chapters": self.total_chapters,
            "stages": [{"name": s.name, "description": s.description,
                        "min_chapters": s.min_chapters, "max_chapters": s.max_chapters,
                        "key_events": s.key_events,
                        "foreshadow_opportunities": s.foreshadow_opportunities}
                       for s in self.stages],
            "opening_patterns": self.opening_patterns,
            "climax_patterns": self.climax_patterns,
            "tags": self.tags,
            "source": self.source,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StructureTemplate":
        return StructureTemplate(
            id=d["id"], name=d.get("name", ""),
            genre=d.get("genre", ""), sub_genre=d.get("sub_genre", ""),
            description=d.get("description", ""),
            total_chapters=d.get("total_chapters", 500),
            stages=[StageNode(**s) for s in d.get("stages", [])],
            opening_patterns=d.get("opening_patterns", []),
            climax_patterns=d.get("climax_patterns", []),
            tags=d.get("tags", []),
            source=d.get("source", ""),
            created_at=d.get("created_at", "2026-05-01"),
            enabled=d.get("enabled", True),
        )


class StructureLibrary:
    """大纲库管理器"""

    def __init__(self):
        self.templates: list[StructureTemplate] = []
        self._save_path = _DEFAULT_DATA_DIR / "structures.json"
        self._load()

    def _load(self):
        if self._save_path.exists():
            with open(self._save_path, encoding="utf-8") as f:
                data = json.load(f)
            self.templates = [StructureTemplate.from_dict(d)
                              for d in data.get("templates", [])]
        else:
            self.templates = BUILTIN_STRUCTURES

    def _save(self):
        self._save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._save_path, "w", encoding="utf-8") as f:
            json.dump({"templates": [t.to_dict() for t in self.templates]},
                      f, ensure_ascii=False, indent=2)

    def load(self, path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.templates = [StructureTemplate.from_dict(d)
                          for d in data.get("templates", [])]

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"templates": [t.to_dict() for t in self.templates]},
                      f, ensure_ascii=False, indent=2)

    def search(self, genre: str = "", sub_genre: str = "",
               chapter_count: int = 0) -> list[StructureTemplate]:
        results = self.templates
        if genre:
            results = [t for t in results if genre in t.genre]
        if sub_genre:
            results = [t for t in results if sub_genre in t.sub_genre]
        if chapter_count:
            # 找总章节数最接近的模板
            results.sort(key=lambda t: abs(t.total_chapters - chapter_count))
        return results

    def get_by_id(self, template_id: str):
        for t in self.templates:
            if t.id == template_id:
                return t
        return None


# ─── 内置大纲结构模板 ───

BUILTIN_STRUCTURES = [
    StructureTemplate(
        id="struct_xuanhuan_01", name="玄幻升级流（标准版）",
        genre="玄幻", sub_genre="升级流",
        description="最经典的玄幻修仙升级结构，从凡人到天帝的旅程",
        total_chapters=500,
        stages=[
            StageNode("觉醒/重生", "开局：身份交代、金手指激活、第一个小目标",
                      3, 8,
                      ["来到新世界/重生", "金手指激活", "面临第一个危机"],
                      ["金手指的代价/限制", "主角背景的疑点"]),
            StageNode("入门试炼", "初入修行/加入势力，快速成长",
                      10, 25,
                      ["获得修炼法门", "第一次战斗胜利", "被发现天赋"],
                      ["势力内的暗流", "隐藏的大阴谋线索"]),
            StageNode("扬名立万", "在宗门/势力中脱颖而出的阶段",
                      25, 50,
                      ["第一次越级挑战", "正式比赛夺冠", "获得重要功法/宝物"],
                      ["更高层次的敌意", "师父/靠山的秘密"]),
            StageNode("宗门风云", "势力内部矛盾激化，卷入更大纷争",
                      30, 60,
                      ["宗门内斗", "挑战更强者", "获得核心传承"],
                      ["世界观扩展", "更高层次势力的存在"]),
            StageNode("外出历练", "离开舒适区，走向更大的世界",
                      40, 80,
                      ["进入新地图", "结识新伙伴", "遭遇更强敌人"],
                      ["新地图的隐藏历史", "远古秘密的一角"]),
            StageNode("逐鹿天下", "在更大舞台上竞争，建立自己的势力",
                      50, 100,
                      ["建立自己的势力", "参与大势力博弈", "首次面对终极boss的棋子"],
                      ["终极boss的布局", "世界层面的危机"]),
            StageNode("问鼎巅峰", "登顶之路，与终极boss决战",
                      50, 100,
                      ["突破最后境界", "集齐最后底牌", "最终决战"],
                      ["世界的真相", "上一纪元的遗留"]),
            StageNode("大结局", "世界新生/新篇章",
                      5, 15,
                      ["最终胜利/牺牲", "新世界秩序", "角色归宿"],
                      []),
        ],
        opening_patterns=["plot_dating_011", "plot_dating_012"],  # 穿越开局、系统激活
        climax_patterns=["plot_dating_005", "plot_dating_007", "plot_dating_010"],
        tags=["玄幻", "修仙", "升级", "爽文"],
    ),
    StructureTemplate(
        id="struct_dushi_01", name="都市爽文（逆袭流）",
        genre="都市", sub_genre="逆袭流",
        description="落魄主角获得外挂后逆袭人生的爽文结构",
        total_chapters=300,
        stages=[
            StageNode("落魄开局", "展示最糟糕的状态，为后续逆袭铺底",
                      3, 5,
                      ["被退婚/被赶出/被看不起", "走到人生最低谷", "获得金手指"],
                      ["金手指的真正来历", "隐藏的敌人/幕后黑手"]),
            StageNode("小试牛刀", "在局部范围内证明自己",
                      10, 20,
                      ["第一次用金手指获利", "打脸第一个反派", "获得初步尊重"],
                      ["金手指的升级条件", "暗中关注主角的势力"]),
            StageNode("站稳脚跟", "成为本地小有名气的人物",
                      20, 40,
                      ["建立自己的产业/势力", "收拢小弟/伙伴", "正面硬刚敌方家族"],
                      ["更大的家族/势力登场", "主角背后牵连的大局"]),
            StageNode("双雄对决", "与主要的地区级对手周旋对抗",
                      30, 60,
                      ["正面交锋", "商业/武力的较量", "女主/身边人卷入"],
                      ["女主角的真实身份", "对手背后的靠山"]),
            StageNode("冲出地界", "跳出地区限制，进入全国/全球舞台",
                      40, 70,
                      ["上级势力的介入", "身份/地位的跃迁", "偶然卷入国际/全国事件"],
                      ["国家/世界层面的暗流", "金手指的终极形态"]),
            StageNode("王座之路", "成为顶级人物",
                      40, 70,
                      ["建立自己的王朝/势力", "扳倒最大的对手", "用行动改变现状"],
                      ["金手指/外挂的终极代价"]),
            StageNode("收尾", "功成身退或开启新篇",
                      10, 20,
                      ["与各个女主的关系收束", "对手的最终下场", "主角的生活方式选择"],
                      []),
        ],
        opening_patterns=["plot_dating_011", "plot_dating_001"],  # 重生 + 退婚打脸
        climax_patterns=["plot_dating_005", "plot_dating_010"],
        tags=["都市", "逆袭", "爽文", "现代"],
    ),
    StructureTemplate(
        id="struct_xuanyi_01", name="悬疑推理（单元剧+主线）",
        genre="悬疑", sub_genre="推理",
        description="单元式案件+隐藏主线，适用于修仙/都市/灵异侦探背景",
        total_chapters=200,
        stages=[
            StageNode("入局", "主角作为'新人'或'局外人'被卷入第一个事件",
                      3, 8,
                      ["特殊能力启用/身份转变", "第一个案件", "发现异常"],
                      ["隐藏的大boss", "事件的异常模式"]),
            StageNode("单元案件×N", "2-5个独立案件，渐进式揭示更大阴谋",
                      30, 60,
                      ["案件逐一解决", "收集线索", "伙伴/关系网拓展"],
                      ["每个案件都留下一点指向主线的伏笔"]),
            StageNode("第一次大交锋", "首次正面接触主线的核心威胁",
                      20, 30,
                      ["关键线索出现", "揭开一半真相", "敌人显露真容"],
                      ["真相的另一面", "更大的阴谋"]),
            StageNode("逆转/空降", "已有的认知被推翻，露出更深的层次",
                      20, 30,
                      ["核心反转", "信任关系打破", "新的危机出现"],
                      ["反转再反转的机会点"]),
            StageNode("终局", "收集线索→推导→正面冲突",
                      30, 50,
                      ["最终推导", "与幕后黑手对决", "全员登场"],
                      []),
        ],
        opening_patterns=["plot_dating_011"],  # 穿越/转生开局
        climax_patterns=["plot_dating_004"],  # 秘境探险(可用于终局大揭秘)
        tags=["悬疑", "推理", "反转", "单元剧"],
    ),
    StructureTemplate(
        id="struct_tianwen_01", name="言情甜文（日常向）",
        genre="言情", sub_genre="甜宠日常",
        description="以日常互动和情感发展为主的轻松甜文",
        total_chapters=100,
        stages=[
            StageNode("初遇", "两人相遇的情景，建立第一印象",
                      1, 3,
                      ["意外/被迫的相遇", "一方先动心/双方都嘴硬"],
                      ["未说出口的秘密"]),
            StageNode("接触期", "频繁互动，感情从浅入深",
                      10, 20,
                      ["日常互动", "小事件中的体贴", "第一个暧昧/脸红场景"],
                      ["对方的过去/痛点"]),
            StageNode("波折期", "小误会/小分离/外部压力",
                      10, 15,
                      ["误会产生（要合理）", "一方受伤/遇险", "有人追对方"],
                      ["第三方的真正目的"]),
            StageNode("确认关系", "正式表白/关系升级",
                      3, 5,
                      ["表白场景", "高甜互动", "第一次秀恩爱/公开展示关系"],
                      ["与双方亲友关系的伏笔"]),
            StageNode("生活日常", "在一起之后的甜甜日常（占全书大部分）",
                      30, 50,
                      ["一起旅行", "一起面对生活琐事", "一起工作/共同目标"],
                      ["另一个隐藏的感情线"]),
            StageNode("收尾", "走向更长远的未来",
                      5, 10,
                      ["婚/终成眷属", "生活新阶段"],
                      []),
        ],
        opening_patterns=["plot_dating_011"],  # 穿越/重生开局
        climax_patterns=["plot_dating_006", "plot_dating_009"],  # 英雄救美、修罗场
        tags=["言情", "甜文", "日常", "短篇"],
    ),
    StructureTemplate(
        id="struct_chuanyue_01", name="穿越/重生爽文（快节奏）",
        genre="穿越", sub_genre="重生逆袭",
        description="快节奏的穿越/重生爽文，用于番茄/短篇平台",
        total_chapters=150,
        stages=[
            StageNode("开局", "穿越/重生、确认身份、利用先发优势",
                      1, 3,
                      ["高能开局（穿越/重生）", "了解身份处境", "第一次利用先发优势"],
                      ["是什么导致了主角的穿越/重生", "隐藏的更大危机"]),
            StageNode("布局期", "利用先知先觉布局，全方位碾压",
                      5, 15,
                      ["建立关系网", "获取未来关键资源", "避开前世雷区"],
                      ["蝴蝶效应引发的新事件"]),
            StageNode("碾压期", "前世敌人一个个被碾压",
                      15, 30,
                      ["碾压第一个前世仇人", "身份/地位突变", "被各方关注"],
                      ["前世死因的真相"]),
            StageNode("跨界", "跳出原有层级，进入更大的竞争",
                      30, 50,
                      ["发现前世的身世真相", "进入更高层面的博弈", "应对新老敌人的合围"],
                      ["穿越/重生的真正原因"]),
            StageNode("最终清算", "与幕后黑手正面决斗",
                      30, 40,
                      ["最终布局", "决斗高潮", "新的开始或总结"],
                      []),
        ],
        opening_patterns=["plot_dating_011", "plot_dating_012"],  # 穿越开局、金手指
        climax_patterns=["plot_dating_005", "plot_dating_001"],
        tags=["穿越", "重生", "快节奏", "爽文", "短篇"],
    ),
]
