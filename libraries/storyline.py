"""
书籍故事线（Book Storyline）— 多大纲序列 + 桥段嵌套配置

核心理念：
  一本书不是一个大纲走到头，而是多个大纲按故事线串接，
  大纲之间可以重叠交叉（A 还没结束 B 已经开始），
  桥段在大纲阶段内可以嵌套、包含、重叠。
"""
from dataclasses import dataclass, field
from typing import Optional
import json

from core.json_store import read_json, write_json_atomic


# ═══════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════

# 世界观「设定圣经」默认结构 —— 由 WorldBuildingGenerator 填充，写作时经 prompt_harness 注入。
# description 是"一句话种子"老字段（新书启动页写入），结构化维度全空时作兜底注入。
DEFAULT_WORLD_BUILDING = {
    "description": "",        # 一句话设定种子（老字段，保持兼容）
    "tags": [],               # 题材标签（番茄式硬约束，多选；世界观/大纲/写作 prompt 注入）
    "era": "",                # 时代背景（含年份/纪元，如"灵气复苏后2030年"）
    "power_system": "",       # 力量体系（数值/技能语义全书唯一口径）
    "factions": [],           # 势力派系 [str] 或 [{name, stance, ...}]
    "rules": [],              # 世界规则（系统/金手指的数值语义写死）
    "geography": "",          # 地理：主要地域/大陆/城市/秘境/势力地盘
    "culture": "",            # 文化：宗门/家族/流派/风俗/价值观
    "history": "",            # 历史：背景大事件/时代断层/被掩盖的秘密
    "social_structure": "",   # 社会结构：阶级划分/权力架构/晋升与压制规则
    "core_conflict": "",      # 核心矛盾：驱动全书的根本冲突
    "world_summary": "",      # 设定文：一段整体世界观概述（200-300 字）
}

# ═══════════════════════════════════════════
# 角色统一存储（characters 数组，主角/配角合一）
# ═══════════════════════════════════════════

# 单条角色条目键（顺序即 to_dict 展示顺序）
_CHAR_FIELDS = ("name", "role", "identity", "gender", "personality",
                "catchphrase", "brief", "title", "golden_finger",
                "age", "death_year", "archetype_id", "relations")

_CHAR_DEFAULT_ROLE = "配角"


def _canon_char(c) -> dict:
    """归一化单条角色：补默认键、age/death_year 强转 int、relations 归一到 [{name,relation}]。"""
    c = dict(c or {})
    out = {k: c.get(k, "") for k in _CHAR_FIELDS}
    out["role"] = str(out["role"] or "").strip() or _CHAR_DEFAULT_ROLE
    try:
        out["age"] = int(out["age"] or 0)
    except (TypeError, ValueError):
        out["age"] = 0
    try:
        out["death_year"] = int(out["death_year"] or 0)
    except (TypeError, ValueError):
        out["death_year"] = 0
    rels = []
    for r in (c.get("relations") or []):
        if isinstance(r, dict) and str(r.get("name", "") or "").strip():
            rels.append({"name": str(r["name"]).strip(),
                         "relation": str(r.get("relation", "") or "")})
    out["relations"] = rels
    return out


def _char_from_protagonist(p) -> dict:
    """旧 protagonist(dict) → 统一条目（role=主角，background→brief）。"""
    p = p or {}
    return {
        "name": str(p.get("name", "") or ""),
        "role": "主角",
        "identity": str(p.get("identity", "") or ""),
        "gender": str(p.get("gender", "") or ""),
        "personality": str(p.get("personality", "") or ""),
        "catchphrase": "",
        "brief": str(p.get("background", "") or ""),
        "title": "",
        "golden_finger": str(p.get("golden_finger", "") or ""),
        "age": int(p.get("age") or 0),
        "death_year": int(p.get("death_year") or 0),
        "archetype_id": "",
        "relations": [],
    }


def _char_from_support(c, mc_name) -> dict:
    """旧 supporting_cast 元素 → 统一条目（旧 role=职位 → 新 identity；旧 relation→relations[0]）。"""
    c = dict(c or {})
    rels = []
    rel = str(c.get("relation", "") or "").strip()
    if rel and mc_name:
        rels.append({"name": mc_name, "relation": rel})
    return {
        "name": str(c.get("name", "") or ""),
        "role": _CHAR_DEFAULT_ROLE,                  # 旧结构非主角一律"配角"，分类在新 UI 调整
        "identity": str(c.get("role", "") or ""),   # 旧 role 是职位 → 新 identity
        "gender": str(c.get("gender", "") or ""),
        "personality": str(c.get("personality", "") or ""),
        "catchphrase": str(c.get("catchphrase", "") or ""),
        "brief": str(c.get("brief", "") or ""),
        "title": str(c.get("title", "") or ""),
        "golden_finger": "",
        "age": int(c.get("age") or 0),
        "death_year": int(c.get("death_year") or 0),
        "archetype_id": str(c.get("archetype_id", "") or ""),
        "relations": rels,
    }


def normalize_basic_info(bi) -> dict:
    """把 basic_info 统一为 characters 数组（主角/配角合一）。旧结构自动迁移，幂等。

    - characters 已存在 → 逐条 _canon_char
    - 否则从 protagonist(dict) + supporting_cast(list) 派生
    - 兜底：无 role==主角 的有名字条目时，首个有名字条目标为主角
    - 移除旧键 protagonist/supporting_cast
    """
    bi = dict(bi or {})
    if isinstance(bi.get("characters"), list):
        chars = [_canon_char(c) for c in bi["characters"] if isinstance(c, dict)]
    else:
        chars = []
        protag = bi.get("protagonist") or {}
        if str(protag.get("name", "") or "").strip():
            chars.append(_char_from_protagonist(protag))
        mc_name = chars[0]["name"] if chars else ""
        for c in (bi.get("supporting_cast") or []):
            if isinstance(c, dict) and str(c.get("name", "") or "").strip():
                chars.append(_char_from_support(c, mc_name))
        bi["characters"] = chars
    # 兜底自动标主角（复刻旧"主角恒首"语义）
    if not any(str(c.get("role", "") or "").strip() == "主角"
               and str(c.get("name", "") or "").strip()
               for c in bi["characters"]):
        for c in bi["characters"]:
            if str(c.get("name", "") or "").strip():
                c["role"] = "主角"
                break
    bi.pop("protagonist", None)
    bi.pop("supporting_cast", None)
    return bi


def get_characters(bi) -> list:
    """读角色统一列表（读侧容忍旧键：characters 缺失时从旧键派生）。"""
    bi = bi or {}
    if isinstance(bi.get("characters"), list):
        return bi["characters"]
    return normalize_basic_info(bi).get("characters", [])


def get_mc(bi) -> dict:
    """严格取主角：role==主角 且有名字的条目；否则空 dict。"""
    for c in get_characters(bi):
        if str(c.get("role", "") or "").strip() == "主角" \
                and str(c.get("name", "") or "").strip():
            return c
    return {}


def relation_to_mc(c, bi) -> str:
    """取角色与主角的关系：扫描 relations 中 name==主角名；兜底旧 relation 字段。"""
    c = c or {}
    mc_name = str(get_mc(bi).get("name", "") or "").strip()
    if mc_name:
        for r in (c.get("relations") or []):
            if isinstance(r, dict) \
                    and str(r.get("name", "") or "").strip() == mc_name:
                return str(r.get("relation", "") or "")
    return str(c.get("relation", "") or "")

@dataclass
class OutlineSlot:
    """一个大纲在故事线上的位置"""
    id: str                        # 唯一标识
    template_id: str               # 对应 StructureLibrary 里的模板，""=已展开不依赖模板
    name: str                      # 显示名称（如"都市爽文开篇"）
    start_chapter: int = 1         # 从第几章开始
    end_chapter: int = 30          # 到第几章
    stages: list = field(default_factory=list)   # 从模板展开的阶段 [{name,min_ch,max_ch,events}]
    expanded: bool = False         # 是否已展开填充了桥段
    notes: str = ""                # 用户备注

    # 与其他大纲的关系
    overlaps_with: list[str] = field(default_factory=list)  # 与哪些大纲重叠（id 列表）
    predecessor: str = ""          # 前驱大纲 id
    successor: str = ""            # 后继大纲 id
    transition_type: str = "sequential"  # sequential(顺序接续)|overlap(重叠过渡)|merge(融合)

    # 叙事手法（故事线严谨性：顺叙/倒叙/插叙）
    narrative: str = "chronological"   # chronological(顺叙)|flashback(倒叙)|interleaved(插叙)
    narrative_target: str = ""         # flashback: 回忆的时间段/章节；interleaved: 所嵌入的主弧 id


@dataclass
class PlotSlot:
    """一个桥段在大纲阶段内的位置"""
    id: str                        # 唯一标识
    template_id: str               # 对应 PlotLibrary 里的模板
    name: str                      # 显示名称
    category: str = ""             # 爽文/开篇/战斗/...
    sub_category: str = ""         # 子分类
    outline_id: str = ""           # 属于哪个大纲
    stage_index: int = 0           # 属于哪个阶段（outline.stages 的索引）
    parent_plot_id: str = ""       # 嵌套：父桥段 id，空=顶级
    children_plot_ids: list[str] = field(default_factory=list)  # 子桥段

    # 位置信息（用于故事线展示）
    order: int = 0                 # 阶段内排序
    cover_beats: int = 4           # 预计覆盖多少个节拍
    template_structure: str = ""   # 桥段模板结构字符串（箭头流程）
    slots: list = field(default_factory=list)  # 变量槽位

    # 注入的加料
    gag_ids: list[str] = field(default_factory=list)    # 匹配的笑点
    theme_hints: list[str] = field(default_factory=list)  # 内涵提示（名字）
    theme_moments: list = field(default_factory=list)     # 阶段级内涵 [{name, position, how}]（rich）
    hook_points: list[str] = field(default_factory=list)  # 吸睛点

    confirmed: bool = False        # 用户已确认
    written_chapter: int = 0       # 已写入第几章（0=未写，用于断点续写）

    # 叙事线程（主线/副线/伏笔线；主角可多线并存）
    thread_id: str = "主线"          # 所属线程
    thread_seq: int = 0              # 线程内序号（组内 tie-break）
    resolves_plot_id: str = ""       # 收局槽位：解决/呼应哪个设局桥段 id（非空=收局）
    resolves_name: str = ""          # 冗余存设局桥段名，供 prompt/前端免查

    # 出场人物（主角恒在；配角按名规则匹配到桥段事件/骨架/槽位）
    roles: list[str] = field(default_factory=list)


@dataclass
class BookStoryline:
    """整本书的故事线配置 —— 新书启动的核心产出"""
    book_title: str = ""
    genre: str = ""
    sub_genre: str = ""
    words_per_chapter: int = 3000
    pen_name: str = ""
    platform: str = "fanqie"   # 目标平台：fanqie/qidian（写作时注入平台写作约束）

    # 基础信息库（参考 show-me-the-story 的设定体系）
    basic_info: dict = field(default_factory=lambda: {
        "characters": [],  # 角色统一列表（主角/配角合一，role 字段标记主角）
        "world_building": dict(DEFAULT_WORLD_BUILDING),
        "tone": "",        # 轻松/沉重/热血/幽默
        "target_audience": "",
        "pov": "第三人称",  # 第一人称/第三人称（全篇统一，防人称漂移）
        "era_language": "",  # 时代语言约束（禁止晚于该时代的网络新词）
    })

    # 故事线
    outlines: list[OutlineSlot] = field(default_factory=list)
    plots: list[PlotSlot] = field(default_factory=list)

    # 叙事线程定义（[{"id","name","desc"}, ...]）
    threads: list[dict] = field(default_factory=list)

    # 读者承诺台账（设局→收局的伏笔生命周期，写作时免费规则登记/兑现）
    # 每项: {id, setup_plot_id, type, desc, status: pending|advanced|fulfilled,
    #        setup_chapter, deadline_chapter, payoff_plot_id, payoff_chapter}
    promises: list[dict] = field(default_factory=list)

    # 全书贯穿元素
    themes: list[str] = field(default_factory=list)
    global_gags: list[str] = field(default_factory=list)

    # 状态
    phase: str = "config"          # config|outlines|plots|gags|ready
    generated_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "book_title": self.book_title,
            "genre": self.genre,
            "sub_genre": self.sub_genre,
            "words_per_chapter": self.words_per_chapter,
            "pen_name": self.pen_name,
            "platform": self.platform,
            "basic_info": self.basic_info,
            "outlines": [{
                "id": o.id, "template_id": o.template_id, "name": o.name,
                "start_chapter": o.start_chapter, "end_chapter": o.end_chapter,
                "stages": o.stages, "expanded": o.expanded, "notes": o.notes,
                "overlaps_with": o.overlaps_with,
                "predecessor": o.predecessor, "successor": o.successor,
                "transition_type": o.transition_type,
                "narrative": o.narrative,
                "narrative_target": o.narrative_target,
            } for o in self.outlines],
            "plots": [{
                "id": p.id, "template_id": p.template_id, "name": p.name,
                "category": p.category, "sub_category": p.sub_category,
                "outline_id": p.outline_id, "stage_index": p.stage_index,
                "parent_plot_id": p.parent_plot_id,
                "children_plot_ids": p.children_plot_ids,
                "order": p.order, "cover_beats": p.cover_beats,
                "template_structure": p.template_structure,
                "slots": p.slots,
                "gag_ids": p.gag_ids, "theme_hints": p.theme_hints,
                "theme_moments": p.theme_moments,
                "hook_points": p.hook_points,
                "confirmed": p.confirmed,
                "written_chapter": p.written_chapter,
                "thread_id": p.thread_id, "thread_seq": p.thread_seq,
                "resolves_plot_id": p.resolves_plot_id,
                "resolves_name": p.resolves_name,
                "roles": p.roles,
            } for p in self.plots],
            "threads": self.threads,
            "promises": self.promises,
            "themes": self.themes,
            "global_gags": self.global_gags,
            "phase": self.phase,
            "generated_at": self.generated_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BookStoryline":
        tl = cls(
            book_title=d.get("book_title", ""),
            genre=d.get("genre", ""),
            sub_genre=d.get("sub_genre", ""),
            words_per_chapter=d.get("words_per_chapter", 3000),
            pen_name=d.get("pen_name", ""),
            platform=d.get("platform", "fanqie"),
            basic_info=normalize_basic_info(d.get("basic_info", {})),
            themes=d.get("themes", []),
            global_gags=d.get("global_gags", []),
            phase=d.get("phase", "config"),
            generated_at=d.get("generated_at", ""),
            updated_at=d.get("updated_at", ""),
        )
        tl.outlines = [OutlineSlot(
            id=o.get("id", ""), template_id=o.get("template_id", ""),
            name=o.get("name", ""), start_chapter=o.get("start_chapter", 1),
            end_chapter=o.get("end_chapter", 30), stages=o.get("stages", []),
            expanded=o.get("expanded", False), notes=o.get("notes", ""),
            overlaps_with=o.get("overlaps_with", []),
            predecessor=o.get("predecessor", ""),
            successor=o.get("successor", ""),
            transition_type=o.get("transition_type", "sequential"),
            narrative=o.get("narrative", "chronological"),
            narrative_target=o.get("narrative_target", ""),
        ) for o in d.get("outlines", [])]
        tl.plots = [PlotSlot(
            id=p.get("id", ""), template_id=p.get("template_id", ""),
            name=p.get("name", ""), category=p.get("category", ""),
            sub_category=p.get("sub_category", ""),
            outline_id=p.get("outline_id", ""), stage_index=p.get("stage_index", 0),
            parent_plot_id=p.get("parent_plot_id", ""),
            children_plot_ids=p.get("children_plot_ids", []),
            order=p.get("order", 0), cover_beats=p.get("cover_beats", 4),
            template_structure=p.get("template_structure", ""),
            slots=p.get("slots", []),
            gag_ids=p.get("gag_ids", []),
            theme_hints=p.get("theme_hints", []),
            theme_moments=p.get("theme_moments", []),
            hook_points=p.get("hook_points", []),
            confirmed=p.get("confirmed", False),
            written_chapter=p.get("written_chapter", 0),
            thread_id=p.get("thread_id", "主线"),
            thread_seq=p.get("thread_seq", 0),
            resolves_plot_id=p.get("resolves_plot_id", ""),
            resolves_name=p.get("resolves_name", ""),
            roles=p.get("roles", []),
        ) for p in d.get("plots", [])]
        tl.threads = d.get("threads", [])
        tl.promises = d.get("promises", [])
        return tl


# ═══════════════════════════════════════════
# 故事线生成器
# ═══════════════════════════════════════════

def structure_to_stages(tmpl) -> list[dict]:
    """把结构模板的阶段展开为 stage dict（name/min_ch/max_ch/events/themes）——多实现共用防漂移。"""
    return [
        {"name": s.name, "min_ch": s.min_chapters, "max_ch": s.max_chapters,
         "events": s.key_events[:5],
         "themes": list(s.themes or [])}
        for s in tmpl.stages
    ]


# 内涵→桥段兼容映射（免费规则，替代 theme_lib.compatible_plots）
# 由内置内涵 compatible_plots 反查：桥段模板 id → 可承载内涵名（保留完整名，与 tl.themes 一致）。
# 删除 theme_lib 后此常量是「内涵跟随桥段」的唯一数据源。
THEME_PLOT_COMPAT = {
    "plot_dating_001": ["公平（Justice）", "身份与伪装（Identity & Disguise）"],
    "plot_dating_003": ["归属感（Belonging）", "传承与突破（Legacy & Breakthrough）"],
    "plot_dating_004": ["成长的代价（Cost of Growth）", "传承与突破（Legacy & Breakthrough）"],
    "plot_dating_005": ["公平（Justice）"],
    "plot_dating_006": ["成长的代价（Cost of Growth）", "牺牲（Sacrifice）"],
    "plot_dating_008": ["身份与伪装（Identity & Disguise）"],
    "plot_dating_010": ["公平（Justice）", "成长的代价（Cost of Growth）",
                        "牺牲（Sacrifice）", "归属感（Belonging）"],
}


def mount_themes_and_hooks(plot: "PlotSlot", storyline_themes: list) -> None:
    """给桥段挂载内涵并标注吸睛点 —— StorylineBuilder/OutlineGenerator 共用，单一实现防漂移。

    内涵来源优先级：
      1) 桥段已从所属阶段继承 theme_moments（阶段级内涵，含位置/手法）→ theme_hints 取其名
      2) 否则按 THEME_PLOT_COMPAT 命中书级内涵（免费规则兜底），不强挂
    未命中的内涵仍作为书级可用线索随「书级设定卡」注入写作；笑点完全涌现，不在此分配。
    """
    moments = list(getattr(plot, "theme_moments", None) or [])
    if moments:
        names = [m.get("name", "") if isinstance(m, dict) else str(m) for m in moments]
        plot.theme_hints = [n for n in names if n][:3]
    else:
        compatible = THEME_PLOT_COMPAT.get(getattr(plot, "template_id", ""), [])
        theme_hints = [name for name in storyline_themes if name in compatible]
        plot.theme_hints = theme_hints[:2]

    hook_candidates = []
    for slot in plot.slots[:3]:
        sname = slot.get("name", "") if isinstance(slot, dict) else getattr(slot, "name", "")
        opts = (slot.get("options", []) if isinstance(slot, dict)
                else getattr(slot, "options", []))
        if sname and opts:
            hook_candidates.append(f"{plot.name}「{sname}」的{opts[0]}")
    plot.hook_points = hook_candidates[:2] if hook_candidates else [
        f"{plot.name}的开场",
        f"{plot.name}的高潮反转",
    ]


class StorylineBuilder:
    """根据流派和用户需求，生成大纲故事线 + 桥段配置"""

    def __init__(self, structure_lib=None, plot_lib=None, gag_lib=None, llm_client=None):
        self.structures = structure_lib
        self.plots = plot_lib
        self.gags = gag_lib
        self.llm = llm_client
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def build_outline_sequence(
        self,
        genre: str = "玄幻",
        sub_genre: str = "",
        custom_context: str = "",
        max_outlines: int = 5,
        mode: str = "ai",
    ) -> list[OutlineSlot]:
        """
        生成大纲序列。
        mode="ai" → AI 辅助（需要 llm）；mode="rule" → 纯规则。
        """
        if mode == "rule" or not self.llm:
            return self._rule_build_sequence(genre)
        return self._ai_build_sequence(genre, sub_genre, custom_context, max_outlines)

    def _rule_build_sequence(self, genre: str) -> list[OutlineSlot]:
        """规则拼接：按流派选 2-3 个大纲，默认顺序接续"""
        if not self.structures:
            return []

        # 流派→常见大纲序列
        genre_map = {
            "玄幻": ["struct_xuanhuan_01", "struct_xuanhuan_01"],  # 升级×2
            "都市": ["struct_dushi_01", "struct_dushi_01"],
            "言情": ["struct_tianwen_01", "struct_tianwen_01"],
            "悬疑": ["struct_xuanyi_01", "struct_xuanyi_01"],
            "穿越": ["struct_chuanyue_01", "struct_xuanhuan_01"],
        }

        template_ids = genre_map.get(genre, ["struct_xuanhuan_01"])
        outlines = []
        ch = 1
        for i, tid in enumerate(template_ids):
            tmpl = self.structures.get_by_id(tid)
            if not tmpl:
                continue
            oid = self._next_id("outline")
            outlines.append(OutlineSlot(
                id=oid,
                template_id=tid,
                name=f"{tmpl.name}{f'(第{i+1}部分)' if len(template_ids)>1 else ''}",
                start_chapter=ch,
                end_chapter=ch + tmpl.total_chapters - 1,
                stages=structure_to_stages(tmpl),
                predecessor=outlines[-1].id if outlines else "",
                transition_type="sequential",
            ))
            if len(outlines) > 1:
                outlines[-2].successor = outlines[-1].id
            ch = outlines[-1].end_chapter + 1
        return outlines

    def _ai_build_sequence(
        self, genre: str, sub_genre: str, context: str, max_outlines: int
    ) -> list[OutlineSlot]:
        """AI 辅助生成大纲序列"""
        available = ""
        if self.structures:
            templates = self.structures.templates[:20]  # 最多 20 个候选
            available = "\n".join(
                f"- {t.id}: {t.name} ({t.total_chapters}章) | 阶段: {'→'.join(s.name for s in t.stages[:5])}"
                for t in templates
            )

        prompt = f"""为一本{genre}/{sub_genre}类网络小说设计大纲故事线。

用户想法：{context if context else '标准开局'}

请从可用大纲模板中选择 2-{max_outlines}个，按故事线串联。大纲之间可以重叠交叉（前一个还没结束，后一个已经开始）。

返回JSON：
{{
  "outlines": [
    {{
      "template_id": "struct_xxx",
      "name": "给这段起个名",
      "start_chapter": 1,
      "end_chapter": 30,
      "overlaps_with": [],
      "transition_type": "sequential|overlap|merge",
      "reason": "为什么在这个位置选这个大纲"
    }}
  ]
}}

注意：
- 相邻大纲建议有 3-5 章的重叠区（过渡更自然）
- 同一流派下可以有不同风格的大纲（如开局爽文→中期正剧）
- 总章节数控制在合理范围内（不要超过 500）

可用大纲模板：
{available[:2000]}"""

        try:
            raw = self.llm.call(
                "你是一位专业的网络小说策划编辑。请只返回JSON，不要加任何额外文字。",
                prompt, temperature=0.7, max_tokens=2048)
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            outlines_data = data.get("outlines", [])
        except Exception:
            return self._rule_build_sequence(genre)

        outlines = []
        prev_id = ""
        for od in outlines_data:
            oid = self._next_id("outline")
            tid = od.get("template_id", "")
            # 从模板库展开阶段
            stages = []
            if self.structures:
                tmpl = self.structures.get_by_id(tid)
                if tmpl:
                    stages = structure_to_stages(tmpl)
            outline = OutlineSlot(
                id=oid,
                template_id=tid,
                name=od.get("name", f"大纲{len(outlines)+1}"),
                start_chapter=od.get("start_chapter", outlines[-1].end_chapter + 1 if outlines else 1),
                end_chapter=od.get("end_chapter", (outlines[-1].end_chapter if outlines else 0) + 30),
                stages=stages,
                overlaps_with=od.get("overlaps_with", []),
                predecessor=prev_id,
                transition_type=od.get("transition_type", "sequential"),
            )
            if outlines:
                outlines[-1].successor = oid
            outlines.append(outline)
            prev_id = oid

        # 填充 overlaps_with（引用前面大纲的 id）
        for i, o in enumerate(outlines):
            if i > 0 and o.start_chapter <= outlines[i-1].end_chapter:
                o.overlaps_with.append(outlines[i-1].id)

        return outlines

    def fill_plots_for_outline(
        self, outline: OutlineSlot, storyline: BookStoryline,
    ) -> list[PlotSlot]:
        """
        给一个大纲的每个阶段填充桥段。

        支持嵌套：第一个桥段作为"框"，后续桥段嵌入其中。
        """
        if not self.plots:
            return []

        new_plots = []
        for si, stage in enumerate(outline.stages):
            stage_name = stage.get("name", "")
            events = stage.get("events", [])

            # 匹配桥段：阶段名+事件描述+流派
            context = f"{outline.name} {stage_name} {' '.join(events)}"
            candidates = self.plots.match_for_chapter(context, storyline.genre)
            if not candidates:
                candidates = self.plots.search(category=storyline.genre)
                if not candidates:
                    candidates = self.plots.templates[:1]

            # 取 1-3 个桥段（支持嵌套）
            selected = candidates[:min(3, len(candidates))]
            parent_id = ""
            for pi, tmpl in enumerate(selected):
                pid = self._next_id("plot")
                p = PlotSlot(
                    id=pid,
                    template_id=tmpl.id,
                    name=tmpl.name,
                    category=tmpl.category,
                    sub_category=tmpl.sub_category or "",
                    outline_id=outline.id,
                    stage_index=si,
                    parent_plot_id=parent_id,
                    order=pi,
                    cover_beats=tmpl.word_range[1] // 400 if tmpl.word_range else 4,
                    template_structure="→".join(tmpl.template_structure) if tmpl.template_structure else "",
                    slots=[{"name": s.name, "default": s.default, "options": s.options}
                           for s in tmpl.slots],
                    theme_moments=stage.get("themes", []),
                )
                new_plots.append(p)
                if parent_id:
                    # 找到父桥段并添加子关系
                    for existing in storyline.plots + new_plots:
                        if existing.id == parent_id:
                            existing.children_plot_ids.append(pid)
                            break
                parent_id = pid  # 链式嵌套（每个桥段包下一个）

        outline.expanded = True
        return new_plots

    def fill_themes_and_hooks(self, plots: list[PlotSlot], storyline: BookStoryline):
        """给桥段挂载内涵（跟随桥段）并标注吸睛点（委托共享 mount_themes_and_hooks）。"""
        for p in plots:
            mount_themes_and_hooks(p, storyline.themes)


# ═══════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════

def save_storyline(storyline: BookStoryline, path: str):
    """保存故事线到文件"""
    from pathlib import Path
    p = Path(path)
    write_json_atomic(p, storyline.to_dict())


def load_storyline(path: str) -> Optional[BookStoryline]:
    """从文件加载故事线"""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = read_json(p, {})
        return BookStoryline.from_dict(data)
    except Exception:
        return None


def _deep_keep_existing(existing: dict, generated: dict) -> dict:
    """以 generated 为基础，existing 里非空字段覆盖（dict 递归）。"""
    existing = existing or {}
    generated = generated or {}
    merged = dict(generated)
    for k, ev in existing.items():
        gv = merged.get(k)
        if isinstance(ev, dict) and isinstance(gv, dict):
            merged[k] = _deep_keep_existing(ev, gv)
        elif ev not in (None, "", [], {}):
            merged[k] = ev
    return merged


def _merge_characters(existing_chars, generated_chars) -> list:
    """characters 数组合并：
    - MC 逐字段深合并（existing 非空字段保留，generated 补空）
    - 非 MC：existing 非空则整组保留，否则用 generated（复刻旧 supporting_cast 语义）
    """
    existing_chars = list(existing_chars or [])
    generated_chars = list(generated_chars or [])

    def _is_mc(c):
        return str(c.get("role", "") or "").strip() == "主角" \
            and str(c.get("name", "") or "").strip()

    ex_mc = next((c for c in existing_chars if _is_mc(c)), None)
    gen_mc = next((c for c in generated_chars if _is_mc(c)), None)
    merged = []
    if ex_mc:
        merged.append(_deep_keep_existing(ex_mc, gen_mc or {}))
    elif gen_mc:
        merged.append(dict(gen_mc))

    ex_nonmc = [c for c in existing_chars if c is not ex_mc]
    gen_nonmc = [c for c in generated_chars if c is not gen_mc]
    merged.extend(ex_nonmc or gen_nonmc)
    # 兜底标主角
    if merged and not _is_mc(merged[0]):
        for c in merged:
            if str(c.get("name", "") or "").strip():
                c["role"] = "主角"
                break
    return merged


def merge_basic_info(existing: dict, generated: dict) -> dict:
    """以 generated 为基础，保留 existing 里用户已填的非空字段（原地累加用）。

    characters 单独合并（_merge_characters）；其余键沿用旧 dict 深合并语义。
    供大纲生成器与 web_ui 共用，避免同一逻辑两份拷贝。
    """
    existing = normalize_basic_info(existing)
    generated = normalize_basic_info(generated)
    merged = {}
    for key, gv in generated.items():
        ev = existing.get(key)
        if key == "characters":
            merged[key] = _merge_characters(
                existing.get("characters", []), gv)
        elif isinstance(gv, dict) and isinstance(ev, dict):
            merged[key] = _deep_keep_existing(ev, gv)
        elif ev not in (None, "", [], {}):
            merged[key] = ev
        else:
            merged[key] = gv
    for key, ev in existing.items():  # generated 没覆盖的字段也保留
        if key not in merged:
            merged[key] = ev
    return merged


def basic_info_world_done(basic_info) -> bool:
    """basic_info 是否已具备世界观设定（宽松判定）。

    供引擎规划态进入 + 书详情「当前阶段」判定共用。
    - _world_generated 标记（世界卡已确认）→ True
    - world_building 任一维度（含 description 一句话种子）填充 或 主角名非空 → True
    - 空 basic_info → False（无设定也无大纲的老书走报错路径）
    """
    bi = basic_info or {}
    if bi.get("_world_generated"):
        return True
    wb = bi.get("world_building") or {}
    if not isinstance(wb, dict):
        wb = {}
    filled = 0
    for k in ("era", "power_system", "geography", "culture", "history",
              "social_structure", "core_conflict", "world_summary", "description"):
        v = wb.get(k)
        if (isinstance(v, list) and v) or str(v or "").strip():
            filled += 1
    protag_name = str(get_mc(bi).get("name", "") or "").strip()
    return filled >= 1 or bool(protag_name)


# 常见词过滤，防角色名误判（如"主角""大家"）
_ROLE_STOPWORDS = {
    "这个", "那个", "什么", "怎么", "一个", "一下", "主角", "大家", "系统",
    "他们", "我们", "你们", "老板", "经理", "同事", "身份", "金手指",
}

# 分类启发式兜底：桥段模板文本是泛化的，名字规则匹配常落空；
# 按桥段 category 推断该出现的配角类型（凭 role/relation 关键词匹配）
_CATEGORY_RELATION = {
    "职场": ("同事", "上司", "老板", "主管", "员工", "老员工"),
    "爽文": ("同事", "上司", "老板", "主管"),
    "打脸": ("同事", "上司", "老板", "主管"),
    "都市": ("同事", "房东", "邻居"),
    "情感": ("家人", "房东", "朋友", "恋人", "邻里", "青梅"),
    "日常": ("家人", "房东", "朋友", "邻居", "邻里"),
    "羁绊": ("家人", "朋友", "恋人"),
    "战斗": ("师兄", "师叔", "对手", "同伴"),
    "悬疑": ("对手", "主管", "同事"),
}


def annotate_plot_roles(tl: BookStoryline) -> int:
    """规则标注每个桥段的出场人物（主角恒在首位；配角名出现在桥段事件/骨架/槽位/吸睛文本 → 出场）。

    幂等：重跑覆盖。返回标注到出场人物的桥段数。
    """
    if not tl or not tl.plots:
        return 0
    bi = tl.basic_info or {}
    protag_name = str(get_mc(bi).get("name", "") or "").strip()
    cast_map = {}
    for c in get_characters(bi):
        if isinstance(c, dict) and c.get("name"):
            n = str(c["name"]).strip()
            if n != protag_name:
                cast_map[n] = c
    names = [n for n in cast_map if len(n) >= 2 and n not in _ROLE_STOPWORDS]
    if protag_name:
        names.insert(0, protag_name)

    outline_map = {o.id: o for o in (tl.outlines or [])}
    annotated = 0
    for p in tl.plots:
        parts = [str(getattr(p, "name", "") or "")]
        if getattr(p, "template_structure", ""):
            parts.append(str(p.template_structure))
        for s in (getattr(p, "slots", None) or []):
            if isinstance(s, dict):
                parts.append(str(s.get("name", "")) + str(s.get("default", "")))
                parts.append("".join(str(x) for x in (s.get("options") or [])))
        for h in (getattr(p, "hook_points", None) or []):
            parts.append(str(h))
        o = outline_map.get(p.outline_id)
        if o and 0 <= p.stage_index < len(o.stages or []):
            stage = o.stages[p.stage_index]
            if isinstance(stage, dict) and stage.get("events"):
                parts.extend(str(e) for e in stage["events"])
        text = "".join(parts)

        roles = []
        if protag_name:
            roles.append(protag_name)
        # 1. 名字规则匹配（主角恒首）
        for n in names:
            if n != protag_name and n in text:
                roles.append(n)
        # 2. 分类启发式兜底：名字没命中时，按桥段 category 推断出场配角
        if len(roles) <= 1:
            rel_kws = _CATEGORY_RELATION.get(str(getattr(p, "category", "") or ""), ())
            for n, c in cast_map.items():
                if n == protag_name or n in roles:
                    continue
                rel_txt = "".join(str(r.get("relation", "") or "")
                                  for r in (c.get("relations") or []) if isinstance(r, dict))
                role_relation = str(c.get("identity", "")) + str(c.get("title", "")) + rel_txt
                if any(k in role_relation for k in rel_kws):
                    roles.append(n)
                    if len(roles) >= 3:
                        break
        p.roles = roles
        if roles:
            annotated += 1
    return annotated

