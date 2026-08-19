"""追读诊断（规则层，零成本）— 最近 N 章正文 + 摘要 → 章级钩子强度 / 掉读风险 / 建议。

设计文档 §4.3。复用 `reviewer.check_cliffhanger` 的章末钩子信号；
前 3 章对话占比偏低（信息密度不足）视为开篇干、掉读风险高。LLM 抽查可选（预留）。
"""
import re

from .reviewer import ContentReviewer
from core.text_utils import count_prose_units

# 章末钩子信号（与 reviewer.check_cliffhanger 同源，加权计数）
_HOOK_SIGNALS = ["突然", "忽然", "就在这时", "只见", "却", "但", "可", "？",
                 "……", "没想到", "然而", "猛地", "震惊", "冷声", "骤然",
                 "跌", "瞳孔", "心跳", "猛地", "一刀", "迎面"]


def _dialogue_ratio(text: str) -> float:
    """对话占比：引号内字符 / 总字符。"""
    if not text:
        return 0.0
    quoted = sum(len(m) for m in re.findall(r'[“"][^”"]*[”"]', text))
    total = len(text)
    return round(quoted / total, 2) if total else 0.0


def _hook_strength(content: str) -> int:
    """章末 200 字钩子强度 0-10（信号命中计数）。"""
    tail = content[-200:]
    hits = sum(1 for s in _HOOK_SIGNALS if s in tail)
    if hits >= 3:
        return 9
    if hits == 2:
        return 7
    if hits == 1:
        return 5
    return 2


def diagnose_chapters(chapters: list, summaries: list = None) -> dict:
    """追读诊断：逐章 钩子强度/掉读风险 + 全书建议。

    chapters: [{num, content, title?, summary?}] 升序（num 可用 int）。
    summaries: 章节摘要列表（暂只透传，不参与评分）。
    返回 {chapter_level: [{chapter, hook_strength, drop_risk, dialogue_ratio, reason}],
          suggestions: []}。
    """
    reviewer = ContentReviewer()
    levels = []
    for i, ch in enumerate(chapters):
        content = ch.get("content") or ""
        if not content:
            continue
        cliff = reviewer.check_cliffhanger(content)
        has_hook = not any(
            iss.severity == "info" and iss.category == "hook" for iss in cliff)
        hook = 9 if has_hook else _hook_strength(content)
        dq = _dialogue_ratio(content)
        wc = count_prose_units(content)
        risk = max(0, 10 - hook)
        if i < 3 and dq < 0.15:
            risk = min(10, risk + 2)     # 开篇对话少 → 信息密度不足，掉读风险高
        if wc and wc < 800:
            risk = min(10, risk + 1)     # 篇幅过短
        levels.append({
            "chapter": ch.get("num") or (i + 1),
            "hook_strength": hook,
            "drop_risk": risk,
            "dialogue_ratio": dq,
            "reason": ("章末有钩子" if has_hook else "章末钩子弱")
                      + f"，对话占比 {int(dq * 100)}%",
        })

    suggestions = []
    weak = [l for l in levels if l["drop_risk"] >= 7]
    if weak:
        nums = "、".join(str(l["chapter"]) for l in weak[:5])
        suggestions.append(
            f"第 {nums} 章掉读风险高：章末强化悬念/反转钩子，"
            "开篇提高对话与冲突密度（当前对话占比偏低）")
    first = levels[0] if levels else None
    if first and first["hook_strength"] <= 5:
        suggestions.append("开篇（第 1 章）钩子弱：黄金三章内尽早抛出冲突/悬念/金手指")

    return {"chapter_level": levels, "suggestions": suggestions}
