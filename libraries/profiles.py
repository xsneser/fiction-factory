"""
笔名风格档案（Pen Name Profile）
每个笔名 = 一个独立的 AI 作家，有自己的风格指纹
"""
from dataclasses import dataclass, field
from pathlib import Path
import json


@dataclass
class PenNameProfile:
    """笔名风格档案"""
    id: str
    pen_name: str                    # 笔名
    # 风格指纹
    style_fingerprint: dict = field(default_factory=dict)
    """
    {
        "sentence_length": "short",          # short/medium/long
        "dialogue_ratio": 0.4,              # 对话占比
        "paragraph_style": "chatty",         # chatty/compact/literary/flashy
        "humor_style": "吐槽型",             # 吐槽型/冷幽默/无厘头/无
        "action_style": "简洁利落",          # 画面感强/简洁利落/华丽铺陈
        "description_density": "low",       # low/medium/high
        "pov_preference": "third_person",   # first_person/third_person
    }
    """
    # 用词指纹
    word_print: dict = field(default_factory=dict)
    """
    {
        "common_words": ["卧槽", "淦", "牛逼"],
        "avoid_words": ["仿佛", "似乎", "不禁", "只见", "但见"],
        "sentence_starters": ["说实话", "要说", "那感觉", "...", "啧"],
        "paragraph_enders": ["——", "...", "。", "!"],
        "dialogue_tags": ["说", "道", "问", "回", "笑了", "冷声"],
        "action_beats": ["眯眼", "挑眉", "咂嘴", "不动声色"],
    }
    """
    # 常用创作套路
    tropes: dict = field(default_factory=dict)
    """
    {
        "preferred_plots": ["plot_dating_001", "plot_dating_008"],
        "preferred_gags": ["gag_001", "gag_007"],
        "avoid_plots": ["plot_dating_006"],
        "character_archetypes": ["面冷心热", "腹黑", "忠犬"],
        "scene_pacing": "快节奏（每章必有爽点）",
        "chapter_hook_style": "断在最精彩处",
    }
    """
    # 书目
    assigned_books: list[str] = field(default_factory=list)
    # 元信息
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "pen_name": self.pen_name,
            "style_fingerprint": self.style_fingerprint,
            "word_print": self.word_print, "tropes": self.tropes,
            "assigned_books": self.assigned_books,
            "description": self.description,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PenNameProfile":
        return PenNameProfile(
            id=d.get("id", ""), pen_name=d.get("pen_name", ""),
            style_fingerprint=d.get("style_fingerprint", {}),
            word_print=d.get("word_print", {}),
            tropes=d.get("tropes", {}),
            assigned_books=d.get("assigned_books", []),
            description=d.get("description", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

    def build_style_prompt(self) -> str:
        """生成注入写作 prompt 的风格约束文本"""
        parts = ["【本笔名的风格约束——必须严格遵守】"]
        fp = self.style_fingerprint
        wp = self.word_print
        tr = self.tropes

        if fp.get("sentence_length"):
            sl_map = {"short": "多用短句，每句8-15字", "medium": "句中偏长，15-25字为主",
                      "long": "可用长句铺陈，25字以上"}
            parts.append(f"- 句子长度：{sl_map.get(fp['sentence_length'], fp['sentence_length'])}")
        if fp.get("dialogue_ratio"):
            parts.append(f"- 对话占比：约{int(fp['dialogue_ratio']*100)}%")
        if fp.get("paragraph_style"):
            parts.append(f"- 段落节奏：{fp['paragraph_style']}")
        if fp.get("humor_style"):
            parts.append(f"- 幽默风格：{fp['humor_style']}")
        if fp.get("action_style"):
            parts.append(f"- 动作描写：{fp['action_style']}")

        if wp.get("common_words"):
            parts.append(f"- 常用词汇：{', '.join(wp['common_words'])}")
        if wp.get("avoid_words"):
            parts.append(f"- 绝对禁用词：{', '.join(wp['avoid_words'])}")
        if wp.get("dialogue_tags"):
            parts.append(f"- 对话标签偏好：{', '.join(wp['dialogue_tags'])}")
        if wp.get("action_beats"):
            parts.append(f"- 动作节拍偏好：{', '.join(wp['action_beats'])}")

        if tr.get("chapter_hook_style"):
            parts.append(f"- 章末钩子风格：{tr['chapter_hook_style']}")
        if tr.get("scene_pacing"):
            parts.append(f"- 场景节奏：{tr['scene_pacing']}")

        return "\n".join(parts) + "\n"


class ProfileManager:
    """笔名档案管理器"""

    def __init__(self, profiles_dir: str = "profiles"):
        self.dir = Path(profiles_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, PenNameProfile] = {}
        self._load_all()

    def _load_all(self):
        for f in self.dir.glob("*.json"):
            profile = PenNameProfile.from_dict(json.loads(f.read_text(encoding="utf-8")))
            self._cache[profile.id] = profile

    def list_all(self) -> list[PenNameProfile]:
        return list(self._cache.values())

    def get(self, profile_id: str) -> PenNameProfile | None:
        return self._cache.get(profile_id)

    def get_by_name(self, pen_name: str) -> PenNameProfile | None:
        for p in self._cache.values():
            if p.pen_name == pen_name:
                return p
        return None

    def create(self, pen_name: str, description: str = "",
               style_fingerprint: dict = None, word_print: dict = None,
               tropes: dict = None) -> PenNameProfile:
        from datetime import datetime
        profile_id = f"profile_{len(self._cache) + 1:03d}"
        profile = PenNameProfile(
            id=profile_id, pen_name=pen_name,
            description=description,
            style_fingerprint=style_fingerprint or {},
            word_print=word_print or {},
            tropes=tropes or {},
            created_at=datetime.now().isoformat(),
        )
        self._cache[profile_id] = profile
        self._save(profile)
        return profile

    def update(self, profile: PenNameProfile):
        from datetime import datetime
        profile.updated_at = datetime.now().isoformat()
        self._cache[profile.id] = profile
        self._save(profile)

    def _save(self, profile: PenNameProfile):
        path = self.dir / f"{profile.id}.json"
        path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def delete(self, profile_id: str) -> bool:
        path = self.dir / f"{profile_id}.json"
        if path.exists():
            path.unlink()
            self._cache.pop(profile_id, None)
            return True
        return False


# ─── 预设笔名模板 ───

PRESET_PROFILES = [
    {
        "pen_name": "枫落",
        "description": "都市爽文专业户，快节奏打脸流",
        "style_fingerprint": {
            "sentence_length": "short",
            "dialogue_ratio": 0.35,
            "paragraph_style": "chatty",
            "humor_style": "吐槽型",
            "action_style": "简洁利落",
            "description_density": "low",
        },
        "word_print": {
            "common_words": ["卧槽", "淦", "牛逼", "真他娘的"],
            "avoid_words": ["仿佛", "似乎", "不禁", "不由得"],
            "sentence_starters": ["说实话", "要说", "啧"],
            "dialogue_tags": ["说", "道", "笑了", "冷声", "回了句"],
            "action_beats": ["眯眼", "挑眉", "咂嘴", "不动声色地"],
        },
        "tropes": {
            "preferred_plots": ["plot_dating_001", "plot_dating_005", "plot_dating_008"],
            "preferred_gags": ["gag_001", "gag_003", "gag_007"],
            "avoid_plots": ["plot_dating_006"],
            "chapter_hook_style": "断在最精彩处，每章留钩子",
            "scene_pacing": "快节奏（每章必有爽点）",
        },
    },
    {
        "pen_name": "夜雨",
        "description": "玄幻修仙流，正剧向，偏厚重",
        "style_fingerprint": {
            "sentence_length": "medium",
            "dialogue_ratio": 0.25,
            "paragraph_style": "literary",
            "humor_style": "冷幽默",
            "action_style": "画面感强",
            "description_density": "medium",
        },
        "word_print": {
            "common_words": ["天地", "道", "意境", "流转"],
            "avoid_words": ["卧槽", "淦", "牛逼"],
            "dialogue_tags": ["道", "冷声", "沉吟", "淡淡道"],
            "action_beats": ["抬手", "目光微凝", "沉默片刻"],
        },
        "tropes": {
            "preferred_plots": ["plot_dating_003", "plot_dating_004", "plot_dating_007", "plot_dating_010"],
            "preferred_gags": ["gag_003", "gag_009"],
            "chapter_hook_style": "以意境或悬念收尾",
            "scene_pacing": "稳扎稳打，有不疾不徐的节奏感",
        },
    },
    {
        "pen_name": "青衫",
        "description": "言情甜文写手，擅长日常互动与情感描写",
        "style_fingerprint": {
            "sentence_length": "medium",
            "dialogue_ratio": 0.45,
            "paragraph_style": "chatty",
            "humor_style": "无厘头",
            "action_style": "简洁利落",
            "description_density": "low",
        },
        "word_print": {
            "common_words": ["心里", "突然", "忍不住", "悄悄"],
            "avoid_words": ["仿佛", "似乎"],
            "dialogue_tags": ["说", "笑", "问", "轻声道", "小声说"],
            "action_beats": ["耳根微红", "别过脸去", "唇角微扬"],
        },
        "tropes": {
            "preferred_plots": ["plot_dating_006", "plot_dating_009"],
            "preferred_gags": ["gag_007", "gag_008"],
            "chapter_hook_style": "以情感转折或甜蜜互动收尾",
            "scene_pacing": "轻松舒畅，对话自然",
        },
    },
]
