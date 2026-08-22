"""写法资产化 — 从文本提取风格特征，可保存/组合/绑定笔名（规则层，零 LLM）。

竞品借鉴：AI-NWA 写法引擎 / creative-writing-skills style-creator（最小可用版）。
特征池：extract_style_features 提取 7 类特征 → 落 profile.style_assets（每类带 enabled
开关）；build_style_prompt / build_deai_prompt_snippet 按启用集重编译（特征池组合）。
"""
import re
from collections import Counter

from .de_ai import AI_WORD_MAP
from .style_ban import STYLE_BAN_LIST

# 写法资产特征池（7 类）：extract_style_features 输出与 profile.style_assets.enabled 键一致
STYLE_ASSET_FEATURES = (
    "sentence_length", "dialogue_ratio", "paragraph_style",
    "common_words", "avoid_words", "sentence_starters", "action_beats",
)


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
    """命中 AI 词表/硬禁句式的词（供 profile 的 avoid_words）。"""
    found = []
    for w in AI_WORD_MAP:
        if w in (text or ""):
            found.append(w)
    for pat, desc, _sev in STYLE_BAN_LIST:
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
