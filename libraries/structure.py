"""
情节弧库（Structure Library）
各类网文题材的故事骨架结构模板。

存储模型（2026-09 v3 平级独立）：
  每行 = 一个**平级独立弧模板**（ArcNode），无父子层级、无 parent_arc_id；
  每个弧自带完整内容：字数区间 / 描述(本弧情节怎么发展) / key_events / themes /
  tags(题材) / source / created_at / enabled。原树中的「整段壳」与各层子弧在
  迁移/内置构造时都各自成为独立弧，tags/来源/收录 从原树根平铺到每个独立弧。
  兼容别名 StructureTemplate = ArcNode（旧引用/类型注解可继续用）。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .base_library import JsonLibrary


# ─── 统一的平级弧节点 ───

@dataclass
class ArcNode:
    """一个平级独立情节弧模板。
    只表述字数（min_words/max_words 为该弧建议字数区间，与运行时字数轴一致，不含章数）。
    全库每个弧字段集完全相同；无父-子关联。"""
    id: str                        # 唯一 id（arc_xxx / scout_src_名）
    name: str
    description: str = ""          # 本弧情节怎么发展（可复用内容主体）
    min_words: int = 0
    max_words: int = 0
    key_events: list[str] = field(default_factory=list)
    foreshadow_opportunities: list[str] = field(default_factory=list)  # 埋坑机会
    themes: list = field(default_factory=list)   # 内涵 [{name, position, how}]，含插入位置
    tags: list[str] = field(default_factory=list)              # 题材标签（每弧可搜）
    opening_patterns: list[str] = field(default_factory=list)  # 开篇桥段模板引用
    climax_patterns: list[str] = field(default_factory=list)   # 高潮桥段模板引用
    source: str = ""               # 来源
    created_at: str = ""           # 收录时间
    enabled: bool = True           # 启用状态

    @property
    def total_words(self) -> int:
        """整段字数量（兼容旧模板字段读取；整段跨度弧 min=max=整段）。"""
        return self.max_words or self.min_words or 0

    def to_dict(self) -> dict:
        # 永远输出统一键集（无 parent）：全库字段结构一致
        return {
            "id": self.id, "name": self.name,
            "description": self.description,
            "min_words": self.min_words, "max_words": self.max_words,
            "key_events": self.key_events,
            "foreshadow_opportunities": self.foreshadow_opportunities,
            "themes": self.themes,
            "tags": self.tags,
            "opening_patterns": self.opening_patterns,
            "climax_patterns": self.climax_patterns,
            "source": self.source,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d) -> "ArcNode":
        if isinstance(d, str):
            return cls(id=d, name=d)
        mn, mx = _words_of(d)
        return cls(
            id=str(d.get("id") or d.get("name") or ""),
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            min_words=mn, max_words=mx,
            key_events=list(d.get("key_events") or []),
            foreshadow_opportunities=list(d.get("foreshadow_opportunities") or []),
            themes=list(d.get("themes") or []),
            tags=list(d.get("tags") or []),
            opening_patterns=list(d.get("opening_patterns") or []),
            climax_patterns=list(d.get("climax_patterns") or []),
            source=str(d.get("source") or ""),
            created_at=str(d.get("created_at") or ""),
            enabled=bool(d.get("enabled", True)),
        )


# 兼容别名：旧代码/类型注解（如 libraries/assembler.py）仍可 import StructureTemplate。
StructureTemplate = ArcNode


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _words_of(d: dict) -> tuple:
    """取一个节点 dict 的字数区间 (min,max)，兼容多代字段：
    优先 min_words/max_words；其次整弧 total_words/total_chapters（min=max=整段）；
    最次旧阶段 min_chapters/max_chapters（×3000）。"""
    if "min_words" in d or "max_words" in d:
        return _int(d.get("min_words")), _int(d.get("max_words"))
    whole = d.get("total_words") or d.get("total_chapters")
    if whole:
        v = _int(whole)
        return v, v
    mn, mx = d.get("min_chapters"), d.get("max_chapters")
    if mn or mx:
        return _int(mn) * 3000, max(_int(mn), _int(mx)) * 3000
    return 0, 0


# ─── 平级化 helper（内置构造 / 旧数据迁移） ───

def flatten_nested_tree(tree: dict, root_id: str = "", source: str = "", created_at: str = "") -> list[dict]:
    """把一棵旧嵌套弧树 dict（顶层用 stages，子层用 children，可多层）摊平成
    「带 parent_arc_id 的节点行 dict」列表（父级内 0 基 DFS 链 id）。仅供随后
    independentize_rows 转平级，或旧格式迁移用；最终落盘不含 parent。"""
    root_id = root_id or str(tree.get("id") or "")
    rows: list[dict] = []

    def node_row(node: dict, node_id: str, parent_id: str) -> dict:
        mn, mx = _words_of(node)
        return {
            "id": node_id,
            "name": str(node.get("name") or ""),
            "description": str(node.get("description") or ""),
            "min_words": mn, "max_words": mx,
            "key_events": list(node.get("key_events") or []),
            "foreshadow_opportunities": list(node.get("foreshadow_opportunities") or []),
            "themes": list(node.get("themes") or []),
            "parent_arc_id": parent_id,
            "tags": list(node.get("tags") or []),
            "opening_patterns": list(node.get("opening_patterns") or []),
            "climax_patterns": list(node.get("climax_patterns") or []),
            "source": str(node.get("source") or ""),
            "created_at": str(node.get("created_at") or ""),
            "enabled": bool(node.get("enabled", True)),
        }

    root = dict(tree)
    root["id"] = root_id
    root["parent_arc_id"] = ""
    root.setdefault("key_events", [])
    root.setdefault("themes", [])
    root.setdefault("foreshadow_opportunities", [])
    if source and not root.get("source"):
        root["source"] = source
    if created_at and not root.get("created_at"):
        root["created_at"] = created_at
    rows.append(node_row(root, root_id, ""))

    def walk(parent_row_id: str, children: list) -> None:
        for i, child in enumerate(children):
            cid = f"{parent_row_id}::{i}"
            rows.append(node_row(child, cid, parent_row_id))
            sub = child.get("children") or child.get("stages") or []
            if sub:
                walk(cid, sub)

    top = tree.get("stages") or tree.get("children") or []
    if top:
        walk(root_id, top)
    return rows


def independentize_rows(rows: list[dict]) -> list[dict]:
    """把「带 parent_arc_id 的扁平行」转成**平级独立弧行**：
    ① 每棵树的根 tags/source/created_at 平铺到每个后代（后代原本无 tags）→ 各自可搜；
    ② 每行去掉 parent_arc_id（及可能的 children/stages 残留）。
    返回可直接 ArcNode.from_dict 的统一行。"""
    rows = [dict(r) for r in rows if isinstance(r, dict)]
    if not rows:
        return rows
    id_map = {str(r.get("id")): r for r in rows if r.get("id")}

    def root_of(r: dict) -> dict:
        seen = set()
        pid = str(r.get("parent_arc_id") or "")
        while pid and pid in id_map and pid not in seen:
            seen.add(pid)
            r = id_map[pid]
            pid = str(r.get("parent_arc_id") or "")
        return r

    out = []
    for r in rows:
        root = root_of(r)
        nr = dict(r)
        for k in ("tags", "source", "created_at"):
            if not nr.get(k) and root is not r and root.get(k):
                v = root.get(k)
                nr[k] = list(v) if isinstance(v, list) else v
        nr.pop("parent_arc_id", None)
        nr.pop("children", None)
        nr.pop("stages", None)
        out.append(nr)
    return out


def normalize_structures(items) -> list:
    """入库/评审前的结构载荷归一：弧库 = 平级独立弧，**每个候选即一条独立弧 dict**，
    原样返回（不做任何聚树）。兼容：候选带旧嵌套 stages 时按其顶层节点数摊平为独立弧。"""
    items = list(items or [])
    out = []
    for it in items:
        if isinstance(it, dict) and ("stages" in it or "children" in it) and "parent_arc_id" not in it:
            # 旧嵌套树形态（整棵模板）→ 摊平成平级独立弧（含原根壳 + 各层子节点）
            out.extend(independentize_rows(flatten_nested_tree(it)))
        else:
            out.append(it)
    return out


def make_root_id(name, source: str = "fanqie") -> str:
    """生成 scout 入库弧 id：scout_{source}_{清洗(name)[:40]}。"""
    base = re.sub(r"[^0-9A-Za-z一-鿿\-]", "", str(name or ""))[:40]
    if not base:
        base = "arc"
    return f"scout_{source}_{base}"


# ─── 库管理器 ───

class StructureLibrary(JsonLibrary):
    """情节弧库管理器（进程内单例，JSONL 一行一个**平级独立弧**）。
    旧嵌套/带父子的数据在加载时自动迁移为平级。"""
    _instance = None
    _list_attr = "templates"     # 平级弧列表（每个 = 一条可独立挑选的弧模板）
    _key = "templates"
    _file_name = "structures.jsonl"

    @classmethod
    def _from_dict(cls, d: dict) -> "ArcNode":
        return ArcNode.from_dict(d)

    @classmethod
    def _builtin(cls) -> list:
        return BUILTIN_STRUCTURES

    def _load_jsonl(self):
        """优先读 .jsonl；同名旧单 JSON（.json）自动迁移；v1 嵌套树 / v2 带 parent 的
        数据在此统一迁移为平级独立弧行并落盘。"""
        from core.json_store import read_json, read_jsonl
        save = Path(str(self._save_path))
        legacy = save.with_suffix(".json")
        raw = None
        if not save.exists() and legacy.exists():
            data = read_json(legacy, {})
            raw = data.get(self._key, [])
        elif save.exists():
            raw = read_jsonl(save)
        if raw is None:
            out = list(self._builtin())
            setattr(self, self._list_attr, out)
            return

        migrated = False
        flat: list = []
        for d in raw:
            if isinstance(d, dict) and ("stages" in d or "children" in d) and "parent_arc_id" not in d:
                # v1：一整棵嵌套模板 → 摊平成带 parent 的行（稍后转平级）
                flat.extend(flatten_nested_tree(d))
                migrated = True
            else:
                flat.append(d)
        # v2：扁平行带 parent_arc_id → 平级化（tags 平铺 + 去 parent）
        if any(isinstance(d, dict) and d.get("parent_arc_id") for d in flat):
            flat = independentize_rows(flat)
            migrated = True
        elif any(isinstance(d, dict) and "parent_arc_id" in d for d in flat):
            # 带空 parent 键的旧行：只去掉键
            flat = independentize_rows(flat)
            migrated = True
        out = [ArcNode.from_dict(d) for d in flat if isinstance(d, dict)]
        setattr(self, self._list_attr, out)
        if migrated:
            self._save()

    # ── 查询（无层级：每个弧都是独立模板） ──
    def roots(self, include_disabled: bool = True) -> list:
        """全部弧（无层级 = 全库即候选模板清单）；默认含已禁用（与原全量语义一致）。"""
        return [t for t in self.templates if include_disabled or t.enabled]

    def get_by_id(self, node_id: str):
        for t in self.templates:
            if t.id == node_id:
                return t
        return None

    def get_node(self, node_id: str):
        return self.get_by_id(node_id)

    def children_of(self, parent_id: str) -> list:
        """无父子层级：恒空（兼容旧调用点）。"""
        return []

    def root_of(self, node_id: str):
        return self.get_by_id(node_id)

    def descendant_ids(self, node_id: str) -> set:
        return {node_id} if self.get_by_id(node_id) else set()

    def subtree_dicts(self, node_id: str) -> dict:
        node = self.get_by_id(node_id)
        return node.to_dict() if node else {}

    def display_trees(self, include_disabled: bool = True) -> list:
        """全弧浅拷列表（无 children；兼容旧展示调用点）。"""
        import copy
        return [copy.copy(t) for t in self.templates
                if include_disabled or t.enabled]

    def delete_tree(self, node_id: str) -> int:
        """删除单个弧（无层级，无连坐）。返回删除行数。"""
        ids = self.descendant_ids(node_id)
        before = len(self.templates)
        self.templates = [t for t in self.templates if t.id not in ids]
        self._save()
        return before - len(self.templates)

    def search(self, tags=None, word_count: int = 0) -> list:
        """按标签（任一命中）/整段字数筛选弧模板。tags 为列表或逗号/空格分隔字符串。"""
        results = self.roots(include_disabled=True)
        if isinstance(tags, str):
            tags = [x.strip() for x in tags.replace("，", " ").replace(",", " ").split() if x.strip()]
        if tags:
            tag_set = {str(t).strip() for t in tags if str(t).strip()}
            results = [t for t in results if tag_set.intersection(t.tags or [])]
            results.sort(key=lambda t: -len(tag_set.intersection(t.tags or [])))  # 命中多的排前
        if word_count:
            results.sort(key=lambda t: abs(t.total_words - word_count))
        return results


# ─── 内置情节弧（2026-09 精选库：平级独立弧，真实 min-max 区间，无整段壳单点）───

_CURATED_ARCS = [
    {"id": "arc_talent_fall_reverse", "name": "天才坠落·试炼逆袭",
     "description": "被废天赋的主角在入门试炼里反杀同门天才、重获宗门重视的整段弧：当众受辱→暗中蓄力→试炼场越级反杀→获长辈赏识并埋下更强敌意。",
     "min_words": 18000, "max_words": 40000,
     "key_events": ["废体诊断/当众贬低", "隐匿底牌暗中蓄力", "试炼开启后越级反杀", "长老关注与新的威胁"],
     "foreshadow_opportunities": ["天赋被废的真相是被人夺走"],
     "themes": [{"name": "成长的代价（Cost of Growth）", "position": "结尾",
                 "how": "用代价换回的力量在反杀瞬间引爆"}],
     "tags": ["玄幻", "升级", "宗门", "爽文"]},
    {"id": "arc_sect_competition", "name": "宗门大比·扬名立万",
     "description": "一场宗门大比从低调入场到力压群雄的弧：报名分组暗藏猫腻→种子对手屡屡挑衅→大比夺魁扬名→受邀进入更高层圈层。",
     "min_words": 16000, "max_words": 36000,
     "key_events": ["大比报名/分组遇刁难", "种子选手挑衅", "一路碾压晋级", "夺魁并受各方拉拢"],
     "foreshadow_opportunities": ["大比赛制背后有人操纵"],
     "themes": [],
     "tags": ["玄幻", "宗门", "扬名", "爽文"]},
    {"id": "arc_son_in_law_rise", "name": "赘婿翻身·身份曝光",
     "description": "被家族看轻的赘婿在关键场合被迫出手、隐藏身份曝光引发震动的弧：当众受辱→迫于局势出手→身份揭示全场哗然→前倨后恭与更大靠山浮现。",
     "min_words": 16000, "max_words": 36000,
     "key_events": ["当众受辱/家族轻视", "被迫出手解围", "隐藏身份曝光", "态度反转与更大图谋"],
     "foreshadow_opportunities": ["她背后的那位才是真正的幕后推手"],
     "themes": [],
     "tags": ["都市", "逆袭", "赘婿", "爽文"]},
    {"id": "arc_shop_warm_rise", "name": "金手指小店·温馨逆袭",
     "description": "盘下落魄小店后凭特殊能力慢慢把生意做火、被整条街认可的轻快弧：接手烂摊→能力初显引来客→与对头几番小冲突→小店成了全城网红。",
     "min_words": 12000, "max_words": 28000,
     "key_events": ["盘下落魄小店", "金手指初显招客", "同行使绊化解", "小店爆红/温馨收尾"],
     "foreshadow_opportunities": [],
     "themes": [],
     "tags": ["都市", "经营", "轻松", "日常"]},
    {"id": "arc_medical_heir_daface", "name": "医武双绝·当众打脸",
     "description": "低调神医传人卷入豪门恩怨、在众目睽睽下以医术碾压对手扬名的弧：被轻视刁难→医案上先声夺人→当面拆穿对手→声名鹊起与仇家上门。",
     "min_words": 15000, "max_words": 34000,
     "key_events": ["被看轻/刁难", "首例疑难症一鸣惊人", "当众揭穿对头", "声名鹊起引出更强对手"],
     "foreshadow_opportunities": ["曾救过的高人将成为援手"],
     "themes": [],
     "tags": ["都市", "神医", "打脸", "爽文"]},
    {"id": "arc_lockedroom_truth", "name": "连环密室·设局揭晓",
     "description": "数起密室命案从无从下手到逐步设局诱凶、真相反转的悬疑弧：现场疑点→排查被误导→设局引蛇出洞→当众揭穿真凶与动机。",
     "min_words": 22000, "max_words": 50000,
     "key_events": ["首案现场/密室疑点", "证人证词冲突", "设局诱出真凶", "真相反转收束"],
     "foreshadow_opportunities": ["死者之间被掩藏的旧事"],
     "themes": [{"name": "公平（Justice）", "position": "结尾",
                 "how": "揭晓时以被掩盖的旧冤收束，点题迟到的公平"}],
     "tags": ["悬疑", "推理", "连环", "反转"]},
    {"id": "arc_bodydouble_truth", "name": "替身入局·真凶另有其人",
     "description": "顶罪替身发现自己被卷进更大的局、一步步反查真凶的弧：被安排顶罪→怀疑有诈→暗中调查找出疑点→身份/真凶双重反转。",
     "min_words": 18000, "max_words": 40000,
     "key_events": ["被安排顶罪", "发现证人破绽", "反查幕后", "真凶落网/替身脱罪"],
     "foreshadow_opportunities": ["安排替身的人其实在保护他"],
     "themes": [],
     "tags": ["悬疑", "反转", "替身"]},
    {"id": "arc_apocalypse_stockpile", "name": "末世囤货·抢跑求生",
     "description": "灾变将至时抢先囤积物资、据守求生的弧：预知灾变抢购物资→占点拉小队→首波危机兑现→为秩序重建前的黑暗做准备。",
     "min_words": 14000, "max_words": 32000,
     "key_events": ["预知灾变/抢购", "占据据点点与结盟", "首波灾变兑现", "活下去的下一步布局"],
     "foreshadow_opportunities": ["幸存者里混进了不该出现的人"],
     "themes": [],
     "tags": ["科幻", "末世", "求生", "囤货"]},
    {"id": "arc_starfield_relic", "name": "星际遗迹·夺宝大逃杀",
     "description": "在刚开启的远古遗迹里多方势力夺宝、主角杀出重围的弧：遗迹开启各方入场→队伍内讧与外敌夹击→核心宝藏前混战→带走关键宝物并埋下更大秘密。",
     "min_words": 20000, "max_words": 46000,
     "key_events": ["遗迹开启/势力入场", "结盟与背叛", "宝藏核心混战", "夺宝突围/发现更大秘密"],
     "foreshadow_opportunities": ["遗迹背后是一个文明的警告"],
     "themes": [],
     "tags": ["科幻", "星际", "冒险", "爽文"]},
    {"id": "arc_reborn_business", "name": "重生商战·夺回一切",
     "description": "重生回过去的商界主角抢回被夺走的一切的弧：回到关键节点→避开上一世的坑布局→对手察觉反击→决战夺回公司/地位。",
     "min_words": 18000, "max_words": 42000,
     "key_events": ["重生回关键节点", "提前布局/夺资源", "宿敌反扑", "决战清算"],
     "foreshadow_opportunities": ["上一世背叛者另有隐情"],
     "themes": [{"name": "复仇（Revenge）", "position": "结尾",
                 "how": "夺回一切的高光以当年所受之辱的报复引爆"}],
     "tags": ["穿越", "重生", "商战", "爽文"]},
    {"id": "arc_infinite_rules", "name": "无限流·规则副本求生",
     "description": "进入规则类死亡副本、靠推演规则极限求生的弧：入场摸清规则→同伴试探/互坑→发现隐藏规则→极限通关并被更大的局盯上。",
     "min_words": 24000, "max_words": 56000,
     "key_events": ["进入副本/规则浮现", "试探规则与伤亡", "识破隐藏规则", "卡点通关/被更高存在注视"],
     "foreshadow_opportunities": ["循环玩家身份不简单"],
     "themes": [],
     "tags": ["无限流", "副本", "求生", "爽文"]},
    {"id": "arc_fake_lovers_real", "name": "欢喜冤家·假扮成真",
     "description": "被安排假扮情侣的两人在鸡飞狗跳的相处里假戏真做的甜弧：被迫组队假扮→日常互怼出默契→真感情被戳破→确认关系。",
     "min_words": 10000, "max_words": 26000,
     "key_events": ["被迫假扮情侣", "共同应付/互怼升温", "暧昧被亲友点破", "坦白并在一起"],
     "foreshadow_opportunities": [],
     "themes": [],
     "tags": ["言情", "甜文", "日常", "欢喜冤家"]},
    {"id": "arc_standin_reconcile", "name": "替身白月光·误会方知情深",
     "description": "被当作替身的恋人从心灰意冷到真相揭晓、破镜重圆的虐甜弧：发现自己是替身→冷言分手→对方追悔/真相揭开→和解确认心意。",
     "min_words": 18000, "max_words": 42000,
     "key_events": ["替身真相被戳破", "心灰意冷分开", "对方追悔/真相大白", "和解与重新开始"],
     "foreshadow_opportunities": ["她与白月光本是同一人"],
     "themes": [],
     "tags": ["言情", "误会", "虐恋", "和解"]},
    {"id": "arc_pill_ascension", "name": "丹道废柴·逆炼成神",
     "description": "炼丹废柴靠独特的异火/丹方一路逆袭、被全城求丹的轻松爽弧：被逐出丹房→尝试旁门丹方一鸣惊人→被大势力招揽与暗算→丹术大成扬名。",
     "min_words": 14000, "max_words": 32000,
     "key_events": ["炼丹废柴被逐", "旁门丹方首成", "丹成引轰动与觊觎", "危机中突破/扬名"],
     "foreshadow_opportunities": ["异火来历牵连更大宗门之争"],
     "themes": [],
     "tags": ["玄幻", "炼丹", "轻松", "爽文"]},
    {"id": "arc_haunted_truth", "name": "鬼宅灵异·夜探真相",
     "description": "接手闹鬼宅邸调查、发现所谓灵异其实是人祸的惊悚反转弧：入住鬼宅遇怪→线索指向旧案→夜探发现地下密室→真凶现身真相大白。",
     "min_words": 20000, "max_words": 44000,
     "key_events": ["入住鬼宅/异象频发", "旧案线索拼图", "夜探密室", "真凶现身/人祸反转"],
     "foreshadow_opportunities": ["老宅地契里藏着的第二份遗嘱"],
     "themes": [],
     "tags": ["悬疑", "灵异", "探案", "反转"]},
    {"id": "arc_revenge_return", "name": "复仇归来·旧怨清算",
     "description": "被挚友背叛/家道中落后的主角蛰伏归来、当众清算旧怨的弧：被背叛跌入谷底→暗中蛰伏攒牌→重回旧日圈子试探→在公开场合清算背叛者。",
     "min_words": 16000, "max_words": 36000,
     "key_events": ["被背叛/谷底", "蛰伏积蓄底牌", "重回旧日圈子", "当众清算/旧怨收束"],
     "foreshadow_opportunities": ["背叛者背后还有指使"],
     "themes": [{"name": "复仇（Revenge）", "position": "结尾",
                 "how": "多年隐忍在清算旧怨一刻集中爆发"}],
     "tags": ["都市", "复仇", "爽文", "权谋"]},
]

BUILTIN_STRUCTURES = [ArcNode.from_dict(x) for x in _CURATED_ARCS]
