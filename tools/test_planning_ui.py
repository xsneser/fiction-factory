"""增量规划 UI HTTP 契约：聚合、确认提交、陈旧版本拒绝。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import save_outlines
from libraries.planning_state import save_replan_preview
from libraries.storyline import BookStoryline
from ui.web_blueprints.ctx import book_mgr
from ui.web_ui import app


book = book_mgr.create(title="规划 UI 测试", pen_name="test", genre="玄幻")
bid = book.book_id
try:
    book_mgr.save_storyline(bid, BookStoryline(book_title=book.title, pen_name=book.pen_name, phase="ready"))
    first = save_outlines(
        bid, outlines=[{"id": "a1", "name": "第一弧", "start_word": 0, "end_word": 3000}],
        plots=[{"id": f"p{i}", "name": f"段{i}", "outline_id": "a1", "words": 1000} for i in range(1, 4)],
        mode="replace", expected_revision=0,
        planning_patch={"committed_until_word": 3000, "future_intents": ["远方威胁"]},
    )
    assert first["storyline_revision"] == 1
    preview = save_replan_preview(bid, {
        "expected_revision": 1,
        "diagnosis": {"current_pressure": "承诺区将尽"},
        "directions": [{"id": "d1", "title": "迎战"}, {"id": "d2", "title": "撤退"}],
        "selected_direction_id": "d1",
        "outlines": [{"id": "a2", "name": "第二弧", "start_word": 3000, "end_word": 6000}],
        "plots": [{"id": f"p{i}", "name": f"段{i}", "outline_id": "a2", "words": 1000} for i in range(4, 7)],
        "planning_patch": {"committed_until_word": 6000, "future_intents": ["更远威胁"]},
        "validation": {"passed": True, "problems": []},
    })
    client = app.test_client()
    state = client.get(f"/api/storyline/{bid}/planning-state")
    assert state.status_code == 200 and state.json["replan_preview"]["preview_id"] == preview["preview_id"]
    committed = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": preview["preview_id"], "expected_revision": 1,
    })
    assert committed.status_code == 200 and committed.json["storyline_snapshot"]["revision"] == 2
    stale = save_replan_preview(bid, {**preview, "preview_id": "stale-preview", "expected_revision": 1})
    rejected = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": stale["preview_id"], "expected_revision": 1,
    })
    assert rejected.status_code == 409 and rejected.json["error"] == "stale_storyline"
    assert book_mgr.load_storyline(bid).storyline_revision == 2
    # 校验失败必须拦截：validation.passed=False 的预览禁止确认，正式 storyline/planning 均不变
    bad = save_replan_preview(bid, {**preview, "preview_id": "bad-preview", "expected_revision": 2,
                                    "validation": {"passed": False, "problems": ["弧 r2 跨度与现有弧重叠"]}})
    blocked = client.post(f"/api/storyline/{bid}/commit-plan", json={
        "preview_id": bad["preview_id"], "expected_revision": 2,
    })
    assert blocked.status_code == 400 and blocked.json["error"] == "preview_invalid"
    assert blocked.json["problems"] == ["弧 r2 跨度与现有弧重叠"]
    assert book_mgr.load_storyline(bid).storyline_revision == 2
    print("[OK] planning UI aggregate / commit / stale revision / invalid preview blocked")

    # 旧书降级：无 planning_state.json 的书 → 聚合接口返回默认空态，不落盘、不报错
    legacy = book_mgr.create(title="规划 UI 旧书", pen_name="test", genre="都市")
    lbid = legacy.book_id
    try:
        book_mgr.save_storyline(lbid, BookStoryline(book_title=legacy.title, pen_name=legacy.pen_name, phase="ready"))
        from libraries.planning_state import planning_path as _ppath
        assert not _ppath(lbid).exists()
        state = client.get(f"/api/storyline/{lbid}/planning-state")
        assert state.status_code == 200 and state.json["ok"]
        ps = state.json["planning_state"]
        assert ps["written_until_word"] == 0 and ps["committed_until_word"] == 0
        assert state.json["storyline_snapshot"]["revision"] == 0
        assert not _ppath(lbid).exists()  # persist=False 只读聚合，不建文件
        print("[OK] legacy book planning-state empty state / no file created")
    finally:
        book_mgr.delete(lbid)
finally:
    book_mgr.delete(bid)
