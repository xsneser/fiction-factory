"""Prediction → Fact reconcile（WS2）—— 纯规则、无 LLM。

原则（修订 2/3）：
- **禁止**用关键词/正则从小说正文推断语义事实。正文不属于平台可解析对象；
  「实际发生了什么」只来自 Agent 随 `save_plot_draft(outcome=...)` 结构化上报的结构化 facts。
- Reconcile **只比较** machine-comparable 的 `expected_facts[]`（{subject,type,expected_to,strength}）
  与 structured actual facts（character_events 的 {subject,type,to}）。自然语言 execution_brief/
  character_impact 仅供 Agent 阅读，**不参与硬匹配**。
- Fact 永远优先于 Prediction：漂移只更新 planning/decision（apply_fact_intents），**绝不回写正文**。
- 本模块无状态：facts 在 chapter/draft bridge、预测在 PlotSlot.expected_facts / bridge.expected_facts，
  随时可从磁盘重算；不建 run 持久化 ledger。

actual fact 条目来源：`facts.character_events`（[{name, events:[{type,from?,to?,reason?}]}]）
→ 展开为 [{subject, type, actual_to(=to)}]。其余 outcome 字段（choices_made / information_revealed /
relationship_changes / resource_changes / promise_updates / new_story_questions）为叙事性记录，
**不做机器比较**（只透出、不匹配），避免伪精确。
"""
from __future__ import annotations

import copy

# 与 character_state._ALLOWED_EVENT_TYPES 对齐的类型集合（机器可比较的范围）
ALLOWED_TYPES = {"goal_shift", "power_shift", "location_shift", "arc_stage",
                 "relationship", "trust_change", "note"}

FACT_LIST_KEYS = ("choices_made", "information_revealed", "relationship_changes",
                  "resource_changes", "promise_updates", "new_story_questions")


def facts_from_bridge(bridge) -> dict:
    """读 chapter/draft bridge 里的结构化 facts（缺省空 dict）。只读，不做任何推断。"""
    if not isinstance(bridge, dict):
        return {}
    return bridge.get("facts") if isinstance(bridge.get("facts"), dict) else {}


def actual_fact_entries(facts: dict | None) -> list:
    """把 structured facts 展开成可比较的 actual 条目 [{subject,type,actual_to}]（仅 character_events）。"""
    out = []
    for ev in (facts or {}).get("character_events") or []:
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("name") or "").strip()
        if not name:
            continue
        for e in (ev.get("events") or []):
            if isinstance(e, dict) and str(e.get("type") or "").strip() in ALLOWED_TYPES:
                out.append({"subject": name, "type": str(e["type"]),
                            "actual_to": e.get("to")})
    return out


def expected_fact_entries(plot, bridge=None, expected_facts=None) -> list:
    """取本 run 的 machine-comparable 预测：bridge.expected_facts(运行快照) 优先，
    否则回退 PlotSlot.expected_facts。每个条目 {subject,type,expected_to,strength}。"""
    src = (bridge or {}).get("expected_facts") if isinstance(bridge, dict) else None
    if not src:
        src = expected_facts
    if not src and plot is not None:
        src = list(getattr(plot, "expected_facts", None) or [])
    out = []
    for it in (src or []):
        if not isinstance(it, dict):
            continue
        subject = str(it.get("subject") or "").strip()
        typ = str(it.get("type") or "").strip()
        if subject and typ in ALLOWED_TYPES:
            out.append({"subject": subject, "type": typ,
                        "expected_to": it.get("expected_to"),
                        "strength": str(it.get("strength") or "likely")})
    return out


def reconcile_run(*, plot=None, bridge=None, expected_facts=None,
                  based_on_storyline_revision=0, current_revision=0,
                  chapter_num=0) -> dict:
    """对一次 Plot Run 做 reconcile：expected_facts vs actual facts。

    返回 {plot_id, run_id, matched[], drifts[], unpredicted[], kind, stale, summary}：
    - matched:     预测命中（expected_to == actual_to）
    - drifts:      预测了但事实不符/缺失 [{kind: prediction_drift|missed_prediction, subject, type,
                    expected_to, actual_to?, strength}]
    - unpredicted: 事实出现但无预测 [{subject,type,actual_to}]
    - kind: clean | prediction_drift | missed_prediction | unpredicted_fact
    - stale: current_revision > based_on_storyline_revision + 1（除本章自身 bump 外故事又变了）
    """
    pid = getattr(plot, "id", "") if plot is not None else ""
    based = int(based_on_storyline_revision or 0)
    cur = int(current_revision or 0)
    facts = facts_from_bridge(bridge)
    exp = expected_fact_entries(plot, bridge, expected_facts)
    act = actual_fact_entries(facts)

    def _to(t):
        return t if t is not None else None

    matched, drifts, unpredicted = [], [], []
    # 期望逐一核对
    for e in exp:
        cands = [a for a in act if a["subject"] == e["subject"] and a["type"] == e["type"]]
        if not cands:
            if e.get("strength") != "possible":
                drifts.append({"kind": "missed_prediction", "subject": e["subject"],
                               "type": e["type"], "expected_to": _to(e.get("expected_to")),
                               "strength": e.get("strength", "likely")})
            continue
        hits = [a for a in cands if a.get("actual_to") == e.get("expected_to")]
        if hits:
            matched.append({"subject": e["subject"], "type": e["type"],
                            "expected_to": e.get("expected_to"), "actual_to": hits[0]["actual_to"]})
        else:
            drifts.append({"kind": "prediction_drift", "subject": e["subject"],
                           "type": e["type"], "expected_to": e.get("expected_to"),
                           "actual_to": cands[0].get("actual_to"), "strength": e.get("strength", "likely")})
    # 未被预测的额外事实
    for a in act:
        if not any(e["subject"] == a["subject"] and e["type"] == a["type"] for e in exp):
            unpredicted.append({"subject": a["subject"], "type": a["type"],
                                "actual_to": a.get("actual_to")})

    kind = "clean"
    if any(d["kind"] == "prediction_drift" for d in drifts):
        kind = "prediction_drift"
    elif any(d["kind"] == "missed_prediction" for d in drifts):
        kind = "missed_prediction"
    elif unpredicted:
        kind = "unpredicted_fact"
    stale = bool(based) and cur > based + 1
    reason_parts = []
    if drifts:
        reason_parts.append("预测与事实不符")
    if stale:
        reason_parts.append("故事线版本已前进(基于旧版本运行)")
    return {
        "plot_id": pid,
        "run_id": (bridge or {}).get("run_id") or (f"{pid}@{based}" if pid else ""),
        "based_on_storyline_revision": based,
        "chapter_num": int(chapter_num or 0),
        "matched": matched, "drifts": drifts, "unpredicted": unpredicted,
        "kind": kind, "stale": stale,
        "facts_payload": facts,
        "summary": ("运行与预测一致" if kind == "clean"
                    else "；".join(reason_parts) or "存在未覆盖事实"),
    }


def reconcile_chapter(book_id: str, chapter_num: int, tl=None) -> dict:
    """遍历一章各 bridge 逐 plot reconcile（幂等：facts 已持久，可随时重算）。"""
    from core.json_store import read_json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "books", book_id, "chapters", f"{int(chapter_num):04d}.json")
    raw = read_json(path) if os.path.exists(path) else None
    if not isinstance(raw, dict):
        return {"ok": False, "chapter": int(chapter_num or 0), "runs": []}
    from libraries.storyline import load_storyline
    if tl is None:
        tl = load_storyline(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "books", book_id, "storyline.json"))
    by_plot = {p.id: p for p in (tl.plots or [])} if tl else {}
    runs = []
    cur_rev = int(getattr(tl, "storyline_revision", 0) or 0) if tl else 0
    for b in (raw.get("bridges") or []):
        if not isinstance(b, dict) or not b.get("plot_id"):
            continue
        plot = by_plot.get(b.get("plot_id"))
        runs.append(reconcile_run(
            plot=plot, bridge=b,
            based_on_storyline_revision=int(b.get("based_on_storyline_revision") or 0),
            current_revision=cur_rev, chapter_num=int(chapter_num or 0)))
    return {"ok": True, "chapter": int(chapter_num or 0), "runs": runs}


def apply_fact_intents(book_id: str, tl, book, runs) -> dict:
    """以事实校正 planning_state.character_intents（修订 3：Fact 优先于旧 Prediction）。

    对每个带结构化事实变化的角色 upsert 进 character_intents，挂上最近观察到的事实
    （observations[type]=to + basis=fact + chapter），供下次 replan 不再沿用被推翻的旧预测。
    只动 character_intents；不覆盖 committed 预算、不改 story_questions 之外的字段、
    **绝不回写正文**。失败静默返回 ok=False（不阻塞正文已成功落盘）。
    """
    from libraries.planning_state import (load_planning_state, save_planning_state,
                                          upsert_character_intents)
    try:
        observed = {}  # name -> {type: to}
        for r in (runs or []):
            facts = r.get("facts_payload") or {}
            for it in facts.get("character_events") or []:
                name = str(it.get("name") or "").strip()
                if not name:
                    continue
                bucket = observed.setdefault(name, {})
                for e in (it.get("events") or []):
                    if isinstance(e, dict) and str(e.get("type") or "").strip() in ALLOWED_TYPES:
                        bucket[str(e["type"])] = e.get("to")
        if not observed:
            return {"ok": True, "updated": 0}
        state = load_planning_state(book_id, tl, book, persist=False)
        upserts = []
        for name, obs in observed.items():
            upserts.append({"name": name, "observations": obs,
                            "basis": "fact", "status": "observed"})
        merged = upsert_character_intents(state.get("character_intents"), upserts)
        changed = merged != state.get("character_intents")
        if changed:
            state["character_intents"] = merged
            state["storyline_revision"] = int(getattr(tl, "storyline_revision", 0) or 0)
            state["written_until_word"] = int(getattr(book, "total_words", 0) or 0) if book else 0
            save_planning_state(book_id, state)
        return {"ok": True, "updated": len(observed), "changed": changed}
    except Exception:
        return {"ok": False, "updated": 0}
