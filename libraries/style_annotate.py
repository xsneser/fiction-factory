# -*- coding: utf-8 -*-
"""样文多维权表「自动预标」—— 规则启发式,供存量迁移与 /samples「一键预标」。

只做结构性维度(scene/dramatic_state/narrative_action/cast/dialogue_density/
information_density/pace),不做风格词/质量分。英文键存储(见 style_samples.DIM_CHOICES),
UI 中文展示。词条缺某维 = 选择器通配。输出只是「建议」,人工在 /samples 微调后落库。

迁移路径:旧中文 scene_tags → 折算到英文 scene/cast/narrative_action 等 + 文本统计补其余维。
"""
import re
from . import style_samples as ss

# 旧 10 类中文标签 → (scene, cast_guess, dramatic_guess, dialogue_guess, narr_guess)
LEGACY = {
    "开场":        (["opening"], None, None, None, ["introduce", "establish"]),
    "群像":        ([], "large_group", None, None, ["transition", "introduce"]),
    "推理":        (["investigation", "exploration"], None, "uneasy", None, ["investigate", "deduce"]),
    "多人对白":     (["dialogue"], "small_group", None, "high", ["investigate", "obstruct"]),
    "冲突·威胁":    (["confrontation", "danger"], None, "tense", None, ["escalate", "obstruct"]),
    "死亡·惊悚":    (["death", "danger"], None, "crisis", None, ["escalate", "reveal"]),
    "规则·设定":    (["revelation", "planning"], None, "calm", None, ["establish", "reveal"]),
    "独处·心理":    (["quiet"], "solo", "uneasy", "none", ["decision", "consequence"]),
    "过渡·日常":    (["quiet", "transition"], None, "calm", None, ["transition", "relationship"]),
    "高潮·转折":    (["revelation"], None, "escalating", None, ["reveal", "pay_off"]),
}
_TAG = re.compile(r"[」”]+\s*([一-鿿A-Za-z·]{1,5}?)(?:说道|说|问|道|喊|骂|开口|回应|回答|大叫|轻声)")
_CRISIS = "死|杀|血|尸|命|尖叫|崩溃|救不了|刺入|枪|爆炸"
_ESC = "逼近|涌来|冲向|扑向|动手|扑上来|追上来"
_TENSE = "对峙|冷|瞪|威胁|针锋|沉默|冷汗|紧绷|剑拔弩张|颤抖"
_UNEASY = "奇怪|诡异|不安|怀疑|不对劲|心|难道|不禁"
_CALM = "平静|日常|笑|吃饭|喝酒|啤酒|安静|散步|太阳|温暖|牛奶|唱歌"
_INFO = "规则|为什么|因为|也就是说|这意味着|真相|解释|答案|原来是|应该|推理|线索|证据|证明|所以"
_REVEAL = "真相|原来|居然|竟然|发现|揭穿|承认|说明|其实"
_DEDUCE = "推理|猜|推测|应该|难道|意味着|证明|线索|证据|所以|八成|想必"


def _speakers(text):
    return {m for m in _TAG.findall(text)}


def _cast_from_speakers(n, legacy_cast=None, legacy_tags=None):
    if legacy_cast:
        return legacy_cast
    if legacy_tags and "独处·心理" in legacy_tags:
        return "solo"
    if n >= 8:
        return "crowd"
    if n >= 5:
        return "large_group"
    if n >= 3:
        return "small_group"
    if n == 2:
        return "duo"
    if n == 1:
        return "solo"
    if legacy_tags and any("群像" in t for t in legacy_tags):
        return "large_group"
    return "small_group"  # 兜底


def _dialogue_density(text):
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return "low"
    q = sum(1 for p in paras if p.lstrip().startswith("“"))
    r = q / len(paras)
    if r == 0:
        return "none"
    if r < 0.3:
        return "low"
    if r < 0.55:
        return "medium"
    return "high"


def _pace(text):
    body = re.sub(r"\s", "", text)
    if not body:
        return "medium"
    sents = re.findall(r"[。！？…]+", body)
    n = max(1, len(sents))
    avg = len(body) / n
    if avg < 11:
        return "fast"
    if avg > 20:
        return "slow"
    return "medium"


def _dramatic(text, legacy=None):
    body = text
    scores = {"crisis": len(re.findall(_CRISIS, body)),
              "escalating": len(re.findall(_ESC, body)),
              "tense": len(re.findall(_TENSE, body)),
              "uneasy": len(re.findall(_UNEASY, body)),
              "calm": len(re.findall(_CALM, body))}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "uneasy" if "独处·心理" in (legacy or []) else "calm"
    # 平静词与不安词并现 → 取更高;crisis 词极少时不抬
    if best == "calm" and scores.get("uneasy", 0) >= 2 and scores["calm"] <= 1:
        return "uneasy"
    return best if scores[best] >= 1 else ("calm")


def _info_density(text):
    hits = len(re.findall(_INFO, text))
    if hits >= 6:
        return "high"
    if hits >= 2:
        return "medium"
    return "low"


def _narrative(text, legacy=None):
    acts = set()
    if len(re.findall(_REVEAL, text)) >= 2:
        acts.add("reveal")
    if len(re.findall(_DEDUCE, text)) >= 2:
        acts.add("deduce")
    if re.search(r"线索|证据|调查|查证|盘问|审问", text):
        acts.add("investigate")
    if re.search(r"威胁|围住|动手|捉住|抓住|堵住", text):
        acts.add("obstruct")
    if re.search(r"救|逃|破解|解决|摆脱|脱险", text):
        acts.add("resolve")
    if re.search(r"计划|战术|对策|决定|要.{0,6}(走|做)|方案", text):
        acts.add("decision")
    if re.search(r"规则|守则|合同|条件|游戏规则|只要.{0,8}就", text):
        acts.add("establish")
    if legacy:
        for tag in legacy:
            if tag in LEGACY:
                acts.update(LEGACY[tag][4])
    return list(acts)


def suggest_dims(text, legacy_tags=None):
    """给一段样文原文(可选旧中文 tags)算多维权表建议。返回只含非空合法值。"""
    legacy_tags = [t for t in (legacy_tags or []) if t]
    scene, cast_g, dram_g, dial_g, narr_g = [], None, None, None, []
    for tag in legacy_tags:
        if tag in LEGACY:
            s, c, dg, dd, na = LEGACY[tag]
            scene.extend(s)
            if c:
                cast_g = c
            if dg and not dram_g:
                dram_g = dg
            if dd:
                dial_g = dd
            narr_g.extend(na)
    scene = list(dict.fromkeys(scene))
    if not scene:
        # 无 legacy 场景线索时从正文粗判
        if re.search(r"游戏|关卡|房间|回合", text):
            scene.append("planning")
        if re.search(r"尸|死|血|杀", text):
            scene.append("death")
    narr = _narrative(text, legacy_tags)
    if not narr:
        narr = narr_g

    speakers = _speakers(text)
    dims = {
        "scene": scene,
        "dramatic_state": dram_g or _dramatic(text, legacy_tags),
        "narrative_action": narr,
        "cast": _cast_from_speakers(len(speakers), cast_g, legacy_tags),
        "dialogue_density": dial_g or _dialogue_density(text),
        "information_density": _info_density(text),
        "pace": _pace(text),
    }
    return ss._clean_dims(dims)


def annotate_samples(samples):
    """对 StyleSample 列表预标:合并进现有 dims(已有值保留,空维补建议)。"""
    out = []
    for s in samples:
        cur = dict(s.dims or {})
        sug = suggest_dims(s.text or "", legacy_tags=getattr(s, "scene_tags", None))
        for f, v in sug.items():
            if f not in cur or not cur[f]:
                cur[f] = v
        s.dims = cur
        out.append(s)
    return out
