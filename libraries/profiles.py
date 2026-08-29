"""
笔名风格档案（Pen Name Profile）
每个笔名 = 一个独立的 AI 作家，有自己的风格指纹
"""
from dataclasses import dataclass, field
from pathlib import Path
import json

# 平台中文标签（UI 表单 / publisher 软提醒 / skill 展示共用）
PLATFORM_LABELS = {
    "fanqie": "番茄小说",
    "qidian": "起点中文网",
    "jinjiang": "晋江文学城",
    "web": "网页/其他",
}
# 已知平台顺序（编辑表单按此渲染）
KNOWN_PLATFORMS = ["fanqie", "qidian", "jinjiang", "web"]


@dataclass
class PenNameProfile:
    """笔名风格档案"""
    id: str
    pen_name: str                    # 笔名
    language: str = "zh"             # zh 中文笔名 / en 英文笔名（不同语言笔名分开）
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
    # 写法资产（从文本提取的风格特征，可保存/组合/绑定笔名）
    style_assets: dict = field(default_factory=dict)
    """
    {
        "sentence_length": "short",          # short/medium/long
        "dialogue_ratio": 0.4,              # 对话占比
        "paragraph_style": "chatty",         # chatty/compact/literary
        "common_words": ["卧槽", "淦"],
        "avoid_words": ["仿佛", "似乎"],
        "sentence_starters": ["说实话", "啧"],
        "action_beats": ["眯眼", "挑眉"],
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
    # 平台账号注册信息（仅 UI 人工登记；agent 只读）—— 运营元数据，不进风格 prompt
    platform_accounts: dict = field(default_factory=dict)
    """
    {
        "fanqie": {"registered": True, "site_id": "作者号/站点ID", "author_url": "作者主页URL",
                    "notes": "备注", "last_published_at": "最近发布时间"},
        ...
    }
    """
    # 元信息
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def is_registered_on(self, platform: str) -> bool:
        """该笔名是否已在某平台登记注册账号。"""
        return bool((self.platform_accounts or {}).get(platform or "", {}).get("registered"))

    def registered_platforms(self) -> list:
        """已登记注册账号的平台列表。"""
        return [k for k, v in (self.platform_accounts or {}).items()
                if isinstance(v, dict) and v.get("registered")]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "pen_name": self.pen_name, "language": self.language,
            "style_fingerprint": self.style_fingerprint,
            "word_print": self.word_print, "tropes": self.tropes,
            "style_assets": self.style_assets,
            "assigned_books": self.assigned_books,
            "platform_accounts": self.platform_accounts,
            "description": self.description,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PenNameProfile":
        return PenNameProfile(
            id=d.get("id", ""), pen_name=d.get("pen_name", ""),
            language=d.get("language", "zh"),
            style_fingerprint=d.get("style_fingerprint", {}),
            word_print=d.get("word_print", {}),
            tropes=d.get("tropes", {}),
            style_assets=d.get("style_assets", {}),
            assigned_books=d.get("assigned_books", []),
            platform_accounts=d.get("platform_accounts", {}),
            description=d.get("description", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

    def build_style_prompt(self) -> str:
        """生成注入写作 prompt 的风格约束文本。

        句式风格 = prefer 规则 + 禁止内容 = ban 规则（经 build_rules_block 注入）。
        表单只读身份/调性字段（幽默/动作/描写密度）+ 套路（章末钩子/场景节奏）。
        style_assets / word_print 已废弃不再编译（迁移期已转成规则）。"""
        from .style_rules import StyleRuleLibrary
        parts = ["【本笔名的风格约束——必须严格遵守】"]
        fp = self.style_fingerprint
        tr = self.tropes
        if fp.get("humor_style"):
            parts.append(f"- 幽默风格：{fp['humor_style']}")
        if fp.get("action_style"):
            parts.append(f"- 动作描写：{fp['action_style']}")
        if fp.get("description_density"):
            parts.append(f"- 描写密度：{fp['description_density']}")
        if tr.get("chapter_hook_style"):
            parts.append(f"- 章末钩子风格：{tr['chapter_hook_style']}")
        if tr.get("scene_pacing"):
            parts.append(f"- 场景节奏：{tr['scene_pacing']}")
        rb = StyleRuleLibrary().build_rules_block(self.id)
        if rb:
            parts.append(rb)
        parts.append(self.build_language_hints())
        return "\n".join(parts) + "\n"

    def build_language_hints(self) -> str:
        """按笔名语言给出写作习惯提示（中文/英文笔名分开；注入到风格约束尾部）。"""
        lang = (self.language or "zh").lower()
        if lang == "en":
            return ("- 英文笔名风格：句式长短交错，时态/主谓一致，缩写与口语自然；"
                    "避免过度从句嵌套，对话标签多用常见词（said/asked 等）。")
        return ("- 中文写作习惯：标点用全角，善用四字词/成语/惯用语，句末语气词适度；"
                "避免欧化长句与翻译腔，偶尔留半截话/省略，行文口语化。")

    def build_writing_prompt(self) -> str:
        """生成前强注入全文本（get_writing_context 使用）：
        本笔名风格（含句式风格/禁止内容规则）+ 语言习惯 + 通用写作纪律。
        目标 <1.5KB 防 dsh 工具结果裁剪（style_rules 位于 payload 尾部可幸存）。"""
        parts = ["【本笔名的写作风格约束——动笔前必读，必须严格遵守】"]
        body = self.build_style_prompt()
        lines = [l for l in body.splitlines() if l.strip()]
        if lines and lines[0].startswith("【"):
            lines = lines[1:]           # 剥重复标题行
        if lines:
            parts.append("\n".join(lines))
        parts.append(
            "【通用写作纪律】"
            "\n- 对话用日常语气，不要文绉绉"
            "\n- 每段 2-3 句，不要大段堆砌描写"
            "\n- 内心独白可口语化（如：靠、淦、这TM...）"
            "\n- 偶尔留半截话，不要所有句子主谓宾完整"
            "\n- 动作描写不要每句都带修饰副词"
        )
        return "\n".join(parts) + "\n"

    def build_style_card(self) -> str:
        """精简风格卡（~220 字，一行）：每桥段注入 get_writing_context 的 style_card 提醒。
        只取身份/调性 + 前 3 句式 + 前 5 禁词 + 前 3 禁句式，防 dsh 尾部裁剪、防风格漂移；
        完整规则用 get_pen_style 取 build_writing_prompt。"""
        from .style_rules import StyleRuleLibrary
        lang_label = "中文" if (self.language or "zh").lower() != "en" else "英文"
        parts = [f"笔名：{self.pen_name}（{lang_label}）"]
        fp = self.style_fingerprint
        if fp.get("humor_style"):
            parts.append(f"幽默：{fp['humor_style']}")
        if fp.get("action_style"):
            parts.append(f"动作：{fp['action_style']}")
        own = [r for r in StyleRuleLibrary().rules_for(self.id) if r.enabled and r.pattern]
        prefers = [r.pattern for r in own if r.kind == "prefer"][:3]
        if prefers:
            parts.append("句式：" + "；".join(prefers))
        words = [r.pattern for r in own if r.kind in ("ban", "word") and r.replacements][:5]
        if words:
            parts.append("禁词：" + "、".join(w + "→改写" for w in words))
        patterns = [r.desc or r.pattern for r in own
                    if r.kind == "ban" and not r.replacements and r.severity == "warning"][:3]
        if patterns:
            parts.append("禁句式：" + "、".join(patterns))
        return "｜".join(parts)[:220]


class ProfileManager:
    """笔名档案管理器"""

    def __init__(self, profiles_dir: str | None = None):
        # 锚定到项目根目录，避免依赖当前工作目录（从任何目录启动都找得到数据）
        base = Path(__file__).resolve().parent.parent
        self.dir = Path(profiles_dir) if profiles_dir else base / "profiles"
        if not self.dir.is_absolute():
            self.dir = base / self.dir
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
               language: str = "zh",
               style_fingerprint: dict = None, word_print: dict = None,
               tropes: dict = None, platform_accounts: dict = None) -> PenNameProfile:
        from datetime import datetime
        profile_id = f"profile_{len(self._cache) + 1:03d}"
        profile = PenNameProfile(
            id=profile_id, pen_name=pen_name, language=language,
            description=description,
            style_fingerprint=style_fingerprint or {},
            word_print=word_print or {},
            tropes=tropes or {},
            platform_accounts=platform_accounts or {},
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
        "pen_name": "Lunaris",
        "description": "默认英文笔名（English default）",
        "style_fingerprint": {
            "sentence_length": "medium",
            "dialogue_ratio": 0.35,
            "paragraph_style": "compact",
        },
        "word_print": {},
        "tropes": {},
    },
]
