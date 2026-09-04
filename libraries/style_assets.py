"""写法资产化 — 从文本提取风格特征 → 转成句式风格规则（规则层，零 LLM）。

竞品借鉴：AI-NWA 写法引擎 / creative-writing-skills style-creator（最小可用版）。
特征池：extract_style_features 提取 7 类特征 → features_to_rules 转成 prefer/ban 规则
写入 style_rules（kind 合一后句式风格=prefer 规则、禁止内容=ban 规则，profile.style_assets 已废弃）。
"""
import re
from collections import Counter

from .style_rules import StyleRule, StyleRuleLibrary, WORD_SEED

# 写法资产特征池（7 类）：extract_style_features 输出（每类带 enabled 开关）
STYLE_ASSET_FEATURES = (
    "sentence_length", "dialogue_ratio", "paragraph_style",
    "common_words", "avoid_words", "sentence_starters", "action_beats",
)

# 特征 → 句式风格 prefer 文本
_SL_MAP = {"short": "句长偏短（多用短句，每句8-15字）",
           "medium": "句长中等（句中偏长，15-25字为主）",
           "long": "句长偏长（可用长句铺陈，25字以上）"}
_PS_MAP = {"chatty": "段落偏短促（chatty，对话多）",
           "compact": "段落紧凑（compact）",
           "literary": "段落铺陈（literary）"}


def default_enabled() -> dict:
    """特征池默认开关：全部启用（落盘 style_assets['enabled']；缺省即启用=向后兼容）。"""
    return {k: True for k in STYLE_ASSET_FEATURES}

# 中文单字停用字（过滤高频功能字）
_STOP_CHARS = set(
    "的了是在有和就不人都一这那也我他她它你您们个中上下来去说想看到没很又但而或与及被把让给对从向为以于之其此等如因果然则所将已正再才只还更最太非常真的一直已经还是就是但是然而然后所以因为如果虽然即使尽管不过只是可是却仍仍然依然终于突然忽然顿时瞬间缓缓微微仿佛似乎不禁不由得只见但见与此同时就在这时转眼间")

# 常见动作节拍（网文高频动作描写）
_ACTION_BEATS = ("眯眼", "挑眉", "咂嘴", "抬手", "转身", "冷笑", "皱眉", "握拳",
                 "咬牙", "耸肩", "撇嘴", "低头", "抬头", "深吸一口气", "不动声色")


def _split_sentences(text: str) -> list:
    from .storyline_writer import _split_sentences as _ss
    return _ss(text)


def _top_words(text: str, n: int = 8) -> list:
    """统计高频 2 字词（简单无分词方案，过滤停用字）。"""
    cleaned = re.sub(r'[^一-鿿]', '', text or "")
    if len(cleaned) < 4:
        return []
    bigrams = [cleaned[i:i + 2] for i in range(len(cleaned) - 1)]
    counter = Counter(b for b in bigrams
                      if b[0] not in _STOP_CHARS and b[1] not in _STOP_CHARS and b[0] != b[1])
    return [w for w, c in counter.most_common(n) if c >= 2]


def _detect_avoid_words(text: str) -> list:
    """命中 AI 词表/硬禁句式的词（供 profile 的 avoid_words；读 style_rules 库）。"""
    from .style_rules import StyleRuleLibrary
    lib = StyleRuleLibrary()
    found = []
    for w in lib.get_word_map():
        if w in (text or ""):
            found.append(w)
    for pat, desc, _sev in lib.get_bans():
        if pat.search(text or ""):
            found.append(desc)
    return found[:10]


def _top_sentence_starters(sentences: list, n: int = 5) -> list:
    """高频句首词（2 字）。"""
    starters = []
    for s in sentences:
        cleaned = re.sub(r'[^一-鿿]', '', s or "")
        if len(cleaned) >= 2:
            starters.append(cleaned[:2])
    counter = Counter(st for st in starters if st[0] not in _STOP_CHARS)
    return [w for w, _ in counter.most_common(n) if counter[w] >= 2]


def _detect_action_beats(text: str) -> list:
    return [b for b in _ACTION_BEATS if b in (text or "")][:8]


def extract_style_features(text: str) -> dict:
    """从文本提取风格特征（规则层，零 LLM）。

    返回 {sentence_length, dialogue_ratio, paragraph_style, common_words,
          avoid_words, sentence_starters, action_beats}
    """
    if not text:
        return {}
    sentences = _split_sentences(text)
    avg_len = sum(len(s) for s in sentences) / max(len(sentences), 1)
    sentence_length = "short" if avg_len < 15 else ("medium" if avg_len < 25 else "long")

    lines = text.split("\n")
    dialogue_lines = sum(1 for l in lines if '「' in l or '」' in l or '"' in l or '“' in l)
    dialogue_ratio = round(dialogue_lines / max(len(lines), 1), 2)

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    avg_para = sum(len(p) for p in paras) / max(len(paras), 1)
    paragraph_style = "chatty" if avg_para < 60 else ("compact" if avg_para < 120 else "literary")

    return {
        "sentence_length": sentence_length,
        "dialogue_ratio": dialogue_ratio,
        "paragraph_style": paragraph_style,
        "common_words": _top_words(text),
        "avoid_words": _detect_avoid_words(text),
        "sentence_starters": _top_sentence_starters(sentences),
        "action_beats": _detect_action_beats(text),
    }


def features_to_rules(features: dict, profile_id: str) -> list:
    """写法资产特征 → 句式风格规则（kind 合一：正向→prefer，禁用词→ban）。

    按 enabled 开关逐项转换（关闭的特征不生成规则）；avoid_words 已在内置 AI 词表
    （默认禁止内容）里的跳过，避免重复。返回新增 StyleRule 列表（未落盘）。
    """
    srl = StyleRuleLibrary()
    existing = {r.id for r in srl.rules}
    def _nid(kind: str) -> str:
        i = 1
        while f"{kind}_{i}" in existing:
            i += 1
        existing.add(f"{kind}_{i}")
        return f"{kind}_{i}"
    def _on(key: str) -> bool:
        return bool((features.get("enabled") or {}).get(key, True))

    rules = []
    if _on("sentence_length") and features.get("sentence_length") in _SL_MAP:
        rules.append(StyleRule(id=_nid("prefer"), kind="prefer", profile_id=profile_id,
                               pattern=_SL_MAP[features["sentence_length"]]))
    if _on("dialogue_ratio") and features.get("dialogue_ratio"):
        rules.append(StyleRule(id=_nid("prefer"), kind="prefer", profile_id=profile_id,
                               pattern=f"对话占比约{int(features['dialogue_ratio']*100)}%"))
    if _on("paragraph_style") and features.get("paragraph_style") in _PS_MAP:
        rules.append(StyleRule(id=_nid("prefer"), kind="prefer", profile_id=profile_id,
                               pattern=_PS_MAP[features["paragraph_style"]]))
    if _on("common_words") and features.get("common_words"):
        rules.append(StyleRule(id=_nid("prefer"), kind="prefer", profile_id=profile_id,
                               pattern="常用词：" + "、".join(features["common_words"])))
    if _on("sentence_starters") and features.get("sentence_starters"):
        rules.append(StyleRule(id=_nid("prefer"), kind="prefer", profile_id=profile_id,
                               pattern="句首偏好：" + "、".join(features["sentence_starters"])))
    if _on("action_beats") and features.get("action_beats"):
        rules.append(StyleRule(id=_nid("prefer"), kind="prefer", profile_id=profile_id,
                               pattern="动作节拍：" + "、".join(features["action_beats"])))
    # 禁用词：不在内置 AI 词表的才转 ban（内置词表=默认禁止内容已覆盖）
    if _on("avoid_words"):
        for w in (features.get("avoid_words") or []):
            if w and w not in WORD_SEED:
                rules.append(StyleRule(id=_nid("ban"), kind="ban", profile_id=profile_id,
                                       pattern=w, desc=f"「{w}」笔名禁用词"))
    return rules
