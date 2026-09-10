"""replan 提交共享服务 —— UI(commit-plan HTTP)与 auto orchestrator(R5)共用的单一原子提交点。

业务规则只在此实现一遍(修订 2)：校验 preview 存在/版本一致/结构已通过 → BookLock →
复用 `agent_tools.save_outlines`(append + expected_revision + planning_patch + validate)在
**同一原子边界**里提交 storyline + planning_state(任一失败整份回滚)→ 成功删除 preview。

save_outlines 本身已在副本上完成全部结构变更与 validate、并在写盘失败时把两个文件都
回滚到旧值(见 agent_tools.save_outlines 的 try/except 回滚段)，因此这里无需重复任何规则。
"""
from __future__ import annotations


def _server_last_replan(book_id: str, from_revision: int, preview_id: str) -> dict:
    """续规划提交后由**服务端**记下的边界快照（写入 planning_state.last_replan）。

    计划器与调用方都不必（也不该）自己填这个字段：reason_codes 必须与服务端同一口径算出来
    （绝对轴已写字数 vs 承诺水位），否则去抖会拿「被人为缩小的 reasons」当子集，
    把本该续规划的状态误判成已处理。

    作用域说明：去抖只在 `from_revision == 当前 revision` 时生效，而提交本身会把 revision
    +1，所以它主要覆盖「同一版本内重复决策」的情形；本字段同时是 UI/排查用的
    「上次续规划在哪个版本、因为什么」记录。
    """
    from agent_tools import _draft_read, _runtime_written_words, book_mgr, load_tl
    from libraries.planning_state import detect_story_boundary, load_planning_state
    import time as _time
    tl = load_tl(book_id)
    book = book_mgr.get(book_id)
    if tl is None:
        return {}
    draft = _draft_read(book_id) or {}
    state = load_planning_state(book_id, tl, book, persist=False)
    drafted = {x.get("plot_id") for x in (draft.get("bridges") or [])}
    remaining = sum(1 for p in (tl.plots or [])
                    if not int(getattr(p, "written_chapter", 0) or 0) and p.id not in drafted)
    boundary = detect_story_boundary(
        written_until_word=_runtime_written_words(book_id, tl, book, draft),
        committed_until_word=int(state.get("committed_until_word") or 0),
        remaining_plots=remaining,
        words_per_batch=int(tl.words_per_chapter or 3000),
        storyline_revision=int(from_revision or 0),
        last_replan={})
    return {"from_revision": int(from_revision or 0),
            "reason_codes": list(boundary.get("reason_codes") or []),
            "at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "preview_id": str(preview_id or "")}


def commit_replan_preview(book_id: str, preview_id: str, expected_revision) -> dict:
    """原子提交 replan preview。

    返回 dict，含 `status`(HTTP 建议码)供端点直接转码；**成功判据请用显式字段 `commit_ok`**，
    不要用 `ok`（那是规划 UI 聚合载荷的 ok，与提交结果无关）：
      {commit_ok:True, ok:True, status:200, commit:<save_outlines 结果>, planning:<payload>}
      {ok:False, error:preview_not_found, status:404}
      {ok:False, error:invalid_input_shape|preview_revision_mismatch, status:400}
      {ok:False, error:preview_invalid, problems:[...], status:400}
      {ok:False, error:stale_storyline, expected, actual, action, status:409}
      {ok:False, error:commit_failed, message, status:400}
    锁失败抛 BookBusyError(与既有行为一致)。
    注：save_outlines 内的 storyline+planning_state 是**顺序写 + 异常补偿回滚**，不是崩溃安全事务
    （进程硬崩溃仍可能留下两文件不一致）；崩溃窗口与修复手段见 tools/migrate_runtime_v2.py。
    """
    from libraries.book_lock import BookBusyError, BookLock
    from libraries.planning_state import (delete_replan_preview, load_replan_preview)
    from agent_tools import save_outlines

    preview = load_replan_preview(book_id)
    if not preview or str(preview.get("preview_id") or "") != str(preview_id or ""):
        return {"ok": False, "error": "preview_not_found", "status": 404}
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        return {"ok": False, "error": "invalid_input_shape",
                "field": "expected_revision", "status": 400}
    stored_revision = preview.get("expected_revision")
    if isinstance(stored_revision, bool) or not isinstance(stored_revision, int):
        return {"ok": False, "error": "preview_missing_revision", "status": 400}
    if expected_revision != int(stored_revision):
        return {"ok": False, "error": "preview_revision_mismatch",
                "expected": stored_revision, "actual": expected_revision, "status": 400}
    validation = preview.get("validation") or {}
    if not validation.get("passed"):
        return {"ok": False, "error": "preview_invalid",
                "problems": validation.get("problems") or [], "status": 400}

    lock = BookLock(book_id)
    if not lock.acquire(timeout=30.0, purpose="commit_plan"):
        raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
    try:
        patch = dict(preview.get("planning_patch") or {})
        # 服务端补记边界快照（单一写点，UI commit-plan 与 auto 共用）。
        last_replan = _server_last_replan(book_id, expected_revision, preview.get("preview_id"))
        if last_replan:
            patch["last_replan"] = last_replan
        result = save_outlines(
            book_id=book_id, outlines=preview.get("outlines") or [],
            plots=preview.get("plots") or [], threads=preview.get("threads") or None,
            themes=preview.get("themes") or None, mode="append",
            expected_revision=expected_revision,
            planning_patch=patch,
            validate=True,
        )
    except Exception as e:
        return {"ok": False, "error": "commit_failed", "message": str(e), "status": 400}
    finally:
        lock.release()
    if not result.get("ok"):
        status = 409 if result.get("error") == "stale_storyline" else 400
        out = dict(result)
        out["status"] = status
        return out

    delete_replan_preview(book_id, preview.get("preview_id") or "")

    # 成功后回传最新 planning-ui payload(与 commit-plan 端点既有行为一致)
    payload = None
    try:
        from ui.web_blueprints.storyline import _planning_ui_payload
        payload = _planning_ui_payload(book_id) or {"ok": True}
    except Exception:
        payload = {"ok": True}
    payload["commit"] = result
    payload["commit_ok"] = True
    payload["status"] = 200
    return payload
