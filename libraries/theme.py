"""
内涵/意义库（Theme Library）
故事背后要传达的核心主题与讨论母题
"""
from dataclasses import dataclass, field
from .base_library import JsonLibrary


@dataclass
class ThemeEntry:
    """一个母题/主题单元"""
    id: str
    name: str
    description: str
    techniques: list[str]
    """
    可选手法：
    - "角色行动展示"
    - "对话间接暗示"
    - "环境/象征物呼应"
    - "对比/反差"
    - "群像反应"
    """
    compatible_plots: list[str] = field(default_factory=list)
    compatible_gags: list[str] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    source: str = ""                 # 来源
    created_at: str = "2026-05-01"   # 收录时间
    enabled: bool = True              # 启用状态

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "description": self.description,
            "techniques": self.techniques,
            "compatible_plots": self.compatible_plots,
            "compatible_gags": self.compatible_gags,
            "tips": self.tips, "examples": self.examples,
            "source": self.source, "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThemeEntry":
        return ThemeEntry(**{k: v for k, v in d.items()
                             if k in ThemeEntry.__dataclass_fields__})


class ThemeLibrary(JsonLibrary):
    """内涵库管理器（进程内单例，避免每实例重复读 JSON）"""
    _instance = None
    _list_attr = "entries"
    _key = "entries"
    _file_name = "themes.json"

    @classmethod
    def _from_dict(cls, d: dict) -> "ThemeEntry":
        return ThemeEntry.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_THEMES

    def get_by_id(self, theme_id: str):
        for e in self.entries:
            if e.id == theme_id:
                return e
        return None

    def search(self, name: str = "") -> list[ThemeEntry]:
        if not name:
            return self.entries
        return [e for e in self.entries if name in e.name or name in e.description]


# ─── 内置母题模板 ───

BUILTIN_THEMES = [
    ThemeEntry(
        id="theme_001", name="公平（Justice）",
        description="世界是否公平？主角如何在不公平中获取公平？",
        techniques=["角色行动展示", "对比/反差", "群像反应"],
        compatible_plots=["plot_dating_001", "plot_dating_005", "plot_dating_010"],
        tips=[
            "用结果而不是语言来表现公平与否",
            "不同阶层的人对公平的定义不同，这是冲突的有力来源",
            "主角赢得公平的过程应该比结果更动人",
        ],
        examples=["主角被冤枉赶出宗门 -> 多年后以实力洗刷冤屈 -> 但选择了放过而非杀戮"],
    ),
    ThemeEntry(
        id="theme_002", name="成长的代价（Cost of Growth）",
        description="每一次成长都伴随着失去。主角为了变强失去了什么？",
        techniques=["角色行动展示", "对话间接暗示", "对比/反差"],
        compatible_plots=["plot_dating_004", "plot_dating_010", "plot_dating_006"],
        compatible_gags=["gag_010"],
        tips=[
            "成长的代价不一定非得是亲近的人去世（避免滥用死亡）",
            "可以是性格的偏移、初心的遗失、无法挽回的错过",
            "用对比展示主角成长前后的变化",
        ],
        examples=["为了突破境界，主角舍弃了某种情感，变得沉默寡言 -> 后来某个场景让他重新找回"],
    ),
    ThemeEntry(
        id="theme_003", name="身份与伪装（Identity & Disguise）",
        description="主角以多个身份/伪装生存，探讨真实与虚假的边界",
        techniques=["角色行动展示", "对话间接暗示", "群像反应"],
        compatible_plots=["plot_dating_008", "plot_dating_001"],
        compatible_gags=["gag_004"],
        tips=[
            "用不同身份面临的道德困境来深挖",
            "伪装一旦深入，角色本人也会模糊真假",
        ],
    ),
    ThemeEntry(
        id="theme_004", name="牺牲（Sacrifice）",
        description="为更大的目标牺牲小我，探讨牺牲的边界",
        techniques=["角色行动展示", "对话间接暗示", "环境/象征物呼应"],
        compatible_plots=["plot_dating_010", "plot_dating_006"],
        tips=[
            "更打动人的牺牲是'放弃可能的幸福'而不是放弃生命",
            "牺牲应该有一种沉甸甸的重量，而非轻描淡写",
            "不同角色面对牺牲的不同选择形成群像",
        ],
    ),
    ThemeEntry(
        id="theme_005", name="归属感（Belonging）",
        description="主角寻找和建立归属感的旅程",
        techniques=["角色行动展示", "群像反应"],
        compatible_plots=["plot_dating_003", "plot_dating_010"],
        compatible_gags=["gag_005"],
        tips=[
            "一个团队/家庭的温暖可以成为最强大的力量来源",
            "用主角从冷眼旁观到主动守护的转变来展示归属感",
        ],
    ),
    ThemeEntry(
        id="theme_006", name="传承与突破（Legacy & Breakthrough）",
        description="继承前人意志与打破旧有规则之间的张力",
        techniques=["角色行动展示", "对比/反差", "环境/象征物呼应"],
        compatible_plots=["plot_dating_003", "plot_dating_004"],
        tips=[
            "主角既要接受前人积累，又要超越前人",
            "旧势力往往是「传承」的代表，新势力是「突破」的代表",
        ],
    ),
]
