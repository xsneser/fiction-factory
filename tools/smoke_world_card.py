"""世界观设定卡 Web 冒烟测试（真实 LLM 生成一次，随后清理测试书）。

用法: python tools/smoke_world_card.py
覆盖：POST /books/start（一句话）→ GET 世界卡 → borrow-preview → 真实 generate(SSE)
      → 校验 done.basic_info → confirm → 清理。
"""
import sys, io, json, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.web_ui import app
from ui.web_blueprints.ctx import book_mgr


def run_smoke():
    client = app.test_client()
    book_id = None
    try:
        # 1) 一句话启动 → 应 302 到书详情页（设定已并入详情页）
        r = client.post("/books/start", data={
            "world_idea": "灵气复苏后我觉醒了复制异能，绑定了一个专坑宿主的菜鸡系统",
            "genre": "玄幻", "pen_name": "枫落", "platform": "fanqie",
        })
        loc = r.headers.get("Location", "")
        print("[1] POST /books/start ->", r.status_code, loc)
        assert r.status_code == 302 and loc.startswith("/books/"), "应重定向到书详情页"
        assert "/world" not in loc, "不再跳独立世界卡页"
        book_id = loc.rstrip("/").split("/books/")[1] if "/books/" in loc else None
        assert book_id, "解析 book_id 失败"

        # 2) GET 书详情页 → 应含内嵌设定编辑表单；/world 旧入口 302 回详情
        r = client.get(f"/books/{book_id}")
        html = r.get_data(as_text=True)
        print("[2] GET 详情页 ->", r.status_code, "| 内嵌设定表单:", 'id="world-idea"' in html)
        assert r.status_code == 200 and 'id="world-idea"' in html, "详情页应内嵌设定表单"
        rw = client.get(f"/books/{book_id}/world")
        print("    /world 旧入口 ->", rw.status_code, "| Location:", rw.headers.get("Location", ""))
        assert rw.status_code == 302 and f"/books/{book_id}" in rw.headers.get("Location", ""), "旧 /world 应 302 回详情"

        # 3) borrow-preview（借用 book_001 的种子，不调 LLM）
        r = client.post(f"/api/world-builder/{book_id}/borrow-preview",
                        json={"source_book_id": "book_001"})
        d = r.get_json()
        print("[3] borrow-preview ->", d.get("ok"), "| seed 含:", list((d.get("seed") or {}).keys())[:3])
        assert d.get("ok") and d.get("seed")

        # 4) 真实生成世界观（SSE 流式）
        print("[4] 生成世界观（真实 LLM，可能 30-90s）...")
        r = client.post(f"/api/world-builder/{book_id}/generate",
                        json={"mode": "one", "idea": "灵气复苏后我觉醒了复制异能，绑定了一个专坑宿主的菜鸡系统"})
        assert r.status_code == 200
        events = []
        for line in r.get_data(as_text=True).splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except Exception:
                    pass
        kinds = [e.get("event") for e in events]
        print("   SSE 事件:", kinds)
        assert "done" in kinds and "error" not in kinds, "生成应有 done 且无 error"
        done_evt = [e for e in events if e.get("event") == "done"][0]
        bi = done_evt.get("basic_info") or {}
        wb = bi.get("world_building") or {}
        proto = bi.get("protagonist") or {}
        filled = sum(1 for k in ("era", "power_system", "geography", "culture",
                                 "history", "social_structure", "core_conflict")
                     if str(wb.get(k, "") or "").strip())
        print("   _world_generated:", bi.get("_world_generated"),
              "| 主角:", proto.get("name"), "| 世界观维度填充:", filled, "/7")
        assert bi.get("_world_generated") is True
        assert proto.get("name"), "主角名应非空"
        assert filled >= 3, f"扩展维度应较充实，实际 {filled}"

        # 5) confirm → 应返回跳转，且 _world_generated 落盘
        r = client.post(f"/api/world-builder/{book_id}/confirm")
        d = r.get_json()
        print("[5] confirm ->", d.get("ok"), "| redirect:", d.get("redirect"))
        assert d.get("ok") and "/storyline/" in d.get("redirect", "")

        # 6) GET 确认后的跳转目标 → 应渲染写作台（规划态，非错误页，无写桥段按钮）
        r = client.get(d["redirect"], follow_redirects=True)
        html = r.get_data(as_text=True)
        print("[6] GET", d["redirect"], "->", r.status_code,
              "| 写作台:", "✍️ 写作台" in html, "| 错误页:", "⚠️ 无法进入写作" in html,
              "| 一键生成完整大纲:", "✨ 一键生成完整大纲" in html,
              "| 写桥段按钮(应无):", "▶ 写下一个桥段" in html)
        assert r.status_code == 200, "写作台应可渲染"
        assert "✍️ 写作台" in html, "应渲染写作台"
        assert "⚠️ 无法进入写作" not in html, "不应是错误页"
        assert "✨ 一键生成完整大纲" in html, "规划态应见一键生成完整大纲按钮"
        assert "▶ 写下一个桥段" not in html, "规划态不应显示写桥段按钮（已门控）"

        print("\nSMOKE OK")
    finally:
        if book_id and book_id.startswith("book_"):
            try:
                book_mgr.delete(book_id)
                print("[cleanup] 已删除测试书", book_id)
            except Exception as e:
                print("[cleanup] 删除失败", e)


if __name__ == "__main__":
    run_smoke()
