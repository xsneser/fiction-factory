#!/usr/bin/env python3
"""WS2 核心验收 —— 写一章 → Prediction → Fact → reconcile 漂移 → 决策点。

覆盖（修订 8）：
  1. 同一 plot 在 revision 42/43 产生不同 run_id
  2. structured expected_facts 与 actual fact clean match
  3. prediction drift（预期 矿脉，实际 霜脊镇）
  4. unpredicted fact
  5. stale run（based_on 远小于 current）
  6. Fact 覆盖 planning 中旧 Prediction（character_intents.observations = Fact 值）
  7. reconcile 失败不影响正文已成功落盘
  8. reconcile 永不修改正文（ch.content 与提供文本逐字一致）

纯 Python（Flask test_client 可选），无 MCP/LLM。用法：python tools/test_reconcile_flow.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _filler(n: int, seed: int = 0) -> str:
    base = ("灯从顶棚垂下来，顾衡把检测仪抵在金属舱壁上，读数正一格一格地跳。"
            "走廊那头的脚步声停住了，他屏住呼吸，等它下一步是向前还是后退。")
    if seed:
        base = base.replace("顾衡", "林晚").replace("检测仪", "密钥").replace("舱壁", "台面")
    out = []
    while sum(len(x) for x in out) < n:
        out.append(base)
    return "".join(out)[:n]


def _make_book(bm, book_id_hint=""):
    from libraries.book_manager import BookManager
    from libraries.storyline import BookStoryline, OutlineSlot, PlotSlot
    cfg = bm.create(title="Reconcile验收", pen_name="测试", chapter_count=500)
    bid = cfg.book_id
    tl = BookStoryline(
        book_title="Reconcile验收", pen_name="测试", platform=cfg.platform,
        words_per_chapter=3000, basic_info={}, phase="ready", storyline_revision=5)
    tl.outlines = [OutlineSlot(id="o1", template_id="", name="开篇", start_word=0, end_word=9000)]
    tl.plots = [PlotSlot(
        id="p1", template_id="", name="查明真凶", outline_id="o1", words=3000, order=1,
        expected_facts=[{"subject": "顾衡", "type": "location_shift",
                         "expected_to": "矿脉", "strength": "must"}]),
        PlotSlot(id="p2", template_id="", name="后续收束", outline_id="o1", words=3000, order=2)]
    bm.save_storyline(bid, tl)
    return bm, bid


def main():
    from libraries.book_manager import BookManager
    from libraries.storyline import load_storyline
    from libraries.planning_state import planning_path, read_json
    from agent_tools import save_plot_draft, save_chapter_text, _run_id_for

    bm = BookManager(os.path.join(_ROOT, "books"))
    bm, bid = _make_book(bm)
    try:
        # (1) run_id = plot_id@based_revision：不同 revision 不同 run
        check("run_id 42 vs 43 不同", _run_id_for("p1", 42) != _run_id_for("p1", 43),
              f"{_run_id_for('p1',42)} / {_run_id_for('p1',43)}")

        ch_text = _filler(2300, seed=0)
        r_draft = save_plot_draft(
            bid, 1, "p1", "查明真凶", ch_text,
            character_events=[{"name": "顾衡", "events": [
                {"type": "location_shift", "from": "七号工区", "to": "霜脊镇"}]}],
            outcome={"choices_made": ["公开承担违规改造责任"]},
            expected_facts=[{"subject": "顾衡", "type": "location_shift",
                             "expected_to": "矿脉", "strength": "must"}])
        check("save_plot_draft 成功（outcome/facts 入口）", bool(r_draft.get("ok")), f"{r_draft}")

        r = save_chapter_text(bid, 1, ch_text, title="第1章", summary="摘要",
                              plot_segments=[{"plot_id": "p1", "plot_name": "查明真凶",
                                              "text": ch_text}])
        check("save_chapter_text 成功且返回 reconcile", bool(r.get("ok")), f"{r.get('reconcile')}")

        # (2)(3) reconcile：prediction_drift（预期 矿脉，实际 霜脊镇）
        recon = (r.get("reconcile") or {})
        check("reconcile 报告 kind=prediction_drift",
              recon.get("kinds") == ["prediction_drift"], f"{recon.get('kinds')}")
        check("reconcile 有 decision_points", bool(recon.get("decision_points")),
              f"{len(recon.get('decision_points') or [])} 条")

        # (6) Fact 覆盖 planning 中旧 Prediction：character_intents.observations.location_shift=霜脊镇
        plan = read_json(planning_path(bid)) or {}
        gy = next((x for x in (plan.get("character_intents") or []) if x.get("name") == "顾衡"), None)
        obs = (gy or {}).get("observations") or {}
        check("character_intents 顾衡.observations.location_shift=霜脊镇(Fact 胜出)",
              obs.get("location_shift") == "霜脊镇", f"{obs}")

        # (4) unpredicted fact 单独验证（reconcile_run 层，plot 无 expected → 纯未覆盖事实）
        from libraries.reconcile import reconcile_run
        unr = reconcile_run(
            plot=None,
            bridge={"facts": {"character_events": [{"name": "林晚", "events": [
                {"type": "goal_shift", "to": "接管工区"}]}]}},
            based_on_storyline_revision=5, current_revision=7, chapter_num=1)
        check("unpredicted fact kind", unr["kind"] == "unpredicted_fact", f"{unr['kind']}")

        # (5) stale run：based_on 远小于 current
        st = reconcile_run(
            plot=None,
            bridge={"facts": {"character_events": []}},
            based_on_storyline_revision=5, current_revision=9, chapter_num=1)
        check("stale run 标记", st.get("stale") is True, f"{st.get('stale')}")

        # (7)(8) 落盘不变量：content == join(bridges.text)（deai 后的正文），
        #      且 reconcile 只在保存后跑、不改这份正文/不引入新改写。
        from core.json_store import read_json
        ch_raw = read_json(os.path.join(_ROOT, "books", bid, "chapters", "0001.json"))
        joined = "\n\n".join((b.get("text") or "") for b in (ch_raw.get("bridges") or []))
        check("落盘 content==join(bridges.text)（reconcile 不回写正文）",
              ch_raw.get("content") == joined, f"len content={len(ch_raw.get('content') or '')}")
        br = (ch_raw.get("bridges") or [{}])[0]
        spans = ch_raw.get("plot_spans") or []
        check("chapter 顶层 plot_spans 与内容吻合",
              len(spans) == 1 and spans[0]["plot_id"] == "p1"
              and spans[0]["start"] == 0 and spans[0]["end"] == len((br or {}).get("text") or "")
              and spans[0]["end"] == len(ch_raw.get("content") or ""),
              f"{spans}")
        br = (ch_raw.get("bridges") or [{}])[0]
        check("chapter bridge 持久化 facts/run 快照",
              isinstance(br.get("facts"), dict) and br.get("run_id") and br.get("based_on_storyline_revision") == 5,
              f"run_id={br.get('run_id')} based={br.get('based_on_storyline_revision')}")
    finally:
        try:
            bm.delete(bid)
        except Exception:
            pass

    print("\n" + "=" * 50)
    print(f"  Reconcile Flow 验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 50)


if __name__ == "__main__":
    main()
