# -*- coding: utf-8 -*-
"""Plot 提交共享内核（字数自适应预算、硬上限校验与 Protocol v3 事实规范化）。

职责：
1. compute_effective_plot_budget：根据 assigned 目标字数与 Plot 类型，确定自适应 preferred range、
   effective_hard_max 以及 absolute_ceiling。
2. validate_plot_prose_units：正文字数硬上限拦截（超 effective_hard_max 直接拒绝，不落盘、不消费 token）。
3. normalize_plot_submission_facts：事实与关系变更统一规范化（消除双源冲突，Protocol v3 规范对象 + v2 兼容）。
"""

from __future__ import annotations

import logging
from typing import Any

from core.text_utils import count_prose_units

_log = logging.getLogger(__name__)

# 全局物理天花板（单段剧情绝对硬上限，防止失控膨胀）
ABSOLUTE_PLOT_CEILING = 1600


class PlotEffectiveMaxExceeded(RuntimeError):
    """正文字数超过当前情节段有效硬上限异常。"""

    def __init__(self, plot_id: str, actual_words: int, effective_hard_max: int, assigned: int):
        self.plot_id = plot_id
        self.actual_words = actual_words
        self.effective_hard_max = effective_hard_max
        self.assigned = assigned
        msg = (
            f"情节段「{plot_id}」正文字数 {actual_words} 超过本次允许的有效硬上限 {effective_hard_max} "
            f"（分配目标 {assigned} 字）。"
            f"请压缩本段正文，聚焦本段单一主要戏剧转向（primary_turn），"
            f"不要试图在一小段内写完后续剧情。若确实需要更大篇幅，请在编排期通过 chapter_plan 提高分配目标后再写。"
        )
        super().__init__(msg)


def compute_effective_plot_budget(assigned: int, plot_type: str = "normal") -> dict[str, int]:
    """计算自适应字数预算带：

    - assigned: 权威分配目标字数（来自 chapter_plan 覆写或 storyline planned_words）
    - preferred_min: 舒适区间下限
    - preferred_max: 舒适区间上限
    - effective_hard_max: 服务端判决硬上限（冻结进 PreparedPlotRun 与 commit_token）
    - absolute_ceiling: 物理防护天花板
    """
    try:
        assigned_val = max(100, int(assigned or 300))
    except (TypeError, ValueError):
        assigned_val = 300

    # 舒适区间（例如 300 -> 225~450；1000 -> 800~1300）
    preferred_min = max(100, int(assigned_val * 0.75))
    preferred_max = max(preferred_min + 80, int(assigned_val * 1.35))

    # 有效硬上限公式：
    # assigned=300 -> max(500, 300 * 1.35 + 150) = 555 字 (超 600/700 必拒)
    # assigned=500 -> max(700, 500 * 1.35 + 150) = 825 字
    # assigned=1000 -> max(1200, 1000 * 1.35 + 150) = 1500 字 (1150 放行)
    calc_max = int(assigned_val * 1.35 + 150)
    effective_hard_max = min(ABSOLUTE_PLOT_CEILING, max(assigned_val + 200, calc_max))

    return {
        "assigned": assigned_val,
        "preferred_min": preferred_min,
        "preferred_max": preferred_max,
        "effective_hard_max": effective_hard_max,
        "absolute_ceiling": ABSOLUTE_PLOT_CEILING,
    }


def validate_plot_prose_units(
    text: str,
    effective_hard_max: int,
    assigned: int,
    plot_id: str,
    is_v2: bool = True,
) -> int:
    """校验待提交正文是否超出有效硬上限。

    返回 actual_prose_units。如果超出且为 v2 Plot，抛出 PlotEffectiveMaxExceeded。
    """
    actual_words = count_prose_units(text)
    effective_ceiling = min(ABSOLUTE_PLOT_CEILING, max(100, int(effective_hard_max or ABSOLUTE_PLOT_CEILING)))

    if is_v2 and actual_words > effective_ceiling:
        _log.warning(
            "Plot 正文超长被硬拦截: plot=%s actual=%d effective_max=%d assigned=%d",
            plot_id, actual_words, effective_ceiling, assigned,
        )
        raise PlotEffectiveMaxExceeded(
            plot_id=plot_id,
            actual_words=actual_words,
            effective_hard_max=effective_ceiling,
            assigned=assigned,
        )
    return actual_words


def normalize_relationship_item(item: Any) -> dict[str, str]:
    """规范化单条关系变更对象（Protocol v3 统一字典形态）。"""
    if isinstance(item, str):
        # v2 兼容：单行自然语言描述
        return {
            "subject": "",
            "target": "",
            "change_type": "relationship",
            "from_state": "",
            "to_state": str(item).strip(),
            "reason": "",
        }
    if isinstance(item, dict):
        return {
            "subject": str(item.get("subject", "") or "").strip(),
            "target": str(item.get("target", "") or "").strip(),
            "change_type": str(item.get("change_type", "relationship") or "relationship").strip(),
            "from_state": str(item.get("from_state", "") or "").strip(),
            "to_state": str(item.get("to_state", "") or "").strip(),
            "reason": str(item.get("reason", "") or "").strip(),
        }
    return {
        "subject": "",
        "target": "",
        "change_type": "relationship",
        "from_state": "",
        "to_state": str(item or "").strip(),
        "reason": "",
    }


def normalize_plot_submission_facts(
    outcome: dict | None,
    character_events: list | None,
) -> tuple[dict[str, list], list[dict]]:
    """规范化 Plot 提交的事实载荷（Protocol v3 + v2 兼容）。

    解决双源冲突：
    1. 若 character_events 中含有 relationship 或 trust_change，自动提取并并入 outcome.relationship_changes；
    2. character_events 过滤后仅保留行为事件：goal_shift, power_shift, location_shift, arc_stage, note；
    3. outcome.relationship_changes 中的所有条目（无论是字符串还是对象）均归一化为 Protocol v3 规范结构；
    4. 消除双源输入报错，避免初次提交撞车。
    """
    norm_outcome = {
        "choices_made": list((outcome or {}).get("choices_made") or []),
        "information_revealed": list((outcome or {}).get("information_revealed") or []),
        "relationship_changes": [],
        "resource_changes": list((outcome or {}).get("resource_changes") or []),
        "promise_updates": list((outcome or {}).get("promise_updates") or []),
        "new_story_questions": list((outcome or {}).get("new_story_questions") or []),
    }

    # 1. 收集已存在的 outcome.relationship_changes
    raw_rel_changes = list((outcome or {}).get("relationship_changes") or [])
    norm_rel_changes = [normalize_relationship_item(x) for x in raw_rel_changes if x]

    # 2. 扫描 character_events，提取关系变动并过滤
    allowed_char_event_types = {"goal_shift", "power_shift", "location_shift", "arc_stage", "note"}
    clean_character_events = []

    for row in character_events or []:
        if not isinstance(row, dict):
            continue
        char_name = str(row.get("name") or "").strip()
        filtered_events = []
        for ev in row.get("events") or []:
            if not isinstance(ev, dict):
                continue
            ev_type = str(ev.get("type") or "").strip()
            if ev_type in ("relationship", "trust_change"):
                # 提取并入 relationship_changes
                norm_rel_changes.append({
                    "subject": char_name,
                    "target": str(ev.get("target") or ev.get("with") or "").strip(),
                    "change_type": ev_type,
                    "from_state": str(ev.get("from") or "").strip(),
                    "to_state": str(ev.get("to") or ev.get("state") or "").strip(),
                    "reason": str(ev.get("reason") or "").strip(),
                })
            elif ev_type in allowed_char_event_types:
                filtered_events.append(ev)
            else:
                # 其他未知类型降级为 note
                filtered_events.append({
                    "type": "note",
                    "reason": f"[{ev_type}] " + str(ev.get("reason") or ev.get("to") or ""),
                })
        clean_character_events.append({
            "name": char_name,
            "events": filtered_events,
        })

    norm_outcome["relationship_changes"] = norm_rel_changes
    return norm_outcome, clean_character_events
