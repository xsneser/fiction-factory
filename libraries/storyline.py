"""
书籍故事线（Book Storyline）— 多大纲序列 + 情节段嵌套配置

核心理念：
  一本书不是一个大纲走到头，而是多个大纲按故事线串接，
  大纲之间可以重叠交叉（A 还没结束 B 已经开始），
  情节段在大纲阶段内可以嵌套、包含、重叠。
"""
from dataclasses import dataclass, field
from typing import Optional
import json
import math

from core.json_store import read_json, write_json_atomic
from libraries.world_tags import genre_from_tags


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
_CHAR_FIELDS = ("name", "role", "importance", "identity", "gender", "personality",
                "catchphrase", "brief", "title", "golden_finger", "faction",
                "age", "death_year", "archetype_id", "relations",
                "behavior", "speech_profile", "development_plan")

_CHAR_DEFAULT_ROLE = "配角"

# 行为模型（情境→一贯反应）与语言倾向（非固定句式/口头禅复读）的嵌套结构键
_BEHAVIOR_SLOT_KEYS = {
    "decision_style": ("under_pressure", "danger", "betrayal"),
    "communication_style": ("stranger", "friend", "enemy"),
    "emotion_expression": ("anger", "fear", "sadness"),
}
_SPEECH_LIST_KEYS = ("habits", "forbidden")


def _norm_behavior(v) -> dict:
    """归一 behavior：{group:{slot:str}}，非 dict 组 → 全空。"""
    if not isinstance(v, dict):
        v = {}
    out = {}
    for grp, slots in _BEHAVIOR_SLOT_KEYS.items():
        g = v.get(grp) if isinstance(v.get(grp), dict) else {}
        out[grp] = {s: str(g.get(s, "") or "").strip() for s in slots}
    return out


def _norm_speech_profile(v) -> dict:
    """归一 speech_profile：{rhythm,tone:str, habits/forbidden:list[str]}（语言倾向）。"""
    if not isinstance(v, dict):
        v = {}
    _lst = lambda x: [str(i).strip() for i in (x or []) if isinstance(i, str) and str(i).strip()]
    return {
        "rhythm": str(v.get("rhythm", "") or "").strip(),
        "tone": str(v.get("tone", "") or "").strip(),
        "habits": _lst(v.get("habits")),
        "forbidden": _lst(v.get("forbidden")),
    }


def _norm_development_plan(v):
    """development_plan：str 一句成长方向，或 {growth_target,notes}；空 → ""。"""
    if isinstance(v, dict):
        return {k: str(x) for k, x in v.items() if str(x or "").strip()} or ""
    return str(v or "").strip()


def _canon_char(c) -> dict:
    """归一化单条角色：补默认键、age/death_year/importance 强转 int、relations 归一到 [{name,relation}]。"""
    c = dict(c or {})
    out = {k: c.get(k, "") for k in _CHAR_FIELDS}
    out["role"] = str(out["role"] or "").strip() or _CHAR_DEFAULT_ROLE
    # importance：排名制 1 最高；缺失默认 主角1 / 其他2
    try:
        imp = int(out["importance"] or 0)
    except (TypeError, ValueError):
        imp = 0
    if imp < 1:
        imp = 1 if out["role"] == "主角" else 2
    out["importance"] = imp
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
    # 行为模型 / 语言倾向 / 成长规划（可选嵌套；空 → 默认空结构，不随人物丢弃）
    out["behavior"] = _norm_behavior(c.get("behavior"))
    out["speech_profile"] = _norm_speech_profile(c.get("speech_profile"))
    out["development_plan"] = _norm_development_plan(c.get("development_plan"))
    return out


def _char_from_protagonist(p) -> dict:
    """旧 protagonist(dict) → 统一条目（role=主角，background→brief）。"""
    p = p or {}
    return {
        "name": str(p.get("name", "") or ""),
        "role": "主角",
        "importance": 1,
        "identity": str(p.get("identity", "") or ""),
        "gender": str(p.get("gender", "") or ""),
        "personality": str(p.get("personality", "") or ""),
        "catchphrase": "",
        "brief": str(p.get("background", "") or ""),
        "title": "",
        "golden_finger": str(p.get("golden_finger", "") or ""),
        "faction": "",
        "age": int(p.get("age") or 0),
        "death_year": int(p.get("death_year") or 0),
        "archetype_id": "",
        "relations": [],
        "behavior": _norm_behavior(None),
        "speech_profile": _norm_speech_profile(None),
        "development_plan": "",
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
        "importance": int(c.get("importance") or 0) or 2,
        "identity": str(c.get("role", "") or ""),   # 旧 role 是职位 → 新 identity
        "gender": str(c.get("gender", "") or ""),
        "personality": str(c.get("personality", "") or ""),
        "catchphrase": str(c.get("catchphrase", "") or ""),
        "brief": str(c.get("brief", "") or ""),
        "title": str(c.get("title", "") or ""),
        "golden_finger": "",
        "faction": "",
        "age": int(c.get("age") or 0),
        "death_year": int(c.get("death_year") or 0),
        "archetype_id": str(c.get("archetype_id", "") or ""),
        "relations": rels,
        "behavior": _norm_behavior(c.get("behavior")),
        "speech_profile": _norm_speech_profile(c.get("speech_profile")),
        "development_plan": _norm_development_plan(c.get("development_plan")),
    }


def normalize_basic_info(bi) -> dict:
    """把 basic_info 统一为 characters 数组（主角/配角合一）。旧结构自动迁移，幂等。

    - characters 已存在且非空 → 逐条 _canon_char
    - 否则（含 characters 为空列表）从 protagonist(dict) + supporting_cast(list) 派生
      —— 空 characters 时仍回退派生，避免 'characters: []' 吞掉旧格式 protagonist
    - 兜底：无 role==主角 的有名字条目时，首个有名字条目标为主角
    - 移除旧键 protagonist/supporting_cast
    """
    bi = dict(bi or {})
    if isinstance(bi.get("characters"), list) and bi["characters"]:
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
    # 统一写回：if 分支（characters 已存在）同样落 _canon_char 的归一化结果，
    # 保证缺省字段（behavior/speech_profile/development_plan/relations 等）被补全/归一
    bi["characters"] = chars
    # 兜底自动标主角（复刻旧"主角恒首"语义）：importance 未设时置 1
    if not any(str(c.get("role", "") or "").strip() == "主角"
               and str(c.get("name", "") or "").strip()
               for c in bi["characters"]):
        for c in bi["characters"]:
            if str(c.get("name", "") or "").strip():
                c["role"] = "主角"
                if not (c.get("importance") or 0):
                    c["importance"] = 1
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
    """取主角：role==主角 且有名字优先；否则取 importance==1 且有名字（弱化 role 区分后的兜底）。"""
    chars = get_characters(bi)
    for c in chars:
        if str(c.get("role", "") or "").strip() == "主角" \
                and str(c.get("name", "") or "").strip():
            return c
    for c in chars:
        if str(c.get("name", "") or "").strip() and int(c.get("importance") or 0) == 1:
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


# ─── 弧的章/字双坐标换算（字数轴=落盘权威；start_word/end_word 0 基、start 含/end 不含） ───
def chapter_to_word(ch, wpc):
    """1-based 章号 → 0-based 字数起点：第 N 章占 [(N-1)*wpc, N*wpc)。"""
    return max(0, (int(ch) - 1) * int(wpc or 3000))


def word_to_chapter_start(w, wpc):
    """0-based 字数 w（含）→ 所在章（1-based）。"""
    return 1 if (w or 0) <= 0 else (int(w) // int(wpc or 3000)) + 1


def word_to_chapter_end(ew, wpc):
    """排他 end 字数 → 覆盖到的末章（1-based）。"""
    if not ew or ew <= 0:
        return 1
    return max(1, math.ceil(int(ew) / int(wpc or 3000)))


def reconcile_outline(o, wpc):
    """幂等同步弧的章/字双坐标：缺哪对补哪对；双全则原样保留（防字坐标被章坐标覆盖丢精度）。

    - 仅章坐标（旧书）→ 按每章字数推导字坐标（整章对齐）。
    - 仅字坐标（新书，无章节输入）→ 推导章坐标作兼容视图。
    - 双全 → 都不动。"""
    wpc = wpc or 3000
    has_word = (o.start_word is not None and o.end_word is not None
                and o.start_word >= 0 and o.end_word >= 0)
    has_ch = (o.start_chapter is not None and o.end_chapter is not None
              and o.start_chapter > 0 and o.end_chapter > 0)
    if not has_word:
        o.start_word = chapter_to_word(o.start_chapter if has_ch else 1, wpc)
        o.end_word = int(o.end_chapter if has_ch else 30) * wpc
    if not has_ch:
        o.start_chapter = word_to_chapter_start(o.start_word, wpc)
        o.end_chapter = max(o.start_chapter, word_to_chapter_end(o.end_word, wpc))


def _payload_int(v):
    """int 或纯整数字符串 → int；None/bool/其他 → None（对齐 save_outlines int() 与前端 parseInt）。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s and s.lstrip("+-").isdigit():
            try:
                return int(s)
            except ValueError:
                return None
    return None


def _arc_fields(o):
    """dict 或 OutlineSlot → (id, name, start_chapter, end_chapter, start_word, end_word, parent_arc_id)。"""
    if isinstance(o, dict):
        return (str(o.get("id") or "").strip(), o.get("name") or "",
                o.get("start_chapter"), o.get("end_chapter"),
                o.get("start_word"), o.get("end_word"),
                str(o.get("parent_arc_id") or "").strip())
    return (str(getattr(o, "id", "") or "").strip(), getattr(o, "name", "") or "",
            getattr(o, "start_chapter", None), getattr(o, "end_chapter", None),
            getattr(o, "start_word", None), getattr(o, "end_word", None),
            str(getattr(o, "parent_arc_id", "") or "").strip())


def _plot_fields(p):
    """dict 或 PlotSlot → (id, name, outline_id)。"""
    if isinstance(p, dict):
        return (str(p.get("id") or "").strip(), p.get("name") or "",
                str(p.get("outline_id") or "").strip())
    return (str(getattr(p, "id", "") or "").strip(), getattr(p, "name", "") or "",
            str(getattr(p, "outline_id", "") or "").strip())


def outline_payload_problems(outlines, plots, known_outlines=(), known_plots=()):
    """校验提交的 outlines/plots 载荷结构（结构必填，缺则拒收、不自动换算兜底）。

    每条弧须 id 非空唯一 + name 非空 + 一组完整跨度（字数对 0<=start<end 或 章对
    1<=start<=end；半组/全缺非法）。每个情节段须 id 非空唯一 + outline_id 指向
    存在的最底层（叶）弧。known_outlines/known_plots 只作上下文（save_outlines
    append 可把 plots 挂到已落盘弧、防 id 撞），自身不被校验。
    返回问题字符串列表，空 = 通过。"""
    probs = []
    new_arcs = [_arc_fields(o) for o in (outlines or [])]
    known_arcs = [_arc_fields(o) for o in (known_outlines or [])]
    seen_arc_ids = {a[0] for a in known_arcs if a[0]}
    for i, (aid, aname, sc, ec, sw, ew, _parent) in enumerate(new_arcs):
        who = f"弧[{i}]" + (f" id={aid}" if aid else "（未命名）")
        if not aid:
            probs.append(f"{who} 缺 id")
        elif aid in seen_arc_ids:
            probs.append(f"弧 id={aid} 重复（第 {i} 项与已有弧 id 冲突）")
        else:
            seen_arc_ids.add(aid)
        if not aname:
            probs.append(f"弧[id={aid or '?'}] 缺 name")
        _sw, _ew = _payload_int(sw), _payload_int(ew)
        _sc, _ec = _payload_int(sc), _payload_int(ec)
        if _sw is not None and _ew is not None:
            if _sw < 0 or _ew <= _sw:
                probs.append(f"弧[id={aid or '?'}] 字数跨度非法：须整数且 0<=start_word<end_word"
                             f"（当前 start_word={sw!r}, end_word={ew!r}）")
        elif _sc is not None and _ec is not None:
            if _sc < 1 or _ec < _sc:
                probs.append(f"弧[id={aid or '?'}] 章节跨度非法：须整数且 1<=start_chapter<=end_chapter"
                             f"（当前 start_chapter={sc!r}, end_chapter={ec!r}）")
        else:
            given = [k for k, v in (("start_word", sw), ("end_word", ew),
                                    ("start_chapter", sc), ("end_chapter", ec))
                     if _payload_int(v) is not None]
            probs.append(f"弧[id={aid or '?'}] 缺完整跨度（当前只有 {given or '无'}）："
                         "须成对传 start_word&end_word（0<=start<end）或 start_chapter&end_chapter"
                         "（1<=start<=end）；不再自动按每章字数换算")
    id_set = {a[0] for a in (new_arcs + known_arcs) if a[0]}
    non_leaf = {a[6] for a in (new_arcs + known_arcs) if a[6] and a[6] in id_set}
    known_plot_fields = [_plot_fields(p) for p in (known_plots or [])]
    seen_plot_ids = {p[0] for p in known_plot_fields if p[0]}
    for i, (pid, _pname, oid) in enumerate(_plot_fields(p) for p in (plots or [])):
        who = f"情节段[{i}]" + (f" id={pid}" if pid else "（未命名）")
        if not pid:
            probs.append(f"{who} 缺 id")
        elif pid in seen_plot_ids:
            probs.append(f"情节段 id={pid} 重复（第 {i} 项与已有情节段 id 冲突）")
        else:
            seen_plot_ids.add(pid)
        if not oid:
            probs.append(f"情节段[id={pid or '?'}] 缺 outline_id（须指向叶弧 id）")
        elif oid not in id_set:
            probs.append(f"情节段[id={pid or '?'}] outline_id={oid} 未指向任何弧")
        elif oid in non_leaf:
            probs.append(f"情节段[id={pid or '?'}] outline_id={oid} 非叶弧（{oid} 含子弧，情节段只能挂最底层弧）")
    return probs


@dataclass
class OutlineSlot:
    """一个大纲（情节弧）在故事线上的位置：树状目标节点，字数跨度（0 基，start 含/end 不含），可多层嵌套（parent_arc_id）；start_chapter/end_chapter 为兼容/推导视图。"""
    id: str                        # 唯一标识
    template_id: str               # 对应 StructureLibrary 里的模板，""=已展开不依赖模板
    name: str                      # 显示名称（如"末日来临前囤物资"）
    start_chapter: int = 1         # 从第几章开始（兼容/推导视图）
    end_chapter: int = 30          # 到第几章
    start_word: Optional[int] = None  # 0-based inclusive 字数，权威；None=由 chapter 推导
    end_word: Optional[int] = None    # exclusive 字数，权威；None=由 chapter 推导
    stages: list = field(default_factory=list)   # 从模板展开的阶段 [{name,min_ch,max_ch,events,description,foreshadow_opportunities,themes}]
    expanded: bool = False         # 是否已展开填充了情节段
    notes: str = ""                # 用户备注

    # 与其他大纲的关系
    overlaps_with: list[str] = field(default_factory=list)  # 与哪些大纲重叠（id 列表）
    predecessor: str = ""          # 前驱大纲 id
    successor: str = ""            # 后继大纲 id
    transition_type: str = "sequential"  # sequential(顺序接续)|overlap(重叠过渡)|merge(融合)
    parent_arc_id: str = ""          # 弧树嵌套：父弧 id，空=顶层弧

    # 叙事手法（故事线严谨性：顺叙/倒叙/插叙）
    narrative: str = "chronological"   # chronological(顺叙)|flashback(倒叙)|interleaved(插叙)
    narrative_target: str = ""         # flashback: 回忆的时间段/章节；interleaved: 所嵌入的主弧 id


@dataclass
class PlotSlot:
    """一个情节段在大纲阶段内的位置"""
    id: str                        # 唯一标识
    template_id: str               # 对应 PlotLibrary 里的模板
    name: str                      # 显示名称
    category: str = ""             # 爽文/开篇/战斗/...
    sub_category: str = ""         # 子分类
    outline_id: str = ""           # 属于哪个大纲
    stage_index: int = 0           # 属于哪个阶段（outline.stages 的索引）
    parent_plot_id: str = ""       # 嵌套：父情节段 id，空=顶级
    children_plot_ids: list[str] = field(default_factory=list)  # 子情节段

    # 位置信息（用于故事线展示）
    order: int = 0                 # 阶段内排序
    cover_beats: int = 4           # 预计覆盖多少个节拍
    words: int | None = None       # 目标字数（agent 按内容浓淡给的规划字数；0/None=回退 cover_beats×200）
    template_structure: str = ""   # 情节段模板结构字符串（箭头流程）
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
    resolves_plot_id: str = ""       # 收局槽位：解决/呼应哪个设局情节段 id（非空=收局）
    resolves_name: str = ""          # 冗余存设局情节段名，供 prompt/前端免查

    # 出场人物（主角恒在；配角按名规则匹配到情节段事件/骨架/槽位）
    roles: list[str] = field(default_factory=list)
    execution_brief: dict = field(default_factory=dict)   # 为什么写这一段（目标/冲突/选择/不可逆变化/钩子；自然语言，供 Agent 阅读）
    character_impact: list[dict] = field(default_factory=list)  # 写前人物变化预测（自然语言，供 Agent 阅读）
    expected_facts: list[dict] = field(default_factory=list)    # 可机器比较的写前预测（reconcile 只比它）：
        # [{subject, type, expected_to, strength: must|likely|possible}]；type 与 character_state 事件白名单对齐


@dataclass
class BookStoryline:
    """整本书的故事线配置 —— 新书启动的核心产出"""
    book_title: str = ""
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
    storyline_revision: int = 0   # 乐观并发版本；每次结构写入成功后 +1

    def to_dict(self) -> dict:
        def _outline_dict(o):
            reconcile_outline(o, self.words_per_chapter or 3000)   # 落盘前同步章/字双坐标
            return {
                "id": o.id, "template_id": o.template_id, "name": o.name,
                "start_chapter": o.start_chapter, "end_chapter": o.end_chapter,
                "start_word": o.start_word, "end_word": o.end_word,
                "stages": o.stages, "expanded": o.expanded, "notes": o.notes,
                "overlaps_with": o.overlaps_with,
                "predecessor": o.predecessor, "successor": o.successor,
                "transition_type": o.transition_type,
                "narrative": o.narrative,
                "narrative_target": o.narrative_target,
                "parent_arc_id": o.parent_arc_id,
            }
        return {
            "book_title": self.book_title,
            "words_per_chapter": self.words_per_chapter,
            "pen_name": self.pen_name,
            "platform": self.platform,
            "basic_info": self.basic_info,
            "outlines": [_outline_dict(o) for o in self.outlines],
            "plots": [{
                "id": p.id, "template_id": p.template_id, "name": p.name,
                "category": p.category, "sub_category": p.sub_category,
                "outline_id": p.outline_id, "stage_index": p.stage_index,
                "parent_plot_id": p.parent_plot_id,
                "children_plot_ids": p.children_plot_ids,
                "order": p.order, "cover_beats": p.cover_beats,
                "words": p.words,
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
                "execution_brief": p.execution_brief,
                "character_impact": p.character_impact,
                "expected_facts": p.expected_facts,
            } for p in self.plots],
            "threads": self.threads,
            "promises": self.promises,
            "themes": self.themes,
            "global_gags": self.global_gags,
            "phase": self.phase,
            "generated_at": self.generated_at,
            "updated_at": self.updated_at,
            "storyline_revision": self.storyline_revision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BookStoryline":
        tl = cls(
            book_title=d.get("book_title", ""),
            words_per_chapter=d.get("words_per_chapter", 3000),
            pen_name=d.get("pen_name", ""),
            platform=d.get("platform", "fanqie"),
            basic_info=normalize_basic_info(d.get("basic_info", {})),
            themes=d.get("themes", []),
            global_gags=d.get("global_gags", []),
            phase=d.get("phase", "config"),
            generated_at=d.get("generated_at", ""),
            updated_at=d.get("updated_at", ""),
            storyline_revision=int(d.get("storyline_revision", 0) or 0),
        )
        tl.outlines = []
        for o in d.get("outlines", []):
            _sc = o.get("start_chapter")
            _ec = o.get("end_chapter")
            _sw = o.get("start_word")
            _ew = o.get("end_word")
            _slot = OutlineSlot(
                id=o.get("id", ""), template_id=o.get("template_id", ""),
                name=o.get("name", ""),
                start_chapter=int(_sc) if _sc is not None else None,
                end_chapter=int(_ec) if _ec is not None else None,
                start_word=int(_sw) if _sw is not None else None,
                end_word=int(_ew) if _ew is not None else None,
                stages=o.get("stages", []),
                expanded=o.get("expanded", False), notes=o.get("notes", ""),
                overlaps_with=o.get("overlaps_with", []),
                predecessor=o.get("predecessor", ""),
                successor=o.get("successor", ""),
                transition_type=o.get("transition_type", "sequential"),
                narrative=o.get("narrative", "chronological"),
                narrative_target=o.get("narrative_target", ""),
                parent_arc_id=o.get("parent_arc_id", ""),
            )
            reconcile_outline(_slot, tl.words_per_chapter or 3000)
            tl.outlines.append(_slot)
        tl.plots = [PlotSlot(
            id=p.get("id", ""), template_id=p.get("template_id", ""),
            name=p.get("name", ""), category=p.get("category", ""),
            sub_category=p.get("sub_category", ""),
            outline_id=p.get("outline_id", ""), stage_index=p.get("stage_index", 0),
            parent_plot_id=p.get("parent_plot_id", ""),
            children_plot_ids=p.get("children_plot_ids", []),
            order=p.get("order", 0), cover_beats=p.get("cover_beats", 4),
            words=p.get("words"),
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
            execution_brief=p.get("execution_brief", {}),
            character_impact=p.get("character_impact", []),
            expected_facts=p.get("expected_facts", []),
        ) for p in d.get("plots", [])]
        tl.threads = d.get("threads", [])
        tl.promises = d.get("promises", [])
        return tl


# ═══════════════════════════════════════════
# 故事线生成器
# ═══════════════════════════════════════════

def structure_to_stages(stage_nodes, words_per_chapter: int = 3000) -> list[dict]:
    """把若干弧节点（ArcNode，平级库通常传选中弧自身一个）展开为 stage dict（name/min_ch/max_ch/events/description/foreshadow_opportunities/themes）——多实现共用防漂移。
    弧库平级后无子弧可再拆，故「选中的整段弧」即作为书弧内的一条阶段展开；模板只表述字数
    （min_words/max_words），此处按每章字数换算成章数（book 侧 stage 兼容视图）。"""
    wpc = max(1, words_per_chapter or 3000)
    return [
        {"name": s.name,
         "min_ch": max(1, s.min_words // wpc),
         "max_ch": max(1, s.max_words // wpc),
         "events": s.key_events[:5],
         "description": getattr(s, "description", ""),
         "foreshadow_opportunities": list(getattr(s, "foreshadow_opportunities", None) or []),
         "themes": []}
        for s in (stage_nodes or [])
    ]


# 内涵→情节段兼容映射（免费规则，替代 theme_lib.compatible_plots）
# 由内置内涵 compatible_plots 反查：情节段模板 id → 可承载内涵名（保留完整名，与 tl.themes 一致）。
# 删除 theme_lib 后此常量是「内涵跟随情节段」的唯一数据源。
# 2026-09-06 段库种子从零重编(id 换新),旧 plot_dating_* 键一并迁移到新功能类种子。
THEME_PLOT_COMPAT = {
    "plot_confront_001": ["公平（Justice）", "身份与伪装（Identity & Disguise）"],
    "plot_reveal_001": ["身份与伪装（Identity & Disguise）", "公平（Justice）"],
    "plot_reveal_003": ["公平（Justice）", "身份与伪装（Identity & Disguise）"],
    "plot_rel_001": ["归属感（Belonging）"],
    "plot_rel_003": ["归属感（Belonging）"],
    "plot_action_003": ["牺牲（Sacrifice）", "成长的代价（Cost of Growth）"],
    "plot_after_001": ["成长的代价（Cost of Growth）", "牺牲（Sacrifice）", "归属感（Belonging）"],
}


def mount_themes_and_hooks(plot: "PlotSlot", storyline_themes: list) -> None:
    """给情节段挂载内涵并标注吸睛点 —— StorylineBuilder/OutlineGenerator 共用，单一实现防漂移。

    内涵来源优先级：
      1) 情节段已从所属阶段继承 theme_moments（阶段级内涵，含位置/手法）→ theme_hints 取其名
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
    """根据题材方向和用户需求，生成大纲故事线 + 情节段配置"""

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
        """规则拼接：按题材方向选 2-3 个大纲，默认顺序接续"""
        if not self.structures:
            return []

        # 题材方向→弧模板序列：按 tags 首词匹配 genre；无匹配则取库前 2 条。
        # （旧 genre_map 硬编码 arc_xuanhuan_01 等 id 已不存在，属死路径；2026-09-06 重编种子后改为现取）
        roots = self.structures.roots()
        by_tag = [t for t in roots if (t.tags or [""])[0] == genre]
        pick = (by_tag or roots)[:2]
        template_ids = [t.id for t in pick]
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
                end_chapter=ch + max(1, tmpl.total_words // 3000) - 1,
                stages=structure_to_stages([tmpl]),
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
            templates = self.structures.roots()[:30]  # 最多 30 个平级弧候选
            available = "\n".join(
                f"- {t.id}: {t.name} ({t.total_words}字) | 简介: {(t.description or '')[:60]}"
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
- 同一题材方向下可以有不同风格的大纲（如开局爽文→中期正剧）
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
                    stages = structure_to_stages([tmpl])
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
        给一个大纲的每个阶段填充情节段。

        支持嵌套：第一个情节段作为"框"，后续情节段嵌入其中。
        """
        if not self.plots:
            return []

        new_plots = []
        for si, stage in enumerate(outline.stages):
            stage_name = stage.get("name", "")
            events = stage.get("events", [])

            # 匹配情节段：阶段名+事件描述+题材方向
            context = f"{outline.name} {stage_name} {' '.join(events)}"
            candidates = self.plots.match_for_chapter(context, genre_from_tags(storyline))
            if not candidates:
                candidates = self.plots.search(category=genre_from_tags(storyline))
                if not candidates:
                    candidates = self.plots.templates[:1]

            # 取 1-3 个情节段（支持嵌套）
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
                    # 找到父情节段并添加子关系
                    for existing in storyline.plots + new_plots:
                        if existing.id == parent_id:
                            existing.children_plot_ids.append(pid)
                            break
                parent_id = pid  # 链式嵌套（每个情节段包下一个）

        outline.expanded = True
        return new_plots

    def fill_themes_and_hooks(self, plots: list[PlotSlot], storyline: BookStoryline):
        """给情节段挂载内涵（跟随情节段）并标注吸睛点（委托共享 mount_themes_and_hooks）。"""
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
    """以 generated 为基础，existing 里非空字段覆盖（dict 递归）。

    age/death_year 的 0 视为"未知"（跳过，保留 generated 真值），否则 0 会覆盖生成值。
    """
    existing = existing or {}
    generated = generated or {}
    merged = dict(generated)
    for k, ev in existing.items():
        gv = merged.get(k)
        if isinstance(ev, dict) and isinstance(gv, dict):
            merged[k] = _deep_keep_existing(ev, gv)
        elif ev not in (None, "", [], {}, 0):
            merged[k] = ev
    return merged


def _merge_characters(existing_chars, generated_chars) -> list:
    """characters 数组合并：
    - MC 逐字段深合并（existing 非空字段保留，generated 补空）
    - 非 MC：按姓名逐字段补全——existing 空字段（性别/简介/称呼/年龄等）由 generated 补，
      generated 里没有 existing 对应角色的追加（不复刻"整组保留/整组丢弃"）
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
    gen_by_name = {str(c.get("name", "") or "").strip(): c
                   for c in gen_nonmc if str(c.get("name", "") or "").strip()}
    for c in ex_nonmc:
        merged.append(_deep_keep_existing(
            c, gen_by_name.get(str(c.get("name", "") or "").strip()) or {}))
    ex_names = {str(c.get("name", "") or "").strip() for c in ex_nonmc}
    for g in gen_nonmc:
        if str(g.get("name", "") or "").strip() not in ex_names:
            merged.append(dict(g))
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

# 分类启发式兜底：情节段模板文本是泛化的，名字规则匹配常落空；
# 按情节段 category 推断该出现的配角类型（凭 role/relation 关键词匹配）
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
    """规则标注每个情节段的出场人物（主角恒在首位；配角名出现在情节段事件/骨架/槽位/吸睛文本 → 出场）。

    幂等：重跑覆盖。返回标注到出场人物的情节段数。
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
        # 2. 分类启发式兜底：名字没命中时，按情节段 category 推断出场配角
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

