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
_REVEAL = "真相|揭穿|承认|实情|居然是|竟然是|原来如此|瞒不住|供出|坦白|水落石出"
_DEDUCE = "推理|推断|推测|八成|想必|意味着|线索|证据|证明|据此|不难猜|猜到|想通"


def _speakers(text):
    return {m for m in _TAG.findall(text)}


def _cast_from_speakers(n, legacy_cast=None, legacy_tags=None):
    if legacy_cast:
        return legacy_cast
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
    # n==0(独白/旁白):只有 legacy 给强信号才猜,否则留空=通配——避免所有无对白文本
    # 都被硬猜成 small_group(0 人说话不代表群像,会把单人样文塞进群像池)。
    if legacy_tags and "独处·心理" in legacy_tags:
        return "solo"
    if legacy_tags and any("群像" in t for t in legacy_tags):
        return "large_group"
    return ""


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


_PACE_FAST = re.compile(r"逃命|追杀|突围|扑向|冲向|爆发|拔刀|血战|火并|疾驰|冲杀|突入|炸开")
_PACE_SLOW = re.compile(r"发呆|沉思|独处|漫步|入睡|安安静静|缓缓|慢慢|收拾|整理|平静下来|发怔|出神")


def _pace(text):
    # 用动作/静止词汇信号判快慢,不再用平均句长公式——长文本永远算出 slow,
    # 会让全场 pace 塌缩成同一值(见 P2 诊断)。无强信号 → 空(通配),交给场景邻近随机。
    body = text or ""
    fast = len(_PACE_FAST.findall(body))
    slow = len(_PACE_SLOW.findall(body))
    if fast and fast >= slow:
        return "fast"
    if slow and slow >= 2 and slow > fast:
        return "slow"
    return ""


def _dramatic(text, legacy=None):
    body = text or ""
    scores = {"crisis": len(re.findall(_CRISIS, body)),
              "escalating": len(re.findall(_ESC, body)),
              "tense": len(re.findall(_TENSE, body)),
              "uneasy": len(re.findall(_UNEASY, body)),
              "calm": len(re.findall(_CALM, body))}
    best = max(scores, key=scores.get)
    if scores[best] < 2:
        # 信号太弱就不标——避免普通叙述只因带个「笑/难道」就被打成 calm/uneasy,
        # 全场戏剧状态塌缩。legacy(旧中文标签)给的强推断仍由 suggest_dims 的 dram_g 补。
        return "uneasy" if "独处·心理" in (legacy or []) else ""
    # 平静词与不安词并现 → 取更高;crisis 词极少时不抬
    if best == "calm" and scores.get("uneasy", 0) >= 2 and scores["calm"] <= 1:
        return "uneasy"
    return best


def _info_density(text):
    hits = len(re.findall(_INFO, text))
    if hits >= 6:
        return "high"
    if hits >= 2:
        return "medium"
    return "low"


def _narrative(text, legacy=None):
    acts = set()
    # 收紧:旧 _REVEAL/_DEDUCE 混入「原来/发现/所以/应该/难道」等高频词 → 几乎每篇都带
    # deduce/reveal,全池向量雷同。现用窄标记(reveal/deduce 各 ≥2 命中强词才记)。
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


def annotate_samples(samples, force=False):
    """对 StyleSample 列表预标。

    - force=False(默认,安全):合并进现有 dims,已有值保留、只补空维——旧的过度默认标签
      不会被动清除;
    - force=True(重标/审计用):整条按新规则重算覆盖(治理低区分度时先 force 预览再人工微调)。
    """
    out = []
    for s in samples:
        sug = suggest_dims(s.text or "", legacy_tags=getattr(s, "scene_tags", None))
        if force:
            s.dims = sug
        else:
            cur = dict(s.dims or {})
            for f, v in sug.items():
                if f not in cur or not cur[f]:
                    cur[f] = v
            s.dims = cur
        out.append(s)
    return out
