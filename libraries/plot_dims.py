# -*- coding: utf-8 -*-
"""情节段(Plot) → 样文选择 query 的确定性推导。

设计背景见 交接文档/研究文档 R10:写作应让「随机性落在语言参考上」而不是落在 agent 每轮
手判上。plot_run 给出 plot/弧/线程/承诺,但 scene 等 dims 一直靠 agent 脑补(旧
`scene_hint_tags=[] # 归 agent`)。本模块把「当前情节段该匹配哪种样文」收成一层纯函数规则:

- 只从情节段**自身内容字段**推导(name/category/sub_category/roles/所在弧阶段名),
  **绝不从 thread_id / promise 推导**——线程/承诺只作上下文,不得反向决定文风
  (防止「这是伏笔所以写得神秘」类 AI 味)。
- 高置信才声明:scene/cast 给;dialogue_density/pace 只在有强词汇信号时给;
  dramatic_state/information_density/pov/narrative_action 一律留给选择器通配
  (不声明 = 不过滤也不加分,让加权随机在「同场景邻近」里自然漂移)。
- 无任何命中 → 返回 {} (= 无约束),调用方仍按 k=1 从整池加权随机取 1 条。

query 喂给 style_samples.pick_samples:scene/cast/dialogue_density/pace 里,
scene 为 list,其余单值。
"""
from __future__ import annotations

# ─── 场景关键词表(命中 name/category/sub_category/弧阶段名)──────────────
# 多词命中允许共存;但命中数过多会逼出命中窗放宽,故取前 SCENE_CAP 个按此序靠前的场景。
SCENE_CAP = 3

_SCENE_KEYWORDS = [
    # (scene, 关键词);排列越靠前越「具体」,越靠后越泛
    ("negotiation",   ["谈判", "交涉", "谈条件", "要价", "压价", "游说", "结盟", "劝降", "谈崩"]),
    ("death",         ["遇害", "被杀", "尸体", "身亡", "死讯", "濒死", "重伤", "毙命", "祭天"]),
    ("action",        ["战斗", "厮杀", "打斗", "突围", "决斗", "破门", "追逐战", "大战", "交锋", "追杀", "决战", "对垒"]),
    ("danger",        ["追杀", "遇险", "被困", "包围", "陷阱", "偷袭", "逃亡", "逃命", "危机", "追杀"]),
    ("confrontation", ["对峙", "冲突", "翻脸", "拆穿", "摊牌", "拦截", "对质", "质问", "逼问", "当场"]),
    ("investigation", ["调查", "查证", "线索", "现场", "推理", "勘察", "搜证", "验尸", "盘问", "审问"]),
    ("revelation",    ["真相", "身份", "揭穿", "揭底", "反转", "秘密", "实情", "身世", "暴露"]),
    ("discovery",     ["发现", "截获", "找到", "寻到", "搜到", "捡到", "目击"]),
    ("dialogue",      ["对白", "交谈", "会议", "商量", "争论", "对话", "质问", "审问", "劝说"]),
    ("planning",      ["布局", "部署", "设局", "谋划", "计划", "准备", "布子", "筹备", "预谋"]),
    ("quiet",         ["独处", "日常", "休息", "用餐", "散步", "夜谈", "静坐", "思念", "闲谈", "温存"]),
    ("transition",    ["赶路", "返回", "入夜", "清晨", "转场", "路上", "归途", "出发", "撤离"]),
    ("aftermath",     ["善后", "余波", "清点", "收拾", "战后", "恢复", "休整"]),
    ("opening",       ["开场", "登场", "初到", "开局", "楔子", "起始", "初遇"]),
]

# category 兜底(仅关键词扫不到时用;只放可信映射)
_CATEGORY_SCENE = {
    "战斗": "action", "打斗": "action", "热血": "action",
    "悬念": "revelation", "悬疑": "investigation", "惊悚": "danger", "恐怖": "death",
    "推理": "investigation", "反转": "revelation", "解谜": "investigation",
    "谈判": "negotiation", "谋略": "planning", "权谋": "planning",
    "开篇": "opening", "开局": "opening", "日常": "quiet", "情感": "dialogue",
}

_DIALOGUE_HIGH = ["谈判", "对白", "交谈", "会议", "商量", "争吵", "争执", "质问", "审问", "摊牌", "辩论", "劝降", "斗嘴"]
_PACE_FAST = ["追杀", "追逃", "突围", "决战", "爆发", "大战", "冲杀", "逃命", "逃亡", "追逐", "速战"]
_PACE_SLOW = ["休整", "日常", "静坐", "思念", "独处", "散步", "用餐", "夜谈", "平静", "收拾"]
_CAST_CROWD = ["人群", "全员", "众", "满座"]
_CAST_SOLO = ["独处", "一人", "独自"]


def _txt(plot, tl=None):
    """拼接可搜索文本:情节段名/分类 + 所在弧的阶段名(仅当弧阶段是内容信号,非 thread/promise)。"""
    parts = [getattr(plot, "name", "") or "",
             getattr(plot, "category", "") or "",
             getattr(plot, "sub_category", "") or ""]
    oid = getattr(plot, "outline_id", "") or ""
    arc = None
    if tl is not None and oid:
        arc = next((o for o in (tl.outlines or []) if getattr(o, "id", "") == oid), None)
    if arc is not None:
        si = int(getattr(plot, "stage_index", 0) or 0)
        stages = list(getattr(arc, "stages", None) or [])
        if stages and 0 <= si < len(stages):
            st = stages[si]
            if isinstance(st, dict):
                parts.extend([st.get("name", "") or "", " ".join(st.get("events") or [])])
    return "".join(parts)


def infer_plot_query(plot, tl=None) -> dict:
    """PlotSlot(+可选已 load 的 storyline)→ 样文选择 query(英文键)。

    返回只含高置信维:scene(list)/cast/dialogue_density/pace(单值);其余维通配。
    thread_id / resolves_plot_id 等承诺线索**不参与**。无命中 → {}。
    """
    text = _txt(plot, tl)
    name = getattr(plot, "name", "") or ""
    query: dict = {}

    # ── scene:先关键词,后 category 兜底 ──
    scenes = []
    for sc, words in _SCENE_KEYWORDS:
        if any(w in text for w in words) and sc not in scenes:
            scenes.append(sc)
        if len(scenes) >= SCENE_CAP:
            break
    if not scenes:
        cat = (getattr(plot, "category", "") or "").strip()
        if cat:
            for k, v in _CATEGORY_SCENE.items():
                if k in cat or cat in k:
                    scenes.append(v)
                    break
    if scenes:
        query["scene"] = scenes[:SCENE_CAP]

    # ── cast:由出场角色数(粗) → solo/duo/small_group/large_group ──
    roles = [r for r in (getattr(plot, "roles", None) or []) if (r or "").strip()]
    n = len(roles)
    if n == 0:
        # 名字给强独处/群像信号时才猜,否则通配(避免 0 人硬猜 small_group)
        if any(w in name for w in _CAST_SOLO):
            query["cast"] = "solo"
        elif any(w in text for w in _CAST_CROWD):
            query["cast"] = "crowd"
    elif any(w in text for w in _CAST_CROWD):
        query["cast"] = "crowd"
    elif n == 1:
        query["cast"] = "solo"
    elif n == 2:
        query["cast"] = "duo"
    elif n <= 4:
        query["cast"] = "small_group"
    else:
        query["cast"] = "large_group"

    # ── dialogue_density / pace:只在强词汇信号时声明 ──
    if any(w in text for w in _DIALOGUE_HIGH):
        query["dialogue_density"] = "high"
    fast = sum(1 for w in _PACE_FAST if w in text)
    slow = sum(1 for w in _PACE_SLOW if w in text)
    if fast and fast >= slow:
        query["pace"] = "fast"
    elif slow and not fast:
        query["pace"] = "slow"

    return query
