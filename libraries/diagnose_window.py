"""Longitudinal story diagnosis（WS4，修订 5）—— 内部 service，非 MCP。

语义分工（不改）：chapter_quality_gate = Local / chapter scope（单章五检聚合）；
本服务 = Longitudinal / recent-N·arc·book scope（「最近整个故事是不是开始坏掉」）。
二者共享 reviewer/continuity/retention/promise primitives，并统一输出 DecisionPoint。

权威源原则：
- overdue promise **只**来自 promise_ledger.scan_promises（promise 台账唯一判断源）；
  这里不透传 continuity 自算的逾期（避免双报）。底层 continuity.check_overdue_promises 保留但不出现在本窗口 dp。
"""
from __future__ import annotations

import logging

_log = logging.getLogger("diagnose_window")


def diagnose_story_window(book_id: str, recent_n: int = 6, chapter_num: int = 0,
                          scope: str = "window") -> dict:
    """聚合 continuity/retention/promises（+chapter_quality_gate 概要）成统一 DecisionPoint 流。

    返回 {ok, scope, recent_n, book_id, decision_points[], counts, summary}。
    """
    from agent_tools import diagnose_continuity, diagnose_retention, diagnose_promises
    from agent_tools import book_mgr
    from libraries.decision_feed import dp, dedupe

    book = book_mgr.get(book_id)
    if book is None:
        return {"ok": False, "error": "not_found", "decision_points": [], "counts": {},
                "summary": ""}
    continuity = diagnose_continuity(book_id, recent_n=recent_n)
    retention = diagnose_retention(book_id, recent_n=recent_n)
    promises = diagnose_promises(book_id)

    points = []
    # 连续性：warning 级 issue（跳过连续性自算的逾期——逾期由 promises 单源上报）
    for it in (continuity.get("issues") or []):
        if it.get("severity") not in ("warning", "error"):
            continue
        desc = it.get("description") or ""
        if "逾期" in desc:           # overdue_promises 只从 promise ledger 出一份
            continue
        points.append(dp(
            "continuity", it.get("category") or "issue", desc,
            severity=it.get("severity") or "warning",
            subject_id=it.get("location") or "",
            anchor=f"{book_id}:window", chapter=int(chapter_num or 0),
            suggested_action=it.get("suggestion") or ""))
    # 追读：高流失风险章
    for cl in (retention.get("chapter_level") or []):
        if int(cl.get("drop_risk") or 0) >= 7:
            points.append(dp(
                "retention", "drop_risk",
                f"第 {cl.get('chapter')} 章流失风险 {cl.get('drop_risk')}/10（{cl.get('reason') or ''}）",
                severity="warning", subject_id=f"ch{cl.get('chapter')}",
                anchor=f"ch{cl.get('chapter')}:window", chapter=int(cl.get("chapter") or 0),
                suggested_action="在章末补钩子/反转提高追读"))
    # 承诺台账（唯一权威源）
    for ov in (promises.get("overdue") or []):
        points.append(dp(
            "promises", "promise_overdue",
            f"读者承诺「{ov.get('desc') or ov.get('id')}」已逾期 {ov.get('overdue_by') or '?'} 章",
            severity="error", subject_id=ov.get("id") or "",
            anchor=f"{ov.get('id')}:window", chapter=int(chapter_num or 0),
            suggested_action="近期推进或兑现"))
    for st in (promises.get("stalled") or []):
        points.append(dp(
            "promises", "promise_stalled",
            f"伏笔「{st.get('desc') or st.get('id')}」近期无推进",
            severity="warning", subject_id=st.get("id") or "",
            anchor=f"{st.get('id')}:window", chapter=int(chapter_num or 0),
            suggested_action="找机会推进一次，别让伏笔冷掉"))
    for fn in (promises.get("fulfilled_recently") or []):
        points.append(dp(
            "promises", "promise_fulfilled",
            f"伏笔「{fn.get('desc') or fn.get('id')}」已回收",
            severity="info", subject_id=fn.get("id") or "",
            anchor=f"{fn.get('id')}:window", chapter=int(chapter_num or 0)))

    points = dedupe(points)
    from libraries.decision_feed import severity_bucket
    buckets = severity_bucket(points)
    return {
        "ok": True, "scope": scope, "recent_n": recent_n, "book_id": book_id,
        "decision_points": points,
        "counts": {"total": len(points), **buckets},
        "summary": (f"需处理 {buckets['red']} / 值得关注 {buckets['yellow']} / 正常 {buckets['green']}"
                    if points else "窗口内暂无异常"),
    }
