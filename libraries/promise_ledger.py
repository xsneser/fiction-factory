"""伏笔台账规则层扫描 — 逾期/推进/停滞/近期回收（零 LLM，复用 storyline.promises 台账）。

竞品借鉴：OpenNovel F001 状态机 / AI-NWA payoff_ledger / deep-novel-system foreshadowing 字段。
与现有代码形成闭环：
  · 登记：engine._update_promises_ledger（设局→pending、收局→fulfilled）
  · 注入：prompt_harness._promises_block（写前扫一眼欠读者什么）
  · 诊断：本模块（生成后全量扫描，供 diagnose_promises 工具）
"""
import re

# 承诺描述里的检索关键词：优先「」内名词，否则去标点后的短语
_BRACKET_RE = re.compile(r'「([^」]+)」')
_STRIP_PUNCT = re.compile(r'[，。！？、；：""''（）()\s]')


def _promise_keywords(desc: str) -> list[str]:
    """从承诺描述提取检索关键词。"""
    if not desc:
        return []
    inner = _BRACKET_RE.findall(desc)
    if inner:
        return [s.strip() for s in inner if s.strip()]
    phrase = _STRIP_PUNCT.sub("", desc)
    return [phrase[:12]] if phrase else []


def _find_advance(chapters, keywords, setup_chapter, payoff_chapter):
    """在 setup 后、payoff 前的章节正文中找关键词命中，返回最后命中章（无则 None）。"""
    last = None
    for ch in chapters:
        n = int(ch.get("num", 0) or 0)
        if n <= setup_chapter:
            continue
        if payoff_chapter and n >= payoff_chapter:
            break
        content = ch.get("content", "") or ""
        if any(k and k in content for k in keywords):
            last = n
    return last


def _seen_in_recent(chapters, keywords, current_chapter, recent_n):
    """最近 recent_n 章内是否有关键词命中。"""
    for ch in chapters:
        n = int(ch.get("num", 0) or 0)
        if n > current_chapter - recent_n:
            content = ch.get("content", "") or ""
            if any(k and k in content for k in keywords):
                return True
    return False


# 读者承诺六操作分级（AI-NWA payoff_directives，规则层，零 LLM）
# seed=刚埋设保持存在感；touch=活跃维持/轻提；pressure=临期/逾期施压；
# partial_reveal=部分揭示留悬念；payoff=已兑现/本情节段收束；forbid=明确不兑现（暂不自动标）
_PARTIAL_REVEAL_HINTS = ("半", "部分", "露出一角", "一角", "线索", "碎屑", "片段")


def promise_op(q: dict, chapter_num: int) -> str:
    """按当前章号给读者承诺定「操作」分级。逾期/临期→pressure；已兑现→payoff；
    描述含部分揭示暗示→partial_reveal；本章刚设局→seed；其余活跃→touch。"""
    if not q:
        return "touch"
    if q.get("status") == "fulfilled":
        return "payoff"
    deadline = int(q.get("deadline_chapter") or 0)
    if deadline and deadline <= chapter_num:
        return "pressure"
    if deadline and 0 < deadline - chapter_num <= 3:
        return "pressure"
    desc = q.get("desc", "") or ""
    if any(h in desc for h in _PARTIAL_REVEAL_HINTS):
        return "partial_reveal"
    setup_ch = int(q.get("setup_chapter") or 0)
    if setup_ch and setup_ch == chapter_num:
        return "seed"
    return "touch"


def scan_promises(tl, chapters, current_chapter, recent_n: int = 10) -> dict:
    """伏笔台账全量扫描。

    tl：BookStoryline（含 .promises 台账）
    chapters：[{num, content}, ...]（全书章节，供推进/停滞判定）
    current_chapter：当前写作章号
    recent_n：停滞/近期回收的窗口

    返回 {overdue[], advanced[], stalled[], fulfilled_recently[], counts{}, suggestions[]}
    """
    promises = getattr(tl, "promises", None) or []
    overdue, advanced, stalled, fulfilled_recently = [], [], [], []
    for q in promises:
        status = q.get("status", "pending")
        desc = q.get("desc", "") or ""
        setup_ch = int(q.get("setup_chapter") or 0)
        payoff_ch = int(q.get("payoff_chapter") or 0)
        deadline = int(q.get("deadline_chapter") or 0)
        keywords = _promise_keywords(desc)
        item = {
            "id": q.get("id", ""),
            "desc": desc,
            "setup_chapter": setup_ch,
            "deadline_chapter": deadline,
            "payoff_chapter": payoff_ch,
            "op": promise_op(q, current_chapter),
        }
        if status == "pending" and deadline and deadline < current_chapter:
            item["overdue_by"] = current_chapter - deadline
            overdue.append(item)
        elif status == "pending":
            last = _find_advance(chapters, keywords, setup_ch, payoff_ch)
            if last:
                item["last_seen_chapter"] = last
                advanced.append(item)
            elif not _seen_in_recent(chapters, keywords, current_chapter, recent_n):
                item["stalled_since"] = max(setup_ch, current_chapter - recent_n)
                stalled.append(item)
        elif status == "fulfilled" and payoff_ch and payoff_ch >= current_chapter - recent_n:
            fulfilled_recently.append(item)

    counts = {
        "overdue": len(overdue), "advanced": len(advanced),
        "stalled": len(stalled), "fulfilled_recently": len(fulfilled_recently),
        "total": len(promises),
    }
    suggestions = []
    if overdue:
        suggestions.append(f"{len(overdue)} 条读者承诺已逾期，建议在近期章节推进或兑现。")
    if stalled:
        suggestions.append(f"{len(stalled)} 条承诺近期无推进，注意别让伏笔冷掉。")
    if not promises:
        suggestions.append("尚无读者承诺台账（大纲设局/收局情节段会生成）。")
    return {"overdue": overdue, "advanced": advanced, "stalled": stalled,
            "fulfilled_recently": fulfilled_recently, "counts": counts,
            "suggestions": suggestions}
