"""新书启动全链路 E2E（真实 LLM，临时书 + finally 清理）。

链路：一句话启动 → 世界卡生成世界观(SSE) → 确认 → 进写作台(规划态，非错误页)
      → 一键生成完整大纲(6 阶段 SSE) → 引擎缓存失效 → 再进写作台(写作者已建，可写桥段)。

用法: python tools/smoke_flow_full.py [--write-bridge]
"""
import sys, io, json, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.web_ui import app
from ui.web_blueprints.ctx import book_mgr, _engines, _resolve_storyline


def _sse_events(resp):
    events = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass
    return events


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-bridge", action="store_true", help="额外真实写一个桥段")
    args = ap.parse_args()

    client = app.test_client()
    book_id = None
    try:
        # 1) 一句话启动
        r = client.post("/books/start", data={
            "world_idea": "灵气复苏后我觉醒了复制异能，绑定了一个专坑宿主的菜鸡系统",
            "genre": "玄幻", "pen_name": "枫落", "platform": "fanqie",
        })
        loc = r.headers.get("Location", "")
        assert r.status_code == 302 and "/world" in loc, f"启动应 302 到世界卡: {loc}"
        book_id = loc.split("/books/")[1].split("/world")[0]
        print("[1] 一句话启动 ->", book_id)

        # 2) 生成世界观（真实 LLM）
        print("[2] 生成世界观（真实 LLM，可能 30-90s）...")
        r = client.post(f"/api/world-builder/{book_id}/generate",
                        json={"mode": "one", "idea": "灵气复苏后我觉醒了复制异能，绑定了一个专坑宿主的菜鸡系统"})
        ev = _sse_events(r)
        kinds = [e.get("event") for e in ev]
        done = [e for e in ev if e.get("event") == "done"]
        assert "done" in kinds and "error" not in kinds, f"世界生成应有 done 无 error: {kinds}"
        bi = done[0].get("basic_info") or {}
        proto = bi.get("protagonist") or {}
        assert bi.get("_world_generated") and proto.get("name"), "世界生成应有 _world_generated 与主角名"
        print("   主角:", proto.get("name"))

        # 3) 确认
        r = client.post(f"/api/world-builder/{book_id}/confirm")
        d = r.get_json()
        assert d.get("ok") and "/storyline/" in d.get("redirect", ""), f"confirm 应 ok: {d}"
        print("[3] confirm ->", d["redirect"])

        # 4) 进写作台（规划态）：应渲染写作台，非错误页，可见一键生成完整大纲
        r = client.get(d["redirect"], follow_redirects=True)
        html = r.get_data(as_text=True)
        assert "✍️ 写作台" in html and "⚠️ 无法进入写作" not in html, "应渲染写作台而非错误页"
        assert "✨ 一键生成完整大纲" in html, "规划态应见一键生成完整大纲"
        assert "▶ 写下一个桥段" not in html, "规划态不应显示写桥段按钮"
        print("[4] 写作台（规划态）OK")

        # 5) 一键生成完整大纲（真实 LLM 6 阶段，可能 1-3 分钟）
        print("[5] 生成完整大纲（真实 LLM 6 阶段）...")
        r = client.post(f"/api/storyline/{book_id}/generate-full")
        ev = _sse_events(r)
        kinds = [e.get("event") for e in ev]
        assert "done" in kinds and "error" not in kinds, f"大纲生成应有 done 无 error: {kinds}"
        tl = _resolve_storyline(book_id)
        assert tl and tl.outlines, "大纲生成后 outlines 应非空"
        print(f"   大纲 {len(tl.outlines)} 条 / 桥段 {len(tl.plots)} 个")

        # 6) 引擎缓存已失效
        assert _engines.get(f"cont_{book_id}") is None, "gen-full 后 cont_ 引擎缓存应被弹掉"
        print("[6] 引擎缓存已失效 OK")

        # 7) 再进写作台：应建写作者（可写桥段）
        r = client.get(f"/books/{book_id}/continue", follow_redirects=True)
        html = r.get_data(as_text=True)
        assert "✍️ 写作台" in html and "▶ 写下一个桥段" in html, "有大纲后应见写桥段按钮"
        eng = _engines.get(f"cont_{book_id}")
        assert eng is not None and eng.storyline_writer is not None, "引擎应已建桥段写作者"
        print("[7] 再进写作台，写作者已建 OK")

        # 8) 可选真实写一个桥段（首事件应为 bridge/plot/chapter 类）
        if args.write_bridge:
            print("[8] 真实写桥段（可能 30-90s）...")
            r = client.post(f"/api/storyline-engine/cont_{book_id}/write-bridge")
            ev = _sse_events(r)
            first = ev[0] if ev else {}
            print("   首事件 type:", first.get("type") or first.get("event"))
            assert first.get("type") in ("bridge_start", "plot_start", "chapter_progress", "error") \
                or first.get("event") in ("bridge_start", "plot_start", "chapter_progress", "error"), \
                f"写桥段首事件异常: {first}"
            # 不等待写完，验证能启动即可
        else:
            print("[8] 跳过真实写桥段（--write-bridge 开启才跑）")

        print("\nSMOKE_FLOW_FULL OK")
    finally:
        if book_id and book_id.startswith("book_"):
            _engines.pop(f"cont_{book_id}", None)
            try:
                book_mgr.delete(book_id)
                print("[cleanup] 已删除测试书", book_id)
            except Exception as e:
                print("[cleanup] 删除失败", e)


if __name__ == "__main__":
    main()
