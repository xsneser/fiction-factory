# -*- coding: utf-8 -*-
"""全局「样文库」—— STYLE REFERENCE 人工样本(词条)的结构化管理与注入。

样文从「扁平 <sample> 文本文件 / 每笔名 samples.json」升为**全局一份带元数据的词条库**,
解决:词条可按场景分类浏览/添加、各笔名写作时共享同一 STYLE REFERENCE(用户 2026-09-06 拍板)。

存储(全部 gitignored,版权样本不入库):
  - storage/style_samples/samples.json : 权威源,词条数组(每条 id/title/scene_tags/
    source/word_count/note/text/no_warn),UI 与 MCP 工具读写它。
  - storage/style_samples/reference.txt : **镜像**,由权威源再生成(# STYLE REFERENCE
    标题 + §6 引导语 + 每条前 `# 场景:…` + <sample> 块),供人读/注入兜底。
  - 旧 storage/style_refs/<笔名>.samples.json 保留作迁移参照,不再是读写源。

字数口径(易混,务必区分):
  - word_count(展示/落库)= count_prose_units(中文字 + 英文词),对齐平台字数语义;
  - 预算 max_chars = len(text)(Python 字符数),对齐 dsh tool-result-pruner 的
    thresholdChars=8192(字符)口径。

选择策略(select_for_budget):保序 + **跨场景标签多样化优先** —— 预算内尽量每种
scene_tag 先取最早 1 条,再按原序补足;宁可少塞不超限;保证至少返回首条(不空)。
pruner 默认已关 → 预算 0 = 全量注入全部词条。
"""
import os
import random
import re
from dataclasses import dataclass, field, asdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB = os.path.join(_ROOT, "storage", "style_samples")   # 全局样文库(权威源 + 镜像)
_LEGACY_REFS = os.path.join(_ROOT, "storage", "style_refs")  # 旧每笔名文件(迁移/回滚参照)

_SAMPLE_RE = re.compile(r"<sample>(.*?)</sample>", re.S)
_WS_RE = re.compile(r"\s+")

# dsh tool-result-pruner 阈值(headless profile 实际生效源 = dsh-base bundle:
# vendor/dsh-ne/node_modules/@deepseek-ai/dsh-base/cordis.patch.yml thresholdChars:
# 8192,字符)。dsh_bridge overlay 默认把 pruner disabled;设 NE_KEEP_TOOL_PRUNE=1 保留。
DSH_RESULT_THRESHOLD_CHARS = 8192
# style_rules 里 md/引导语/字段杂项之外留给样文的保底;md 越大留给样文越少。
_SAFETY_CHARS = 512
# pruner 关闭时的**多样封顶**(库已 10 类×≥3 ≈ 25 条,不能每章全量灌几十 k 字符);
# 未传 query 时用 select_for_budget 按此上限选跨场景多样子集。
DEFAULT_REF_CHARS = 12000

# ─── 多维权表(英文枚举键存储,UI/渲染中文展示)─────────────────────────────
# scene / narrative_action 可多选(list);其余单选(str)。词条缺某维 = 通配(选择器不过滤、不加分)。
DIM_CHOICES = {
    "scene": ["opening", "exploration", "investigation", "dialogue", "confrontation",
              "negotiation", "discovery", "revelation", "action", "danger", "death",
              "aftermath", "transition", "quiet", "planning"],
    "dramatic_state": ["calm", "uneasy", "tense", "escalating", "crisis", "aftermath", "relief"],
    "narrative_action": ["establish", "introduce", "investigate", "deduce", "mislead",
                         "reveal", "escalate", "obstruct", "resolve", "transition",
                         "foreshadow", "pay_off", "relationship", "decision", "consequence"],
    "cast": ["solo", "protagonist_duo", "duo", "small_group", "large_group", "crowd"],
    "dialogue_density": ["none", "low", "medium", "high"],
    "information_density": ["low", "medium", "high"],
    "pace": ["slow", "medium", "fast"],
    "pov": ["close_character", "medium_character", "external", "omniscient"],
}
# 维度字段中文名(UI 标题用)
DIM_FIELD_ZH = {
    "scene": "场景", "dramatic_state": "戏剧状态", "narrative_action": "叙事动作",
    "cast": "人物组织", "dialogue_density": "对话密度", "information_density": "信息密度",
    "pace": "节奏", "pov": "视角",
}
# 取值 → 中文(单选/多选共用)
DIM_LABELS = {
    "scene": {"opening": "场景开场", "exploration": "探索·调查", "investigation": "推理·查证",
              "dialogue": "多人对话", "confrontation": "冲突·对峙", "negotiation": "谈判·交涉",
              "discovery": "发现信息", "revelation": "真相·揭示", "action": "行动·战斗·追逐",
              "danger": "危险逼近", "death": "死亡·重伤", "aftermath": "事件余波",
              "transition": "场景过渡", "quiet": "平静·日常", "planning": "谋划·部署"},
    "dramatic_state": {"calm": "平静", "uneasy": "不安", "tense": "紧绷", "escalating": "升级",
                       "crisis": "危机", "aftermath": "余波", "relief": "释然"},
    "narrative_action": {"establish": "建立", "introduce": "引入", "investigate": "获取信息",
                         "deduce": "得出判断", "mislead": "制造误判", "reveal": "揭示信息",
                         "escalate": "提高危险", "obstruct": "设置阻碍", "resolve": "解决问题",
                         "transition": "状态过渡", "foreshadow": "埋设伏笔", "pay_off": "回收伏笔",
                         "relationship": "推进关系", "decision": "做出决定", "consequence": "展示后果"},
    "cast": {"solo": "单人", "protagonist_duo": "主角+一人", "duo": "两人",
             "small_group": "3-4人", "large_group": "5人以上", "crowd": "群体"},
    "dialogue_density": {"none": "无", "low": "低", "medium": "中", "high": "高"},
    "information_density": {"low": "低", "medium": "中", "high": "高"},
    "pace": {"slow": "慢", "medium": "中", "fast": "快"},
    "pov": {"close_character": "近焦角色", "medium_character": "中焦", "external": "外景/旁观", "omniscient": "全知"},
}


def _clean_dim(field, value):
    """校验单值;非法/空 → ''。"""
    return value if value in DIM_CHOICES.get(field, []) else ""


def _clean_dim_list(field, values):
    return [v for v in (values or []) if v in DIM_CHOICES.get(field, [])]


def dim_label(field, value) -> str:
    return DIM_LABELS.get(field, {}).get(value, value)


def tool_prune_enabled() -> bool:
    """dsh tool-result-pruner 是否保留(默认关→样文可全量注入;设 NE_KEEP_TOOL_PRUNE=1 保留)。"""
    return bool(os.environ.get("NE_KEEP_TOOL_PRUNE"))


def samples_path(pen_name: str = "") -> str:
    """全局样文库权威源路径(不分笔名;pen_name 仅兼容占位)。"""
    return os.path.join(_LIB, "samples.json")


def reference_path(pen_name: str = "") -> str:
    """全局样文库镜像路径(不分笔名)。"""
    return os.path.join(_LIB, "reference.txt")


def legacy_pen_path(pen_name: str) -> tuple:
    """旧每笔名文件路径(迁移/回滚参照用):(samples.json, reference.txt)。"""
    return (os.path.join(_LEGACY_REFS, f"{pen_name}.samples.json"),
            os.path.join(_LEGACY_REFS, f"{pen_name}.reference.txt"))


def count_text_chars(text: str) -> int:
    """预算用字符数(len),与 dsh thresholdChars 同口径。"""
    return len(text or "")


def count_word_units(text: str) -> int:
    """展示/落库用字数(中文字 + 英文词),平台口径 count_prose_units。"""
    from core.text_utils import count_prose_units
    try:
        return int(count_prose_units(text or ""))
    except Exception:
        return len(text or "")


def parse_blocks(content: str):
    """把样文文本拆成块:<sample>…</sample> 各自成条;无标签则整文件当一条。

    从 ui/web_blueprints/libraries.py 迁入,收敛为唯一解析实现。
    """
    if not content:
        return []
    blocks = _SAMPLE_RE.findall(content)
    if blocks:
        return [b.strip() for b in blocks if b.strip()]
    return [content.strip()] if content.strip() else []


def _clean_dims(raw):
    """清洗多维权表 dict:只留合法字段/合法值;空值丢弃。scene/narrative_action 为 list,其余单值。"""
    raw = raw or {}
    out = {}
    for f, choices in DIM_CHOICES.items():
        if f in ("scene", "narrative_action"):
            v = _clean_dim_list(f, raw.get(f))
        else:
            v = _clean_dim(f, raw.get(f))
        if v:
            out[f] = v
    return out


def _scene_dims(s):
    """返回样本的 scene 维度枚举值(scene_tags 过渡期也折算进来,仅当 dims 没有 scene 时)。"""
    dims = getattr(s, "dims", None) or {}
    scene = dims.get("scene") or []
    if not scene:
        # legacy:旧中文 scene_tags → 折算到英文 scene(用样式注释映射由迁移脚本做;这里仅直取已有 scene_tags 的英文兜底)
        scene = [t for t in (getattr(s, "scene_tags", None) or []) if t in DIM_CHOICES["scene"]]
    return scene


@dataclass
class StyleSample:
    id: str
    text: str
    title: str = ""
    scene_tags: list = field(default_factory=list)   # 过渡期保留;迁移后弃用,由 dims.scene 取代
    source: str = ""
    note: str = ""
    word_count: int = 0
    # 人工确认保留:该条短(<800)或与其它条整段重叠也接受 → 保存不弹对应软预警
    no_warn: bool = False
    # 多维权表(英文枚举键,缺=通配):scene[]/dramatic_state/narrative_action[]/cast/
    # dialogue_density/information_density/pace/pov
    dims: dict = field(default_factory=dict)

    def __post_init__(self):
        self.scene_tags = [t for t in (self.scene_tags or []) if (t or "").strip()]
        self.dims = _clean_dims(self.dims)
        self.id = (self.id or "").strip()
        self.text = (self.text or "").strip()
        if not self.word_count:
            self.word_count = count_word_units(self.text)

    def to_dict(self):
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            id=str(d.get("id") or "").strip(),
            text=str(d.get("text") or ""),
            title=str(d.get("title") or "").strip(),
            scene_tags=[str(t).strip() for t in (d.get("scene_tags") or []) if str(t).strip()],
            source=str(d.get("source") or "").strip(),
            note=str(d.get("note") or "").strip(),
            word_count=int(d.get("word_count") or 0),
            no_warn=bool(d.get("no_warn") or False),
            dims=_clean_dims(d.get("dims")),
        )


# ─── 读写 ───

def load_samples(pen_name: str = ""):
    """读全局样文库词条;无文件/损坏 → None。pen_name 仅兼容占位(不分笔名)。"""
    path = samples_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json_load(fh.read())
        return [StyleSample.from_dict(x) for x in (data or {}).get("samples", [])] or []
    except Exception:
        return None


def json_load(text):
    import json
    return json.loads(text)


def _next_id(existing_ids):
    n = 1
    while f"s{n}" in existing_ids:
        n += 1
    return f"s{n}"


def save_samples(pen_name: str = "", samples: list = None) -> dict:
    """把词条列表整体落**全局**权威源(缺 id 自动补),并再生 reference.txt 镜像。

    返回 {ok, saved, path}。records 里元素可为 StyleSample / dict。pen_name 仅兼容占位。
    """
    samples = samples or []
    normalized = []
    existing = set()
    for x in samples:
        s = x if isinstance(x, StyleSample) else StyleSample.from_dict(x)
        if not s.text:
            continue
        if not s.id:
            s.id = _next_id(existing)
        elif s.id in existing:
            s.id = _next_id(existing)  # 同文件重 id → 重排,避免覆盖丢数据
        existing.add(s.id)
        s.word_count = count_word_units(s.text)
        normalized.append(s)

    os.makedirs(_LIB, exist_ok=True)
    path = samples_path()
    payload = {
        "version": 2,
        "scope": "global",
        "samples": [s.to_dict() for s in normalized],
    }
    with open(path, "w", encoding="utf-8", newline="") as fh:
        import json
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    # 镜像 reference.txt = 全量渲染(注入时才按预算选,镜像保留完整给人读/兜底)
    ref = render_reference(normalized)
    with open(reference_path(), "w", encoding="utf-8", newline="") as fh:
        fh.write(ref)
    return {"ok": True, "saved": len(normalized), "path": path}


# ─── 渲染 / 选择 ───

_REFERENCE_GUIDE = (
    "以下文本是本作品实际的人工作品样本。\n"
    "不要总结样本。\n"
    "不要解释样本为什么这样写。\n"
    "不要抽取固定技巧。\n"
    "不要建立固定句式模板。\n"
    "不要复制具体句子、人物和事件。\n"
    "直接参考其自然呈现出的语言习惯、叙事距离、信息组织、人物表达和节奏变化。\n"
    "在剧情独立的前提下,继续采用相近的写作惯性。"
)


def render_reference(samples: list) -> str:
    """渲染 STYLE REFERENCE 文本:标题 + §6 引导语 + 每条样文(前带 `# 场景:…` 标签行)。

    标签行在 `<sample>` 块**外**(解析正文不受影响),只让模型知道这段对应什么场景,
    写对应场景时可就近参考。无 scene_tags 的条目不产标签行。
    """
    if not samples:
        return ""
    blocks = []
    for s in samples:
        tags = _scene_dims(s)
        head = ("# 场景:" + " · ".join(dim_label("scene", v) for v in tags) + "\n") if tags else ""
        blocks.append("{0}<sample>\n{1}\n</sample>".format(head, s.text))
    return "# STYLE REFERENCE\n\n{0}\n\n{1}".format(_REFERENCE_GUIDE, "\n\n".join(blocks))


def select_for_budget(samples: list, max_chars):
    """预算内选样:保序 + 跨场景标签多样化优先。

    - 先按 scene_tag 首次出现次序,各取最早 1 条(多样性打底);
    - 再按原序补足到预算上限(len(text) 计);
    - 单条再长也保证返回首条(宁超限不空);返回 (selected, summary)。
    """
    if not samples:
        return [], {"count": 0, "total": 0, "chars": 0, "selected": []}
    if not max_chars or max_chars <= 0:  # 无限额 → 全量
        chars = sum(count_text_chars(s.text) for s in samples)
        return samples, {"count": len(samples), "total": len(samples),
                         "chars": chars, "selected": [s.id for s in samples]}

    tag_first = {}
    for i, s in enumerate(samples):
        tags = _scene_dims(s) or [""]
        for t in tags:
            if t not in tag_first:
                tag_first[t] = i
    first_ids = sorted({tag_first[t] for t in tag_first})
    order = list(first_ids) + [i for i in range(len(samples)) if i not in first_ids]

    chosen = []
    total = 0
    for i in order:
        s = samples[i]
        c = count_text_chars(s.text)
        if not chosen:
            chosen.append(s)  # 首条保底
            total = c
            continue
        if total + c <= max_chars:
            chosen.append(s)
            total += c
        else:
            break  # 预算切断,后续更大/同序不放(确定性、留余量)
    return chosen, {"count": len(chosen), "total": len(samples),
                    "chars": total, "selected": [s.id for s in chosen]}


# 加权随机选择器:维度权重(用户设计)——scene/narrative_action 多选按命中数计,其余单选命中计权。
SEL_WEIGHTS = {
    "scene": 5, "dramatic_state": 3, "narrative_action": 3, "cast": 2,
    "dialogue_density": 2, "information_density": 1, "pace": 1, "pov": 1,
}
_MULTI_DIMS = ("scene", "narrative_action")


def _want_list(value):
    if value is None or value == "":
        return []
    return value if isinstance(value, (list, tuple, set)) else [value]


def pick_samples(samples, query=None, k=3, avoid=None, rng=None, temperature=1.0):
    """加权随机取 k 条样文(硬过滤 → 软加权 → 加权随机 → 近期避重)。

    - query: {字段: 值|列表},只认 SEL_WEIGHTS 里的维;样本缺某维 = 通配(不过滤也不加分)。
    - 硬过滤:query 声明、样本已填且不匹配 → 排除。
    - temperature(命中窗放宽):精确命中为空时,允许沿**最低权重声明维**逐级丢弃最多
      int(temperature) 个再试(0=不放开)——让候选留在「同场景附近」(scene 权重最高,最后才丢),
      而非命中集外无差别随机;全放开仍空 → 才走整池兜底(_fallback_pick)。
    - 软加权:命中权重求和;avoid 里的样本 ×0.5(软避重,不绝对禁止)。
    - rng 可注入(random.Random(seed))便于测试复现。
    返回 (picked:list, meta)。"""
    rng = rng or random
    query = query or {}
    avoid = set(avoid or [])
    declared = [(f, w) for f, w in SEL_WEIGHTS.items() if _want_list(query.get(f))]

    def _score_with(kept_dims):
        out = []
        for s in samples:
            ok, score = True, 0
            for f, w in kept_dims:
                want = _want_list(query.get(f))
                have = (s.dims or {}).get(f)
                if have is None or have == "" or have == []:
                    continue  # 样本缺维 → 通配
                if f in _MULTI_DIMS:
                    inter = len(set(have) & set(want))
                    if inter == 0:
                        ok = False
                        break
                    score += w * inter
                else:
                    if have in want:
                        score += w
                    else:
                        ok = False
                        break
            if ok:
                out.append((s, score))
        return out

    scored = _score_with(declared)
    relaxed = []
    if not scored and declared and len(declared) > 1 and int(max(0, temperature or 0)) >= 1:
        order = sorted(range(len(declared)), key=lambda i: declared[i][1])  # 低→高权重下标
        for d in range(1, min(int(max(0, temperature)), len(declared) - 1) + 1):
            kept = [declared[i] for i in order[d:]]  # 丢最低 d 个,保高权重维
            if not kept:
                break
            hit = _score_with(kept)
            if hit:
                scored = hit
                relaxed = [declared[i][0] for i in order[:d]]
                break
    if not scored:
        return _fallback_pick(samples, k, avoid, rng)

    # 加权随机轮盘,不放回;avoid 里近期用过的 ×0.5(软避重,不绝对禁止)
    work = []
    for s, sc in scored:
        w = sc + 0.5
        if s.id in avoid:
            w *= 0.5
        work.append((s, w))
    picked = []
    for _ in range(min(k, len(work))):
        total = sum(w for _, w in work)
        r = rng.uniform(0.0, total)
        acc, chosen = 0.0, None
        for s, w in work:
            acc += w
            if r <= acc:
                chosen = s
                break
        if chosen is None:
            chosen = work[-1][0]
        picked.append(chosen)
        work = [(s, w) for s, w in work if s.id != chosen.id]
    meta = {"mode": "pick", "query": {f: query[f] for f in SEL_WEIGHTS if query.get(f) is not None},
            "count": len(picked), "total": len(samples),
            "chars": sum(count_text_chars(s.text) for s in picked),
            "selected": [s.id for s in picked], "fallback": False,
            "relaxed_dims": relaxed}
    return picked, meta


def _fallback_pick(samples, k, avoid, rng):
    pool = list(samples)
    picked = []
    for _ in range(min(k, len(pool))):
        if not pool:
            break
        ws = [0.5 if s.id in avoid else 1.0 for s in pool]
        s = rng.choices(pool, weights=ws, k=1)[0]
        picked.append(s)
        pool = [x for x in pool if x.id != s.id]
    meta = {"mode": "pick", "query": {}, "count": len(picked), "total": len(samples),
            "chars": sum(count_text_chars(s.text) for s in picked),
            "selected": [s.id for s in picked], "fallback": True}
    return picked, meta


# ─── 近期避重历史(按笔名持久化,跨 dsh 任务/会话生效) ───

def _pick_history_path(profile_id):
    return os.path.join(_LIB, "pick_history", f"{profile_id}.json")


def load_pick_history(profile_id: str) -> list:
    """读某笔名近期注入过的样文 id(旧→新);无文件/异常 → []。"""
    if not profile_id:
        return []
    p = _pick_history_path(profile_id)
    if not os.path.exists(p):
        return []
    try:
        from core.json_store import read_json
        data = read_json(p) or {}
        return [str(x) for x in (data.get("entries") or [])]
    except Exception:
        return []


def record_pick_history(profile_id: str, sample_ids: list, max_hist: int = 10) -> None:
    """把本次注入的样文 id 记入该笔名近期历史(去重置尾),跨任务避重;失败静默降级。"""
    if not profile_id:
        return
    cur = load_pick_history(profile_id)
    for sid in (sample_ids or []):
        if sid in cur:
            cur.remove(sid)
        cur.append(sid)
    cur = cur[-max_hist:]
    try:
        p = _pick_history_path(profile_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        from core.json_store import process_file_lock, write_json_atomic
        with process_file_lock(p, timeout=1.0):
            write_json_atomic(p, {"profile_id": profile_id, "entries": cur})
    except Exception:
        pass  # 抢锁/写失败 → 本次不持久化,不阻断注入


def estimate_budget_for(style_md_text, guide_chars=None):
    """估算 style_rules 里留给样文的预算。

    - 默认(dsh pruner 已关):返回 DEFAULT_REF_CHARS 多样封顶(库 ~25 条不再全量);
    - 设 NE_KEEP_TOOL_PRUNE=1(保留 pruner):阈值 − md − 引导语 − 安全边距,下限 1000,
      让 md+样文整体 ≤ dsh 8192 字符阈值免被裁中段。
    """
    if not tool_prune_enabled():
        return DEFAULT_REF_CHARS
    md_len = count_text_chars(style_md_text)
    budget = DSH_RESULT_THRESHOLD_CHARS - md_len - _SAFETY_CHARS - (guide_chars or 0)
    return max(1000, budget)


def build_ref_text_for_profile(profile, max_chars=None, query=None, k=3, avoid=None):
    """组注入用 STYLE REFERENCE 文本(权威源 JSON → 加权随机 query 取样 / 预算多样 → 渲染)。

    - 笔名在样文库选了词条(profile.sample_ids) → 先收窄池子到所选词条;
    - query 给定 → pick_samples(硬过滤→软加权→加权随机→近期避重)注入 ≤k 条(mode:"pick");
    - 无 query → select_for_budget 多样封顶(pruner 关=DEFAULT_REF_CHARS,不每章灌全库);
    - 有 samples.json:返回 (text, meta{mode:"pick"|"samples",…});
    - 无 samples.json 但有旧 reference.txt:legacy 兜底;两者皆无返回 (None, None)。
    """
    from . import style_md as _sm
    samples = load_samples()  # 全局样文库(不分笔名)
    if samples is not None:
        # 笔名已选样文 → 池子收窄到所选词条;未选 → 全库(向后兼容)
        chosen_ids = [x for x in (getattr(profile, "sample_ids", None) or []) if x]
        if chosen_ids:
            by_id = {s.id: s for s in samples}
            narrowed = [by_id[i] for i in chosen_ids if i in by_id]
            if narrowed:
                samples = narrowed
        if query:
            picked, meta = pick_samples(samples, query=query, k=k, avoid=avoid)
            if not picked:
                return None, None
            meta["max_chars"] = None
            meta["pen_selected"] = bool(chosen_ids)
            return render_reference(picked), meta
        if max_chars is None:
            md_text = (_sm.read_style_md(profile) or "") if profile is not None else ""
            guide_chars = count_text_chars("# STYLE REFERENCE\n\n{0}\n\n".format(_REFERENCE_GUIDE))
            max_chars = estimate_budget_for(md_text, guide_chars)
        selected, meta = select_for_budget(samples, max_chars)
        if not selected:
            return None, None
        meta["mode"] = "samples"
        meta["max_chars"] = max_chars
        meta["pen_selected"] = bool(chosen_ids)
        return render_reference(selected), meta

    # legacy:旧扁平 reference.txt 兜底(仅全局库尚不存在时,迁移期参照;非权威)
    if profile is None:
        return None, None
    raw = _sm.read_ref(profile)
    if not raw:
        return None, None
    blocks = parse_blocks(raw)
    if len(blocks) <= 1:
        return raw, {"mode": "legacy", "count": 1, "total": 1,
                     "chars": count_text_chars(raw), "selected": [], "max_chars": max_chars}
    if max_chars is None or max_chars <= 0:
        return raw, {"mode": "legacy", "count": len(blocks), "total": len(blocks),
                     "chars": count_text_chars(raw), "selected": [], "max_chars": max_chars}
    picked = []
    total = 0
    for i, b in enumerate(blocks):
        c = count_text_chars(b)
        if not picked:
            picked.append(b)
            total = c
            continue
        if total + c <= max_chars:
            picked.append(b)
            total += c
        else:
            break
    body = "\n\n".join("<sample>\n{0}\n</sample>".format(b) for b in picked)
    text = "# STYLE REFERENCE\n\n{0}\n\n{1}".format(_REFERENCE_GUIDE, body)
    return text, {"mode": "legacy", "count": len(picked), "total": len(blocks),
                  "chars": total, "selected": [], "max_chars": max_chars}


def duplicate_warnings(samples: list) -> list:
    """近似重复检测:某条归一化文本整段包含于另一条 → 短者报「重复(子段)」。

    把「样文③是样文②子段」这类冗余暴露出来(UI/MCP 保存后提示,不硬拦)。
    任一方标 no_warn(人工确认保留,如对白是从推理里截的聚焦样本)则跳过该对。
    """
    norm, no_warn = {}, {}
    for s in samples:
        t = (s.text or "").strip()
        if not t:
            continue
        norm[s.id] = _WS_RE.sub("", t)
        if s.no_warn:
            no_warn[s.id] = True
    warns = []
    ids = [s.id for s in samples if (s.text or "").strip()]
    for i in range(len(ids)):
        for j in range(len(ids)):
            if i == j:
                continue
            a, b = ids[i], ids[j]
            if no_warn.get(a) or no_warn.get(b):
                continue  # 任一方人工确认保留 → 不提示重复
            na, nb = norm.get(a, ""), norm.get(b, "")
            # 只报较短者是较长者的子段(明显冗余);两条均不短到失去意义
            if len(na) >= 80 and len(na) <= len(nb) and na in nb:
                if not any(w["id"] == a and w["dup_of"] == b for w in warns):
                    warns.append({"id": a, "dup_of": b})
    return warns
