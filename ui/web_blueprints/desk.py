"""写作台引擎（按情节段撰写/续写） — 蓝图（自 ui/web_ui.py 按域拆分）。"""
import sys, os, json, threading, logging, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Blueprint, render_template, render_template_string, request, jsonify, redirect, url_for, Response, stream_with_context
from .ctx import *
bp = Blueprint("desk", __name__)

# ═══════════════════════════════════════════
# ✍️ 写作台（按书列出，进入写作/续写）
@bp.route("/desk")
# ═══════════════════════════════════════════

def desk_list():
    """写作台已按书进入（书库每本书的「开始写作/继续写作」入口），
    /desk 无书上下文时直接引导回书库，避免空页面死胡同。"""
    return redirect(url_for("books.books"), 302)


@bp.route("/books/start/timeline/<timeline_id>/write")
def _compat_storyline_start_writing(timeline_id):
    """旧「从故事线启动写作」URL 兼容：规划书即正式书，「开始写作」统一走 /books/<id>/continue。"""
    return redirect(url_for("desk.continue_book_page", book_id=timeline_id), 302)


# ─── 兼容：旧 /books/timeline/write 与 /api/timeline-engine 前缀 ───

@bp.route("/books/timeline/write/<engine_id>")
def _compat_storyline_write_flow(engine_id):
    return redirect(url_for("desk.storyline_write_flow", engine_id=engine_id), 302)


@bp.route("/api/timeline-engine/<path:rest>", methods=["POST"])
def _compat_api_storyline_engine(rest):
    return redirect("/api/storyline-engine/" + rest, 307)


def _chapters_from_disk(book_id: str, current_chapter: int):
    """从磁盘构造「已写章节 + 进行中草稿」列表（跨进程 stale 免疫：MCP/dsh 子进程写盘后可见）。

    MCP 是独立进程，save_chapter_text/save_plot_draft 只写磁盘 book.json/chapters/、draft_chapter.json；
    Web 进程缓存的 engine.book.current_chapter 可能滞后。这里全部从磁盘现读。"""
    from core.text_utils import count_prose_units
    chapters = []
    for n in range(1, current_chapter + 1):
        ch = book_mgr.load_chapter(book_id, n)
        if ch and ch.get("content"):
            metrics = {"actual_prose_units": int(ch.get("actual_prose_units") or count_prose_units(ch.get("content") or "")),
                       "raw_codepoints": int(ch.get("raw_codepoints") or len(ch.get("content") or ""))}
            chapters.append({
                "num": n,
                    "title": ch.get("title") or f"第{n}章",
                    "content": ch.get("content") or "",
                    "bridges": ch.get("bridges") or [],
                    "word_count": metrics["actual_prose_units"],
                    **metrics,
            })
    # 进行中草稿（draft_chapter.json）：bridges 逐情节段 span.m-bridge，刚写完的情节段即时可见
    dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
    if os.path.exists(dp):
        try:
            with open(dp, encoding="utf-8") as f:
                draft = json.load(f)
            bridges = draft.get("bridges") or []
            buffer = draft.get("buffer") or []
            dn = int(draft.get("chapter_num") or 0)
            if dn > current_chapter and (bridges or buffer):
                chapters.append({
                    "num": dn,
                    "title": "（写作中）",
                    "content": "\n\n".join((b.get("text") or "") for b in bridges) if bridges
                               else "\n\n".join(buffer),
                    "bridges": bridges,
                    "word_count": count_prose_units("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer)),
                    "actual_prose_units": count_prose_units("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer)),
                    "raw_codepoints": len("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer)),
                    "draft": True,
                })
        except Exception as e:
            logging.getLogger(__name__).warning("加载进行中草稿失败: %s", e)
    return chapters


def _plot_context_projection(tl, plot):
    """从正式故事线提取某个 Plot 的弧/线程只读摘要，供对照投影使用。

    这里只读取已落盘的结构，不调用模型，也不把当前 Plot 的规划状态倒推成历史事实。
    """
    if tl is None or plot is None:
        return {}, {}
    arc = next((o for o in (tl.outlines or []) if o.id == getattr(plot, "outline_id", "")), None)
    arc_goal = {}
    if arc:
        arc_goal = {
            "arc_id": getattr(arc, "id", "") or "",
            "name": getattr(arc, "name", "") or "",
            "stage_index": int(getattr(plot, "stage_index", 0) or 0),
            "stage_name": "",
            "stage_desc": "",
        }
        stages = list(getattr(arc, "stages", None) or [])
        idx = arc_goal["stage_index"]
        if 0 <= idx < len(stages) and isinstance(stages[idx], dict):
            arc_goal["stage_name"] = stages[idx].get("name", "") or ""
            arc_goal["stage_desc"] = stages[idx].get("description", "") or ""
    tid = getattr(plot, "thread_id", "") or ""
    thread_def = next((t for t in (tl.threads or [])
                       if (t.get("id") if isinstance(t, dict) else getattr(t, "id", "")) == tid), None)
    if isinstance(thread_def, dict):
        thread = {"id": tid, "name": thread_def.get("name", "") or "",
                  "desc": thread_def.get("desc", "") or ""}
    else:
        thread = {"id": tid, "name": "", "desc": ""}
    if tid:
        same = [q for q in (tl.plots or []) if (getattr(q, "thread_id", "") or "") == tid]
        thread["written_count"] = sum(1 for q in same if getattr(q, "written_chapter", 0) or 0)
        thread["unwritten_count"] = sum(1 for q in same if not (getattr(q, "written_chapter", 0) or 0))
    return arc_goal, thread


def _plot_outcome(bridge, tl, chapter_num=0):
    """把一条已完成 plot 的 bridge 转成 Prediction→Fact 对照视图（WS2）。

    predictions = PlotSlot.character_impact（自然语言，供人读）；expected_facts = 可机器比较预测；
    facts = structured facts（agent 上报，绝不从正文推断）；reconcile = reconcile_run 对照结果。
    """
    if not bridge:
        return None
    from libraries.reconcile import reconcile_run
    pid = bridge.get("plot_id")
    plot = next((x for x in ((tl.plots) or []) if x.id == pid), None) if tl is not None else None
    facts = bridge.get("facts") or {}
    if not facts and bridge.get("character_events"):
        facts = {"character_events": list(bridge.get("character_events") or [])}
    base = int(bridge.get("based_on_storyline_revision") or 0)
    cur = int(getattr(tl, "storyline_revision", 0) or 0) if tl is not None else 0
    run = reconcile_run(plot=plot, bridge=bridge,
                        based_on_storyline_revision=base, current_revision=cur,
                        chapter_num=int(chapter_num or 0))
    arc_goal, thread = _plot_context_projection(tl, plot)
    return {
        "plot_id": pid,
        "plot_name": getattr(plot, "name", "") if plot else bridge.get("plot_name", ""),
        "roles": list(getattr(plot, "roles", None) or []) if plot else list(bridge.get("roles") or []),
        # 对照区左栏要写「已提交 · 第 N 章」基准戳：章节号此前只喂给 reconcile_run，没回传。
        "chapter_num": int(chapter_num or 0),
        "run_id": bridge.get("run_id") or run.get("run_id") or "",
        "predictions": list(getattr(plot, "character_impact", None) or []) if plot else [],
        "expected_facts": (bridge.get("expected_facts")
                           or (list(getattr(plot, "expected_facts", None) or []) if plot else [])),
        "facts": facts,
        "reconcile": run,
        # 历史弧/线程只来自该 plot 在正式故事线中的归属；不从当前规划反推。
        "arc_goal": arc_goal,
        "thread": thread,
    }


_COMPARE_FACT_FIELD = {
    "goal_shift": "goal",
    "power_shift": "power_level",
    "location_shift": "location",
    "arc_stage": "arc_stage",
    "relationship": "relationship_to_mc",
    "trust_change": "relationship_to_mc",
}
_COMPARE_FIELD_LABELS = {
    "goal": "目标",
    "power_level": "实力",
    "relationship_to_mc": "关系",
    "arc_stage": "弧阶段",
    "location": "位置",
}
def _build_compare_projection(previous, current, chapter_num=0):
    """把相邻情节段投影成一个可对齐的左右矩阵（纯展示数据，不写盘）。

    previous/current 都来自同一次 desk 聚合响应；这里不重新读取故事状态，也不从正文
    推断事实。两侧缺席、dyn 缺失和 from 缺失都保留为显式标记，交给 UI 做空态展示。
    """
    previous = previous if isinstance(previous, dict) else {}
    current = current if isinstance(current, dict) else {}
    facts = previous.get("facts") or {}
    events = list(facts if isinstance(facts, list) else facts.get("character_events") or [])
    prev_chars = {}
    prev_predictions = {}

    def prev_char(name):
        return prev_chars.setdefault(name, {"name": name, "fields": {}, "events": []})

    for group in events:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or group.get("character") or "角色").strip()
        item = prev_char(name)
        for event in group.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "note")
            field = _COMPARE_FACT_FIELD.get(event_type)
            value = event.get("to")
            if value in (None, ""):
                value = event.get("reason")
            entry = {
                "type": event_type,
                "value": str(value) if value not in (None, "") else "",
                "from": str(event.get("from")) if event.get("from") not in (None, "") else "",
                "missing_from": event.get("from") in (None, ""),
            }
            item["events"].append(entry)
            if field and entry["value"]:
                item["fields"][field] = entry

    cp = current.get("cast_pack") or {}
    current_cards = []
    for group_name, cards in (("protagonists", cp.get("protagonists") or []),
                              ("active", cp.get("active") or []),
                              ("referenced", cp.get("referenced") or [])):
        for card in cards:
            if not isinstance(card, dict) or not str(card.get("name") or "").strip():
                continue
            current_cards.append((group_name, card))
    current_chars = {}
    for group_name, card in current_cards:
        name = str(card.get("name")).strip()
        if name in current_chars:
            continue
        dyn = card.get("dyn") if isinstance(card.get("dyn"), dict) else None
        current_chars[name] = {
            "name": name,
            "role": card.get("identity") or card.get("role") or "",
            "group": group_name,
            "fields": dict(dyn or {}),
            "state_missing": dyn is None and group_name != "referenced",
            "state_source": card.get("state_source") or "",
        }

    expected_by_char = {}
    for expected in ((current.get("plot") or {}).get("expected_facts") or []):
        if not isinstance(expected, dict):
            continue
        name = str(expected.get("subject") or expected.get("name") or expected.get("character") or "角色").strip()
        target = expected.get("expected_to")
        if target in (None, ""):
            target = expected.get("to")
        if target in (None, ""):
            continue
        field = _COMPARE_FACT_FIELD.get(str(expected.get("type") or ""))
        if not field:
            continue
        expected_by_char.setdefault(name, {})[field] = {
            "type": str(expected.get("type") or "note"),
            "value": str(target),
            "strength": expected.get("strength") or "",
        }
    # 预测中出现、但 cast_pack 尚未列出的人物也属于本段对照对象；保留为无动态状态的占位。
    for name in expected_by_char:
        current_chars.setdefault(name, {
            "name": name, "role": "", "group": "active", "fields": {},
            "state_missing": True, "state_source": "",
        })
    for role in ((current.get("plot") or {}).get("roles") or []):
        name = str(role).strip()
        if name:
            current_chars.setdefault(name, {
                "name": name, "role": "", "group": "active", "fields": {},
                "state_missing": True, "state_source": "",
            })
    # 上一段只有自然语言预测/结构化预测而没有事实时，仍保留人物名，避免左右行消失。
    for prediction in previous.get("predictions") or []:
        if isinstance(prediction, dict):
            name = str(prediction.get("name") or prediction.get("character") or prediction.get("subject") or "角色").strip()
            if name:
                prev_char(name)
                text = (prediction.get("prediction") or prediction.get("expected_change")
                        or prediction.get("intent") or prediction.get("change") or prediction.get("description") or "")
                if text:
                    prev_predictions[name] = str(text)
    for expected in previous.get("expected_facts") or []:
        if isinstance(expected, dict):
            name = str(expected.get("subject") or expected.get("name") or expected.get("character") or "角色").strip()
            if name:
                prev_char(name)
    for role in previous.get("roles") or []:
        name = str(role).strip()
        if name:
            prev_char(name)

    names = []
    for name in list(current_chars) + list(prev_chars):
        if name not in names:
            names.append(name)

    def side(value=None, **extra):
        result = dict(extra)
        if value not in (None, ""):
            result["value"] = str(value)
        return result

    def character_rows():
        """每角色一行：目标/实力/关系 全部内联进同一行的两侧单元格。

        不按字段拆行（一个人物只占一行），不显示身份角色标签（如「主角」）；
        字段以 fields 数组随行下发，由前端在同一行内紧凑渲染。
        """
        rows = []
        fields = ("goal", "power_level", "relationship_to_mc")
        for name in names:
            old = prev_chars.get(name)
            new = current_chars.get(name)
            # 仅被提及、没有动态或预期变化的 referenced 角色不进入默认对照，
            # 避免把出场名单渲染成一排“状态未记录”的空行；完整人物上下文仍在 plot_run。
            if (new and new.get("group") == "referenced" and not old
                    and not (expected_by_char.get(name) or new.get("fields"))):
                continue
            old_exists = old is not None
            new_exists = new is not None
            previous_side, current_side = {}, {}
            if old_exists:
                previous_side = {"detail": prev_predictions.get(name, "")}
                prev_fields = []
                for field in fields:
                    old_field = (old or {}).get("fields", {}).get(field) or {}
                    if not old_field:
                        continue
                    prev_fields.append({
                        "label": _COMPARE_FIELD_LABELS[field],
                        "value": str(old_field.get("value") or ""),
                        "missing_from": bool(old_field.get("missing_from")),
                        "event_type": old_field.get("type", ""),
                    })
                if prev_fields:
                    previous_side["fields"] = prev_fields
                if not previous_side.get("fields") and not previous_side.get("detail"):
                    previous_side["role"] = "上一段出现"
            if new_exists:
                current_side = {
                    "state_source": (new or {}).get("state_source", ""),
                    "state_missing": (new or {}).get("state_missing", False),
                }
                curr_fields = []
                for field in fields:
                    new_value = (new or {}).get("fields", {}).get(field)
                    expected = (expected_by_char.get(name) or {}).get(field) or {}
                    if new_value in (None, "") and not expected:
                        continue
                    entry = {"label": _COMPARE_FIELD_LABELS[field],
                             "value": str(new_value) if new_value not in (None, "") else ""}
                    if expected:
                        entry["expected_to"] = expected.get("value", "")
                        entry["expected_type"] = expected.get("type", "")
                        entry["expected_strength"] = expected.get("strength", "")
                    curr_fields.append(entry)
                if curr_fields:
                    current_side["fields"] = curr_fields
            expected_fields = expected_by_char.get(name) or {}
            changed_fields = any(
                str(((old or {}).get("fields", {}).get(field) or {}).get("value", "")) !=
                str(((new or {}).get("fields", {}).get(field) or ""))
                for field in fields
            )
            rows.append({
                "key": "character:" + name,
                "entity": name,
                "label": "人物",
                "previous": previous_side,
                "current": current_side,
                "status": "both" if old_exists and new_exists else ("previous" if old_exists else "current"),
                "actionable": bool(expected_fields or changed_fields or not old_exists or not new_exists),
            })
        return rows

    def location_rows():
        """按人物保留位置变化：只显示有真实位置或明确预计去向的角色。

        地点没有独立 registry；把人物名保留在 entity 上，避免“旧港 → 新城”丢失归属。
        """
        def value_of(value):
            if isinstance(value, dict):
                return value.get("value") or value.get("to") or ""
            return value if value not in (None, "") else ""

        rows = []
        for name in names:
            old = prev_chars.get(name) or {}
            new = current_chars.get(name) or {}
            old_value = value_of((old.get("fields") or {}).get("location"))
            new_value = value_of((new.get("fields") or {}).get("location"))
            expected = (expected_by_char.get(name) or {}).get("location") or {}
            expected_value = value_of(expected)
            if old_value in (None, "") and new_value in (None, "") and expected_value in (None, ""):
                continue
            previous_side = side(old_value) if old_value not in (None, "") else {}
            current_side = side(new_value,
                                state_source=new.get("state_source", ""),
                                state_missing=new.get("state_missing", False)) if new_value not in (None, "") else {}
            if expected_value not in (None, ""):
                current_side["expected_to"] = str(expected_value)
                current_side["expected_type"] = expected.get("type", "")
            rows.append({
                "key": "location:" + name,
                "entity": name,
                "label": "地点",
                "previous": previous_side,
                "current": current_side,
                "status": "both" if previous_side and current_side else ("previous" if previous_side else "current"),
                "actionable": bool(expected_value not in (None, "") or
                                    (old_value not in (None, "") and new_value not in (None, "")
                                     and str(old_value) != str(new_value))),
            })
        return rows

    def plot_rows():
        plot = current.get("plot") or {}
        brief = current.get("execution_brief") or {}
        # Plot 名称已经在上方“已发生/待写”表头显示，这里只保留真正指导写作的字段。
        rows = []
        for key, label in (("dramatic_goal", "戏剧目标"), ("conflict_source", "冲突来源"),
                           ("character_choice", "人物选择"), ("irreversible_change", "不可逆变化"),
                           ("reader_question", "读者问题"), ("ending_hook", "结尾钩子")):
            if brief.get(key):
                rows.append({"key": "plot:" + key, "entity": "", "label": label,
                             "previous": {}, "current": side(brief.get(key))})
        impact = current.get("character_impact") or plot.get("character_impact") or []
        for index, item in enumerate(impact):
            if isinstance(item, dict):
                value = item.get("prediction") or item.get("expected_change") or item.get("change") or item.get("description") or ""
                entity = item.get("name") or item.get("character") or item.get("subject") or ""
            else:
                value, entity = str(item), ""
            if value:
                rows.append({"key": "plot:impact:" + str(index), "entity": entity,
                             "label": "人物变化预期", "previous": {}, "current": side(value)})
        for key, label in (("choices_made", "已作选择"), ("information_revealed", "已知信息"),
                           ("relationship_changes", "关系变化"), ("resource_changes", "资源变化")):
            old_value = facts.get(key) if isinstance(facts, dict) else None
            if isinstance(old_value, list):
                old_value = "；".join(str(x.get("text") or x.get("description") or x.get("title") or x)
                                      if isinstance(x, dict) else str(x) for x in old_value)
            elif isinstance(old_value, dict):
                old_value = old_value.get("text") or old_value.get("description") or old_value.get("title") or old_value.get("status") or ""
            if old_value:
                rows.append({"key": "plot:previous:" + key, "entity": "", "label": label,
                             "previous": side(old_value), "current": {}})
        return [row for row in rows if row["previous"] or row["current"]]

    def pair_records(old_values, new_values, prefix, text_fn):
        """按稳定 id 配对两侧记录；旧 payload 没有 id 时退回规范化文本。"""
        def make_record(value, index, seen):
            stable_id = str(value.get("id") or "").strip() if isinstance(value, dict) else ""
            text = re.sub(r"\s+", " ", str(text_fn(value) or "").strip()).lower()
            base = prefix + ":id:" + stable_id if stable_id else prefix + ":text:" + (text or str(index))
            count = seen.get(base, 0)
            seen[base] = count + 1
            key = base if count == 0 else base + ":" + str(count + 1)
            return {"key": key, "id": stable_id, "text": text, "value": value}

        old_seen = {}
        old_records = [make_record(value, i, old_seen) for i, value in enumerate(old_values or [])]
        new_seen = {}
        new_records = [make_record(value, i, new_seen) for i, value in enumerate(new_values or [])]
        by_id = {item["id"]: item for item in new_records if item["id"]}
        by_text = {}
        for item in new_records:
            if item["text"] and item["text"] not in by_text:
                by_text[item["text"]] = item
        used = set()
        pairs = []
        for old in old_records:
            match = by_id.get(old["id"]) if old["id"] else None
            if match is None and old["text"]:
                candidate = by_text.get(old["text"])
                if candidate is not None and old["id"] and candidate["id"] and old["id"] != candidate["id"]:
                    candidate = None
                match = candidate
            if match is not None and match["key"] not in used:
                used.add(match["key"])
                pairs.append((match["key"], old["value"], match["value"]))
            else:
                pairs.append((old["key"], old["value"], None))
        pairs.extend((item["key"], None, item["value"]) for item in new_records if item["key"] not in used)
        return pairs

    def promise_rows():
        old_values = facts.get("promise_updates") if isinstance(facts, dict) else []
        new_values = current.get("promise_state") or []
        old_values = old_values if isinstance(old_values, list) else [old_values] if old_values else []
        new_values = new_values if isinstance(new_values, list) else []

        def text(value):
            if isinstance(value, dict):
                return (value.get("desc") or value.get("description") or value.get("title")
                        or value.get("status") or "")
            return str(value) if value not in (None, "") else ""

        rows = []
        for key, old_value, new_value in pair_records(old_values, new_values, "promise", text):
            old_text, new_text = text(old_value), text(new_value)
            if not old_text and not new_text:
                continue
            label = (new_value or old_value or {}).get("title") if isinstance(new_value or old_value, dict) else ""
            label = str(label or "承诺")
            old_status = old_value.get("status", "") if isinstance(old_value, dict) else ""
            new_status = new_value.get("status", "") if isinstance(new_value, dict) else ""
            rows.append({"key": key, "entity": "", "label": label,
                         "previous": side(old_text, status=old_status) if old_text else {},
                         "current": side(new_text, status=new_status) if new_text else {},
                         "status": "both" if old_text and new_text else ("previous" if old_text else "current"),
                         "actionable": old_status != new_status or not old_text or not new_text})
        return rows

    def question_rows():
        old_values = facts.get("new_story_questions") if isinstance(facts, dict) else []
        current_planning = current.get("planning") or {}
        new_values = current_planning.get("story_questions") or []
        old_values = old_values if isinstance(old_values, list) else [old_values] if old_values else []
        new_values = new_values if isinstance(new_values, list) else [new_values] if new_values else []

        terminal = {"answered", "superseded", "resolved", "closed"}
        def text(value):
            if isinstance(value, dict):
                return value.get("question") or value.get("title") or value.get("text") or ""
            return str(value) if value not in (None, "") else ""
        def active(value):
            return not (isinstance(value, dict) and str(value.get("status") or "").lower() in terminal)

        rows = []
        for key, old_value, new_value in pair_records(old_values, new_values, "question", text):
            old_text = text(old_value) if active(old_value) else ""
            new_text = text(new_value) if active(new_value) else ""
            if not old_text and not new_text:
                continue
            old_status = old_value.get("status", "") if isinstance(old_value, dict) else ""
            new_status = new_value.get("status", "") if isinstance(new_value, dict) else ""
            rows.append({"key": key, "entity": "", "label": "问题",
                         "previous": side(old_text, status=old_status) if old_text else {},
                         "current": side(new_text, status=new_status) if new_text else {},
                         "status": "both" if old_text and new_text else ("previous" if old_text else "current"),
                         "actionable": old_status != new_status or not old_text or not new_text})
        return rows

    def arc_thread_rows():
        old_arc = previous.get("arc_goal") or {}
        new_arc = current.get("arc_goal") or {}
        old_thread = previous.get("thread") or {}
        new_thread = current.get("thread") or {}
        rows = []

        def arc_value(value):
            if not isinstance(value, dict):
                return ""
            name = str(value.get("name") or "").strip()
            stage = str(value.get("stage_name") or "").strip()
            desc = str(value.get("stage_desc") or "").strip()
            return " · ".join(x for x in (name, stage or desc) if x)

        def thread_value(value):
            if not isinstance(value, dict):
                return ""
            name = str(value.get("name") or value.get("id") or "").strip()
            note = str(value.get("chain_note") or "").strip()
            if not note and (value.get("written_count") is not None or value.get("unwritten_count") is not None):
                note = "已写 %s 条 / 未写 %s 条" % (value.get("written_count", 0), value.get("unwritten_count", 0))
            return " · ".join(x for x in (name, note) if x)

        old_arc_text, new_arc_text = arc_value(old_arc), arc_value(new_arc)
        if old_arc_text or new_arc_text:
            rows.append({"key": "arc:context", "entity": "", "label": "弧阶段",
                         "previous": side(old_arc_text, status="已发生归属") if old_arc_text else {},
                         "current": side(new_arc_text, status="本段计划") if new_arc_text else {},
                         "status": "both" if old_arc_text and new_arc_text else ("previous" if old_arc_text else "current"),
                         "actionable": old_arc_text != new_arc_text})
        old_thread_text, new_thread_text = thread_value(old_thread), thread_value(new_thread)
        if old_thread_text or new_thread_text:
            rows.append({"key": "thread:context", "entity": "", "label": "叙事线程",
                         "previous": side(old_thread_text, status="已发生归属") if old_thread_text else {},
                         "current": side(new_thread_text, status="本段承接") if new_thread_text else {},
                         "status": "both" if old_thread_text and new_thread_text else ("previous" if old_thread_text else "current"),
                         "actionable": old_thread_text != new_thread_text})
        return rows

    sections = []
    for key, title, rows in (("plot", "情节段", plot_rows()),
                             ("characters", "人物", character_rows()),
                             ("locations", "场景位置", location_rows()),
                             ("arc_thread", "弧与线程", arc_thread_rows()),
                             ("promises", "承诺", promise_rows()),
                             ("questions", "待解问题", question_rows())):
        if rows:
            protagonist = next((row for row in rows if row.get("entity") in current_chars
                                and current_chars[row["entity"]].get("group") == "protagonists"), None)
            reconcile = previous.get("reconcile") or {}
            previous_count = sum(1 for row in rows if row.get("previous"))
            current_count = sum(1 for row in rows if row.get("current"))
            change_count = sum(1 for row in rows if row.get("actionable") or row.get("change_kind")
                               or any(isinstance(field, dict) and field.get("expected_to")
                                      for side_name in ("previous", "current")
                                      for field in ((row.get(side_name) or {}).get("fields") or [])))
            # 首屏只打开当前写作简报；人物/位置/承诺/问题/弧线程统一在完整审计中按需查看。
            default_visible = key == "plot"
            sections.append({
                "id": key,
                "title": title,
                "summary": (str(change_count) + " 项变化") if change_count else "",
                "previous_count": previous_count,
                "current_count": current_count,
                "change_count": change_count,
                "default_visible": default_visible,
                "collapsed_preview": protagonist if key == "characters" else rows[0],
                "rows": rows,
                # reconcile 描述的是上一 Plot 的整体对账，只在主情节段组显示一次，
                # 其它组保留行级变化而不重复刷同一枚徽章。
                "status": {"kind": reconcile.get("kind", ""), "stale": bool(reconcile.get("stale"))}
                           if reconcile and key == "plot" else {},
                "detail": reconcile.get("summary", "") if reconcile and key == "plot" else "",
            })
    return {
        "previous": {"plot_id": previous.get("plot_id") or "",
                     "plot_name": previous.get("plot_name") or previous.get("plot_id") or "",
                     "chapter_num": int(previous.get("chapter_num") or 0),
                     "reconcile": previous.get("reconcile") or {}},
        "current": {"plot_id": (current.get("plot") or {}).get("id") or "",
                    "plot_name": (current.get("plot") or {}).get("name") or "",
                    "chapter_num": int(chapter_num or 0)},
        "sections": sections,
        "has_previous": bool(previous.get("plot_id") or previous.get("plot_name") or previous.get("facts")),
        "has_current": bool(current.get("plot")),
    }


@bp.route("/api/desk/chapters/<book_id>")
def desk_chapters_api(book_id):
    """写作台正文 JSON：从磁盘现读已写章节+草稿（供前端轮询刷新右侧，修「agent 写完不显示」）。

    附带 plot_run：当前 Plot Run 上下文（下一个未写情节段 + 弧目标 + 线程状态 + 承诺 +
    字数余量），以及 comparison：上一段/本段共享行模型的三列展示投影。组装逻辑与 Agent
    侧共用 agent_tools._build_plot_run/_next_plot，保证 UI 显示的就是 agent 实际会写的那一段。
    """
    cur = 0
    try:
        d = json.load(open(os.path.join(str(book_mgr.dir), book_id, "book.json"), encoding="utf-8"))
        cur = int(d.get("current_chapter") or 0)
    except Exception:
        pass
    plot_run = None
    recent_plot_outcome = None
    planning = {}
    try:
        from agent_tools import _build_plot_run, _draft_plot_ids, _next_plot, _runtime_projection
        from libraries.storyline import load_storyline
        tl = load_storyline(_storyline_filepath(book_id))
        draft = None
        dp = os.path.join(str(book_mgr.dir), book_id, "draft_chapter.json")
        if os.path.exists(dp):
            with open(dp, encoding="utf-8") as fh:
                draft = json.load(fh)
        runtime = _runtime_projection(book_id, tl, book_mgr.get(book_id), draft)
        written_now = runtime["display_written_words"]
        if tl is not None:
            p = _next_plot(tl, draft)
            if p is not None:
                from agent_tools import _load_char_states, _staged_cast_projection
                plot_run = _build_plot_run(tl, p, _load_char_states(book_id), draft=draft)
                # 与 writer 同源：character_states 只在收章落账，章内已上报未收章的变化必须在这里
                # 覆盖上去（并打 state_source="staged_fact"），否则 UI 显示的人物状态比 Agent 看到的旧。
                from libraries.plot_run_state import load_staged
                plot_run["cast_pack"] = _staged_cast_projection(
                    plot_run.get("cast_pack") or {}, load_staged(book_id))
            event_bridge = next((b for b in reversed((draft or {}).get("bridges") or [])
                                 if isinstance(b, dict) and b.get("plot_id")
                                 and (b.get("facts") or b.get("character_events"))), None)
            if event_bridge:
                recent_plot_outcome = _plot_outcome(event_bridge, tl,
                                                    int((draft or {}).get("chapter_num") or 0))
            from libraries.planning_state import load_planning_state, detect_story_boundary
            disk_book = book_mgr.get(book_id)
            ps = load_planning_state(book_id, tl, disk_book, persist=False)
            draft_ids = _draft_plot_ids(draft or {})
            remaining = sum(1 for item in (tl.plots or [])
                            if not (getattr(item, "written_chapter", 0) or 0) and item.id not in draft_ids)
            planning = {
                "storyline_revision": int(getattr(tl, "storyline_revision", 0) or 0),
                "target_word_budget": int(ps.get("target_word_budget") or 0),
                "committed_until_word": int(ps.get("committed_until_word") or 0),
                "written_until_word": written_now,
                "committed_words": runtime["committed_words"],
                "draft_words": runtime["draft_words"],
                "raw_codepoints": runtime["committed_raw_codepoints"] + runtime["draft_raw_codepoints"],
                "current_plot": runtime["current_plot"],
                # 写作台轮询与规划面板使用同一份导航数据，避免一个接口显示
                # “未规划”、另一个接口已有 H1/H2 的短暂分裂。
                "horizon": ps.get("horizon") or {},
                "future_intents": ps.get("future_intents") or [],
                "story_questions": ps.get("story_questions") or [],
                "character_intents": ps.get("character_intents") or [],
                "last_replan": ps.get("last_replan") or {},
                "boundary": detect_story_boundary(
                    written_until_word=written_now,
                    committed_until_word=int(ps.get("committed_until_word") or 0),
                    remaining_plots=remaining,
                    words_per_batch=int(tl.words_per_chapter or 3000),
                    storyline_revision=int(getattr(tl, "storyline_revision", 0) or 0),
                    last_replan=ps.get("last_replan") or {},
                ),
            }
    except Exception as e:
        logging.getLogger(__name__).warning("组装 plot_run 失败: %s", e)
    chapters = _chapters_from_disk(book_id, cur)
    if recent_plot_outcome is None:
        last_ch, last_bridge = None, None
        for ch in reversed(chapters):
            for b in reversed(ch.get("bridges") or []):
                if isinstance(b, dict) and b.get("plot_id") and (b.get("facts") or b.get("character_events")):
                    last_ch, last_bridge = ch, b
                    break
            if last_bridge:
                break
        if last_bridge:
            _tlc = tl if 'tl' in locals() else None
            recent_plot_outcome = _plot_outcome(last_bridge, _tlc, (last_ch or {}).get("num") or 0)
    # 单快照 UI 投影：Past / Current / Future 与审计信息来自同一 revision。
    rev = int((planning or {}).get("storyline_revision") or 0)
    current_plot = (plot_run or {}).get("plot") or {}
    future_horizon = (planning or {}).get("horizon") or {}
    past = {
        "plot_id": (recent_plot_outcome or {}).get("plot_id", ""),
        "plot_name": (recent_plot_outcome or {}).get("plot_name", ""),
        "reconcile": (recent_plot_outcome or {}).get("reconcile") or {},
    }
    future = {"plots": list(future_horizon.get("h1") or [])[:2],
              "horizon": future_horizon, "questions": (planning or {}).get("story_questions") or []}
    # 审计面取 **令牌账本**（prepare 时签发的权威取证记录）而非 raw `_build_plot_run`——
    # 后者不产出 context_fingerprint/sample_receipt，会让取证面板恒空。
    audit_record, tool_events = {}, []
    try:
        from libraries.plot_commit_tokens import latest_audit_record
        audit_record = latest_audit_record(book_id, plot_id=str(current_plot.get("id") or ""),
                                           storyline_revision=rev) or {}
        if not audit_record:      # 当前 Plot 尚无令牌（未 prepare）：放宽到最近一条
            audit_record = latest_audit_record(book_id) or {}
        if audit_record.get("flow_id"):
            from libraries.dsh_bridge import get_task_events
            want = {"prepare_plot_run", "save_plot_draft"}
            tool_events = [
                {"name": e.get("name") or e.get("tool") or "", "ts": e.get("ts"),
                 "ok": e.get("ok", True), "child_run_id": e.get("child_run_id") or ""}
                for e in get_task_events(limit=300)
                if (e.get("name") or e.get("tool")) in want
                and (not e.get("flow_id") or e.get("flow_id") == audit_record.get("flow_id"))
            ][-12:]
    except Exception as _exc:  # noqa: BLE001
        log.warning("写作台审计面取令牌记录失败 book=%s: %s", book_id, _exc)
    audit = {
        "context_fingerprint": audit_record.get("context_fingerprint", ""),
        "storyline_revision": rev,
        "execution_brief": (plot_run or {}).get("execution_brief") or {},
        "cast_pack": (plot_run or {}).get("cast_pack") or {},
        "sample_receipt": audit_record.get("sample_receipt") or {},
        "token": {"plot_id": audit_record.get("plot_id", ""), "accepted": audit_record.get("accepted"),
                  "issued_at": audit_record.get("issued_at"), "accepted_at": audit_record.get("accepted_at")},
        "flow_id": audit_record.get("flow_id", ""), "child_run_id": audit_record.get("child_run_id", ""),
        "tool_events": tool_events, "reconcile": past.get("reconcile") or {},
    }
    comparison = _build_compare_projection(
        recent_plot_outcome,
        {**(plot_run or {}), "planning": planning},
        cur,
    )
    return jsonify({"book_id": book_id, "current_chapter": cur,
                    "chapters": chapters,
                    "plot_run": plot_run, "recent_plot_outcome": recent_plot_outcome,
                    "comparison": comparison,
                    "planning": planning,
                    "state_revision": rev,
                    "past": past,
                    "current": {"plot": current_plot, "active_run": (plot_run or {}).get("run") or None,
                                 "next_plot": current_plot if current_plot else None},
                    "future": future,
                    "selection": {"plan_plot_id": "", "bridge_plot_id": ""},
                    "audit": audit})


@bp.route("/books/storyline/write/<engine_id>")
def storyline_write_flow(engine_id):
    """蓝图式写作流程页（新核心）"""
    engine = _engines.get(engine_id)
    if not engine:
        return "引擎会话已过期", 404
    # 已写章节（供中栏「章节正文」预载，作为书目内容连续展示）
    chapters = []
    initial_written_words = 0
    initial_raw_codepoints = 0
    book = getattr(engine, "book", None)
    if book and book.book_id:
        # 跨进程 stale：MCP/dsh 子进程写盘后，用磁盘 book.json 的最新 current_chapter 修正缓存
        try:
            d = json.load(open(os.path.join(str(book_mgr.dir), book.book_id, "book.json"), encoding="utf-8"))
            cur = int(d.get("current_chapter") or 0)
            if cur > 0:
                book.current_chapter = cur
        except Exception:
            pass
    if book and (book.current_chapter or 0) >= 1:
        try:
            for n in range(1, book.current_chapter + 1):
                ch = book_mgr.load_chapter(book.book_id, n)
                if ch and ch.get("content"):
                    from core.text_utils import count_prose_units
                    initial_written_words += count_prose_units(ch.get("content") or "")
                    initial_raw_codepoints += len(ch.get("content") or "")
                    chapters.append({
                        "num": n,
                        "title": ch.get("title") or f"第{n}章",
                        "content": ch.get("content") or "",
                        "bridges": ch.get("bridges") or [],
                        "word_count": int(ch.get("word_count") or count_prose_units(ch.get("content") or "")),
                    })
        except Exception as e:
            logger.warning("加载已写章节失败: %s", e)
    # 进行中的章节草稿：与已固化章节同格式渲染（bridges 逐情节段 span.m-bridge），
    # 让刚写完的情节段在写作台上即时可见、可点击高亮；切章固化（_clear_draft）后自动消失。
    try:
        draft = engine._load_draft() if hasattr(engine, "_load_draft") else None
    except Exception as e:
        draft = None
        logger.warning("加载进行中草稿失败: %s", e)
    if book and draft:
        bridges = draft.get("bridges") or []
        buffer = draft.get("buffer") or []
        dn = int(draft.get("chapter_num") or 0)
        # 已固化章节跳过（防陈旧草稿重复渲染）；旧草稿无 bridges 时回退 buffer 纯文本展示
        if dn > (book.current_chapter or 0) and (bridges or buffer):
            chapters.append({
                "num": dn,
                "title": "（写作中）",
                "content": "\n\n".join((b.get("text") or "") for b in bridges) if bridges
                           else "\n\n".join(buffer),
                "bridges": bridges,
                "draft": True,
            })
            from core.text_utils import count_prose_units
            initial_written_words += count_prose_units("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer))
            initial_raw_codepoints += len("\n\n".join((b.get("text") or "") for b in bridges) if bridges else "\n\n".join(buffer))
    sl = getattr(engine, "storyline", None)
    # 将正文桥接段按统一口径回填到故事线视图；规划对象本身不落盘修改。
    storyline_data = sl.to_dict() if sl else None
    if storyline_data:
        from core.text_utils import count_prose_units
        actual_plot_words = {}
        for chapter in chapters:
            for bridge in chapter.get("bridges") or []:
                pid = str(bridge.get("plot_id") or "")
                if pid:
                    actual_plot_words[pid] = actual_plot_words.get(pid, 0) + count_prose_units(bridge.get("text") or "")
        for plot in storyline_data.get("plots") or []:
            pid = str(plot.get("id") or "")
            plot["actual_words"] = int(actual_plot_words.get(pid, 0))
    # 字数轴：总章数优先用引擎已字数化的 state.total_chapters，否则由情节段 planned_words 推导
    total_ch = getattr(getattr(engine, "state", None), "total_chapters", 0) or 0
    if not total_ch and sl:
        try:
            from libraries.storyline_writer import planned_words
            _w = sum(planned_words(p) for p in sl.plots) if sl.plots else 0
            _wpc = (sl.words_per_chapter or 3000)
            total_ch = max(1, (_w + _wpc - 1) // _wpc)
        except Exception:
            total_ch = 0
    from libraries.storyline import basic_info_world_done
    world_done = basic_info_world_done((sl.basic_info if sl else None))
    planning_state = {}
    planning_boundary = {}
    if sl and book:
        try:
            from libraries.planning_state import load_planning_state, detect_story_boundary
            planning_state = load_planning_state(book.book_id, sl, book, persist=False)
            # 页面首屏必须使用磁盘章节的实时字数；Web 进程中的 book 对象可能是 Agent
            # 写作前加载的旧快照。故事线和轮询接口仍以领域状态为准。
            planning_state = dict(planning_state or {})
            planning_state["written_until_word"] = int(initial_written_words)
            remaining_plots = sum(1 for p in (sl.plots or []) if not (getattr(p, "written_chapter", 0) or 0))
            planning_boundary = detect_story_boundary(
                written_until_word=int(initial_written_words),
                committed_until_word=int(planning_state.get("committed_until_word") or 0),
                remaining_plots=remaining_plots,
                words_per_batch=int(sl.words_per_chapter or 3000),
                storyline_revision=int(getattr(sl, "storyline_revision", 0) or 0),
                last_replan=planning_state.get("last_replan") or {},
            )
        except Exception:
            planning_state, planning_boundary = {}, {}
    return render_template("storyline_write_flow.html",
        engine_id=engine_id,
        state=engine.state,
        storyline=sl,
        storyline_data=storyline_data,
        book=book,
        chapters=chapters,
        total_ch=total_ch,
        world_done=world_done,
        planning_state=planning_state,
        planning_boundary=planning_boundary,
        initial_written_words=initial_written_words,
        initial_raw_codepoints=initial_raw_codepoints,
    )


# ⚠️ 已废弃：旧 NovelEngine 逐步写作入口（当前写作走侧栏 dsh + 服务端 FSM）。（无前端引用；删除属 API 面变更，待单独确认）
@bp.route("/api/storyline-engine/<engine_id>/step", methods=["POST"])
def storyline_engine_step(engine_id):
    """蓝图引擎：按故事线写下一章（新书前三章 / 续写任意章节通用）"""
    from plugins import task_manager

    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404

    is_continue = engine.state.book_mode == BookMode.CONTINUE
    task_id = f"engine_{engine_id}"
    next_ch = engine.state.current_chapter + 1
    total_ch = engine.state.total_chapters or next_ch
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)
    book = getattr(engine, "book", None)
    book_id = getattr(book, "book_id", "") or ""
    book_title = getattr(book, "title", "") or ""

    # 注册/更新任务（新书生成 / 续写写作）
    task_name = "续写写作" if is_continue else "新书生成"
    if next_ch == 1:
        task_manager.ensure_single(task_name)
        task_manager.start(task_id, name=task_name,
                          title=engine.state.pen_name or "",
                          phase=f"第{next_ch}章...", url=flow_url)
    else:
        task_manager.progress(task_id, current=min(next_ch, total_ch), phase=f"第{next_ch}章...")
    task_manager.log(task_id, f"蓝图写作：第{next_ch}章", "info")

    # 全书完成（章节数到顶）
    if next_ch > total_ch:
        task_manager.done(task_id, message="全书完成")
        return jsonify({"status": "done", "flow_complete": True, "reason": "已写完全部章节"})

    inst = Instruction(Op.WRITE_STORYLINE_CHAPTER, chapter_num=next_ch)
    result = engine.execute(inst)
    if result.get("error"):
        task_manager.fail(task_id, str(result["error"]))
        return jsonify({"error": result["error"]}), 500
    task_manager.log(task_id, f"第{next_ch}章完成 {result.get('word_count', 0)}字", "success")

    return jsonify({
        "op": "write_storyline_chapter",
        "chapter_num": next_ch,
        "status": result.get("status"),
        "word_count": result.get("word_count", 0),
        "beats": result.get("beats", 0),
        "blueprint": result.get("blueprint", {}),
        "cost": result.get("cost", 0),
        "flow_complete": next_ch >= total_ch,
    })


# ⚠️ 已废弃：旧整章 SSE 写作入口（当前写作走侧栏 dsh + 服务端 FSM）。（无前端引用；删除属 API 面变更，待单独确认）
@bp.route("/api/storyline-engine/<engine_id>/write-chapter", methods=["POST"])
def storyline_engine_write_chapter_sse(engine_id):
    """蓝图引擎：流式写一章（SSE）。逐情节段下发 plot_start / plot_done / chapter_done。

    前端据此在右侧逐情节段展示步骤与正文，并高亮左侧故事线对应的大纲/情节段。
    """
    import json as _json
    from plugins import task_manager
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404
    chapter_num = engine.state.current_chapter + 1
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)
    _book = getattr(engine, "book", None)
    _book_id = getattr(_book, "book_id", "") or ""
    _book_title = getattr(_book, "title", "") or ""

    def generate():
        task_manager.ensure_single("整章写作")
        task_id = f"writechap_{engine_id}_{int(time.time())}"
        task_manager.start(task_id, name="整章写作",
                           title=_book_title or "",
                           url=flow_url)
        try:
            for evt in engine._write_storyline_chapter_stream(chapter_num):
                if isinstance(evt, dict):
                    t = evt.get("type", "")
                    if t == "plot_chunk":
                        pass
                    elif t == "chapter_done":
                        task_manager.done(task_id,
                                          message=f"第{evt.get('chapter', chapter_num)}章完成")
                    elif t == "error":
                        task_manager.fail(task_id, str(evt.get("message", "写入失败")))
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())


# ⚠️ 已废弃：旧单情节段 SSE 写作入口（当前写作走侧栏 dsh + 服务端 FSM）。（无前端引用；删除属 API 面变更，待单独确认）
@bp.route("/api/storyline-engine/<engine_id>/write-bridge", methods=["POST"])
def storyline_engine_write_bridge_sse(engine_id):
    """蓝图引擎：流式写「一个」情节段（SSE，新核心·按情节段撰写）。

    事件：bridge_start / group_chunk / bridge_done / chapter_done / complete。
    写一个情节段即返回；连续点击则继续写下一个未写情节段，本章满字数自动切章。
    """
    import json as _json
    from plugins import task_manager
    engine = _engines.get(engine_id)
    if not engine:
        return jsonify({"error": "not found"}), 404
    flow_url = url_for("desk.storyline_write_flow", engine_id=engine_id)
    _book = getattr(engine, "book", None)
    _book_id = getattr(_book, "book_id", "") or ""
    _book_title = getattr(_book, "title", "") or ""

    def generate():
        task_manager.ensure_single("情节段写作")
        task_id = f"writebrg_{engine_id}_{int(time.time())}"
        task_manager.start(task_id, name="情节段写作",
                           title=_book_title or "",
                           url=flow_url)
        try:
            for evt in engine._write_next_plot_stream():
                if isinstance(evt, dict):
                    t = evt.get("type", "")
                    if t == "group_chunk":
                        pass
                    elif t == "bridge_done":
                        pass
                    elif t in ("chapter_done", "complete"):
                        task_manager.done(task_id,
                                          message=f"第{evt.get('chapter', '')}章完成" if t == "chapter_done"
                                                  else evt.get("message", "全书完成"))
                    elif t == "error":
                        task_manager.fail(task_id, str(evt.get("message", "写入失败")))
                yield "data: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"
        except Exception as e:
            import traceback
            task_manager.fail(task_id, str(e))
            err = {"type": "error", "message": str(e),
                   "traceback": traceback.format_exc()}
            yield "data: " + _json.dumps(err, ensure_ascii=False) + "\n\n"

    return sse_stream_response(generate())





# ═══════════════════════════════════════════
# ♻️ 续写
@bp.route("/books/<book_id>/continue")
# ═══════════════════════════════════════════

def continue_book_page(book_id):
    """书续写 — 统一走故事线蓝图写作流程（新核心）"""
    book = book_mgr.get(book_id)
    if not book:
        return "图书不存在", 404
    llm = get_llm()
    if not llm:
        return jsonify({"error": "LLM 未配置"}), 500
    engine_id = f"cont_{book_id}"
    if engine_id not in _engines:
        try:
            engine = NovelEngine(llm_client=llm)
            engine.continue_book(book_id)
            _engines[engine_id] = engine
        except (ValueError, RuntimeError) as e:
            # 无故事线（旧书/未生成 storyline）：给出指引而非 500，
            # 避免「续写/进入写作台」在残缺书上直接崩溃。
            return render_template_string(
                '<div class="tle-layout"><h2>⚠️ 无法进入写作</h2>'
                '<p style="color:#8b949e">{{ msg }}</p>'
                '<p><a class="btn" style="background:#1f6feb;color:#fff;text-decoration:none" '
                'href="/books/{{ bid }}">📖 返回书详情</a> '
                '<a class="btn" style="background:#30363d;color:#c9d1d9;text-decoration:none" '
                'href="/books">📚 去书库</a></p></div>',
                msg=str(e), bid=book_id), 200
    return redirect(url_for("desk.storyline_write_flow", engine_id=engine_id))
