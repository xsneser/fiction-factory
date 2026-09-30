"""统一 DecisionPoint 决策流（WS3，修订 4）—— ephemeral projection，非第 5 份事实源。

Promise Ledger / Planning State / Reviewer / Diagnose / Reconcile / Boundary 各自保留权威
数据（底层**绝不合并**），只通过 `dp()` 构造统一视图、`collect_*` 聚合成单一 list 供 UI/Agent 消费。
不落 decision_feed.json，不新增任何持久化文件。

DecisionPoint id 用 source:kind:subject + anchor(chapter/revision/plot/promise) 的确定性短哈希，
同一问题在不同 chapter/revision 不会 id 冲突；同源同 subject 同 anchor 重复生成 → id 相同便于去重。
保留向后兼容冗余键 check/description/location/suggestion（既有 gate/validate/skill 读取不破坏）。
"""
from __future__ import annotations

import hashlib

DECISION_SOURCES = (
    "reviewer", "continuity", "retention", "promises", "planning",
    "reconcile", "storyline", "world", "boundary",
)

# kind 白名单（建议，不强校验）
SEVERITY_ORDER = {"info": 0, "ok": 0, "warning": 1, "error": 2, "critical": 3}

# 旧 check 名 → DecisionPoint source（gate/validate 手写形状迁移用）
SOURCE_BY_CHECK = {
    "review": "reviewer", "review_text": "reviewer",
    "continuity": "continuity", "retention": "retention",
    "promises": "promises", "overdue_promises": "promises",
    "storyline": "storyline", "structure": "storyline",
    "world": "world", "boundary": "boundary", "reconcile": "reconcile",
}


def annotate(points: list, default_source: str = "reviewer") -> list:
    """把旧形状 {check,severity,description,location,suggestion} 的决策点升级为统一 dp（幂等）。

    已含 source/id/anchor 的条目原样返回；旧条目补全 source/kind/id/anchor，保留 compat 键。
    """
    out = []
    for p in (points or []):
        if not isinstance(p, dict):
            continue
        if p.get("id") and p.get("source"):
            out.append(p)
            continue
        kind = p.get("check") or p.get("kind") or "issue"
        src = SOURCE_BY_CHECK.get(str(kind)) or p.get("source") or default_source
        out.append(dp(source=src, kind=kind,
                      message=p.get("description") or p.get("message") or "",
                      severity=p.get("severity") or "warning",
                      subject_id=p.get("subject_id") or p.get("location") or "",
                      suggested_action=p.get("suggestion") or p.get("suggested_action") or "",
                      blocking=bool(p.get("blocking")),
                      story_revision=p.get("story_revision"), chapter=p.get("chapter"),
                      anchor=p.get("anchor") or p.get("location") or "",
                      location=p.get("location") or "", suggestion=p.get("suggestion") or ""))
    return out


def _dp_id(source, kind, subject_id, anchor) -> str:
    key = "|".join(str(x or "") for x in (source, kind, subject_id, anchor))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def dp(source: str, kind: str, message: str, *, severity: str = "warning",
       subject_id: str = "", suggested_action: str = "", blocking: bool = False,
       story_revision: int | None = None, chapter: int | None = None,
       anchor: str = "", location: str = "", suggestion: str = "") -> dict:
    """构造统一 DecisionPoint。source ∈ DECISION_SOURCES（不校验，宽松）。

    anchor 建议填 plot_id/chapter/revision/promise id 等能区分「时期」的最小标识，防跨期 id 冲突。
    """
    sev = severity if severity in SEVERITY_ORDER else "warning"
    return {
        "id": _dp_id(source, kind, subject_id, anchor or (f"{chapter or ''}:{story_revision or ''}")),
        "source": source, "severity": sev, "kind": kind,
        "subject_id": subject_id, "anchor": anchor,
        "message": str(message), "suggested_action": str(suggested_action),
        "blocking": bool(blocking),
        "story_revision": story_revision, "chapter": chapter,
        # 向后兼容冗余键
        "check": kind, "description": str(message),
        "location": location or anchor, "suggestion": suggestion or suggested_action,
    }


def dp_from_reconcile(run: dict) -> list:
    """把一次 reconcile run 的漂移/未预测事实转成 DecisionPoint（source=reconcile）。"""
    out = []
    rev = run.get("based_on_storyline_revision")
    chapter = run.get("chapter_num")
    for d in (run.get("drifts") or []):
        kind = d.get("kind")
        if kind == "prediction_drift":
            msg = (f"{d.get('subject')} 预期{d.get('type')}→{d.get('expected_to')}，"
                   f"实际→{d.get('actual_to')}")
            action = "以实际为准，更新规划中该人物的意图"
        elif kind == "missed_prediction":
            msg = (f"预测 {d.get('subject')} 会 {d.get('type')}→{d.get('expected_to')}，"
                   "正文未发生")
            action = "确认是省略了推进，还是剧情改道；据此修 future_intents"
        else:
            continue
        out.append(dp("reconcile", f"reconcile_{kind}", msg,
                      severity="warning", subject_id=run.get("plot_id") or "",
                      anchor=f"{d.get('subject')}:{d.get('type')}", chapter=chapter,
                      story_revision=rev, suggested_action=action))
    for u in (run.get("unpredicted") or []):
        out.append(dp("reconcile", "reconcile_unpredicted_fact",
                      f"{u.get('subject')} 实际发生 {u.get('type')}→{u.get('actual_to')}（预测未覆盖）",
                      severity="info", subject_id=run.get("plot_id") or "",
                      anchor=f"{u.get('subject')}:{u.get('type')}", chapter=chapter,
                      story_revision=rev,
                      suggested_action="作为新事实并入 planning 判断"))
    if run.get("stale"):
        out.append(dp("reconcile", "stale_run",
                      f"{run.get('run_id')} 基于旧版本运行，故事线已前进",
                      severity="warning", subject_id=run.get("plot_id") or "",
                      anchor=run.get("run_id") or "", chapter=chapter,
                      story_revision=rev, suggested_action="refresh_and_reconcile"))
    return out


def dedupe(points: list) -> list:
    """按 id 去重（同源同 subject 同 anchor 相同问题只留一条），保持出现顺序。"""
    seen, out = set(), []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        if p.get("id") in seen:
            continue
        seen.add(p.get("id"))
        out.append(p)
    return out


def severity_bucket(points) -> dict:
    """红/黄/绿 摘要（UI 首层）。blocking 或 error/critical → red；warning → yellow；余 → green。"""
    red = yellow = green = 0
    for p in points or []:
        if p.get("blocking") or p.get("severity") in ("error", "critical"):
            red += 1
        elif p.get("severity") == "warning":
            yellow += 1
        else:
            green += 1
    return {"red": red, "yellow": yellow, "green": green}


def collect_decision_points(groups) -> dict:
    """聚合若干来源的 dp 列表 → {decision_points, summary, counts, buckets}（ephemeral）。"""
    points = dedupe([p for g in (groups or []) for p in (g or [])])
    buckets = severity_bucket(points)
    return {
        "decision_points": points,
        "counts": {"total": len(points), **buckets},
        "buckets": buckets,
        "summary": (f"需处理 {buckets['red']} / 值得关注 {buckets['yellow']} / 正常 {buckets['green']}"
                    if points else "暂无待处理事项"),
    }
