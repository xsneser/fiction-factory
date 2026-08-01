# -*- coding: utf-8 -*-
"""
大纲生成质量测试：真实调用 LLM 跑 5 阶段管线，检查产出质量。
用法: python tools/test_outline_quality.py [--genre 玄幻] [--sub 重生] [--context "..."]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NOVEL_ENGINE_DIR"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.llm_client import LLMClient
from core.models import APIConfig
from libraries.outline_generator import OutlineGenerator
from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--genre", default="都市")
    ap.add_argument("--sub", default="系统流")
    ap.add_argument("--context", default="落魄程序员绑定『打工人系统』，靠加班获得超能力，逆袭都市。开篇30章爽感拉满，后续转向灵气复苏大阴谋。")
    ap.add_argument("--pen", default="测试笔名")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    cfg_data = json.load(open("api.json", encoding="utf-8"))
    api_cfg = APIConfig(
        api_key=cfg_data.get("api_key", ""),
        base_url=cfg_data.get("base_url", "https://api.deepseek.com"),
        model=cfg_data.get("model", "deepseek-chat"),
        http_timeout_seconds=cfg_data.get("http_timeout_seconds", 300),
        verify_ssl=cfg_data.get("verify_ssl", True),
    )
    llm = LLMClient(api_cfg)

    gen = OutlineGenerator(
        llm_client=llm,
        structure_lib=StructureLibrary(),
        plot_lib=PlotLibrary(),
        gag_lib=GagLibrary(),
        theme_lib=ThemeLibrary(),
        profile=None,
    )

    t0 = time.time()
    phase_log = []
    decision_count = {"outline_choice": 0, "plot_choice": 0, "gag_review": 0, "validate": 0}
    total_thinking_chars = 0
    result_timeline = None
    issues = None

    for event_type, message, data in gen.generate(
        genre=args.genre, sub_genre=args.sub,
        custom_context=args.context, pen_name=args.pen,
        words_per_chapter=3000, max_outlines=5,
    ):
        if event_type in ("phase", "phase_done", "warnings"):
            phase_log.append((event_type, message, data))
        elif event_type == "decision":
            kind = message or "?"  # decision 事件的 kind 在 message 位（如 outline_choice）
            decision_count[kind] = decision_count.get(kind, 0) + 1
            if kind in ("outline_choice", "plot_choice") and data.get("chosen"):
                chosen = data["chosen"]
                if isinstance(chosen, dict):
                    print(f"  [决策/{kind}] {data.get('step','')} → 「{chosen.get('name','')}」 理由:{(data.get('reason') or '')[:40]}")
                elif isinstance(chosen, list):
                    print(f"  [决策/{kind}] {data.get('step','')} → 「{'、'.join(c.get('name','') for c in chosen)}」")
        elif event_type == "thinking":
            total_thinking_chars += len((data or {}).get("stream", ""))
        elif event_type == "done":
            result_timeline = data.get("timeline")
            issues = data.get("stats", {}).get("issues")
        elif event_type == "error":
            print("❌ 生成错误:", message)
            print((data or {}).get("traceback", "")[-1000:])
            return 1

    elapsed = time.time() - t0
    print(f"\n===== 耗时 {elapsed:.1f}s | 流式思考累计 {total_thinking_chars} 字 =====")
    for evt, msg, data in phase_log:
        if evt == "phase":
            print(f"  ▶ {msg}")

    if not result_timeline:
        print("❌ 未得到结果")
        return 1

    # ── 质量检查 ──
    outlines = result_timeline.get("outlines", [])
    plots = result_timeline.get("plots", [])
    themes = result_timeline.get("themes", [])
    bi = result_timeline.get("basic_info", {})
    print(f"\n===== 产出概览 =====")
    print(f"母题: {themes}")
    print(f"主角: {bi.get('protagonist', {})}")
    print(f"大纲 {len(outlines)} 条:")
    max_end = 0
    for o in outlines:
        max_end = max(max_end, o.get("end_chapter", 0))
        stages = o.get("stages", [])
        print(f"  - {o.get('name')} [第{o.get('start_chapter')}-{o.get('end_chapter')}章] 过渡={o.get('transition_type')} 阶段{len(stages)}个")
        for s in stages[:3]:
            evs = "、".join(s.get("events", [])[:3])
            print(f"      · {s.get('name')}（{s.get('min_ch')}-{s.get('max_ch')}章）: {evs}")
    print(f"桥段 {len(plots)} 个:")
    by_outline = {}
    for p in plots:
        by_outline.setdefault(p.get("outline_id"), []).append(p)
    for o in outlines:
        pl = by_outline.get(o.get("id"), [])
        print(f"  - {o.get('name')}: {len(pl)} 个桥段")
        for p in pl[:4]:
            print(f"      · {p.get('name')} [{p.get('category')}] 笑点{len(p.get('gag_ids',[]))} 内涵{p.get('theme_hints')}")
    total_gags = sum(len(p.get("gag_ids", [])) for p in plots)
    print(f"\n笑点总数: {total_gags} | 决策事件: {decision_count} | 校验建议: {issues}")

    # ── 保存 ──
    if not args.save:
        args.save = f"books/timelines/test_quality_{args.genre}_{int(time.time())}.json"
    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    with open(args.save, "w", encoding="utf-8") as f:
        json.dump(result_timeline, f, ensure_ascii=False, indent=2)
    print(f"已保存: {args.save}")

    # ── 质量判读 ──
    print("\n===== 质量检查 =====")
    probs = []
    if not outlines:
        probs.append("没有任何大纲")
    if max_end < 10:
        probs.append(f"全书仅覆盖 {max_end} 章，偏短")
    if not plots:
        probs.append("没有任何桥段")
    for o in outlines:
        if not o.get("stages"):
            probs.append(f"大纲「{o.get('name')}」没有阶段")
    if total_gags == 0:
        probs.append("没有任何笑点注入")
    if probs:
        print("⚠️ ", "；".join(probs))
    else:
        print("✅ 大纲=故事线结构完整（大纲→阶段→桥段→笑点/内涵）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
