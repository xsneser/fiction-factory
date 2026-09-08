"""replan 提交共享服务 —— UI(commit-plan HTTP)与 auto orchestrator(R5)共用的单一原子提交点。

业务规则只在此实现一遍(修订 2)：校验 preview 存在/版本一致/结构已通过 → BookLock →
复用 `agent_tools.save_outlines`(append + expected_revision + planning_patch + validate)在
**同一原子边界**里提交 storyline + planning_state(任一失败整份回滚)→ 成功删除 preview。

save_outlines 本身已在副本上完成全部结构变更与 validate、并在写盘失败时把两个文件都
回滚到旧值(见 agent_tools.save_outlines 的 try/except 回滚段)，因此这里无需重复任何规则。
"""
from __future__ import annotations


def commit_replan_preview(book_id: str, preview_id: str, expected_revision) -> dict:
    """原子提交 replan preview。

    返回 dict，含 `status`(HTTP 建议码)供端点直接转码：
      {ok:True, status:200, commit:<save_outlines 结果>, planning:<planning-ui payload>}
      {ok:False, error:preview_not_found, status:404}
      {ok:False, error:invalid_input_shape|preview_revision_mismatch, status:400}
      {ok:False, error:preview_invalid, problems:[...], status:400}
      {ok:False, error:stale_storyline, expected, actual, action, status:409}
      {ok:False, error:commit_failed, message, status:400}
    锁失败抛 BookBusyError(与既有行为一致)。
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
    if expected_revision != int(preview.get("expected_revision", -1)):
        return {"ok": False, "error": "preview_revision_mismatch",
                "expected": preview.get("expected_revision"), "actual": expected_revision,
                "status": 400}
    validation = preview.get("validation") or {}
    if not validation.get("passed"):
        return {"ok": False, "error": "preview_invalid",
                "problems": validation.get("problems") or [], "status": 400}

    lock = BookLock(book_id)
    if not lock.acquire(timeout=30.0, purpose="commit_plan"):
        raise BookBusyError(f"另一进程正在操作这本书，请稍后再试：{book_id}")
    try:
        result = save_outlines(
            book_id=book_id, outlines=preview.get("outlines") or [],
            plots=preview.get("plots") or [], threads=preview.get("threads") or None,
            themes=preview.get("themes") or None, mode="append",
            expected_revision=expected_revision,
            planning_patch=preview.get("planning_patch") or {},
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
    payload["status"] = 200
    return payload
