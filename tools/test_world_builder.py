"""世界观/设定生成器质量测试（真实 LLM + 纯逻辑）。

用法: python tools/test_world_builder.py
覆盖：
  1. 示例候选 generate_candidates() — 一次 2-3 个、各含 one_liner/world_brief
  2. 一句话链式 generate() — 扩展维度 ≥5、主角名非空、_world_generated=True、description 保留
  3. 借鉴变体 extract_seed + generate(seed) — 生成成功、世界观继承源书维度
  4. OutlineGenerator skip_analyze — 假 LLM 计数断言 _analyze_story 不被调用、扩展字段保留
  5. merge_basic_info 单元 — 扩展字段 + description 老字段混用不丢

无 LLM 时跳过 1-3，仅跑 4-5。
"""
import sys, io, json, os, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []
def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))


def have_llm():
    return os.path.exists("api.json")


def main():
    print("=" * 60)
    print("World Builder Test")
    print("=" * 60)

    from libraries.storyline import BookStoryline, merge_basic_info
    from libraries.prompt_harness import PromptHarness

    # ── 4) OutlineGenerator skip_analyze（纯逻辑，假 LLM 计数）──
    print("\n[4] OutlineGenerator skip_analyze")
    from libraries.outline_generator import OutlineGenerator

    class CountingLLM:
        def __init__(self):
            self.calls = 0
        def stream_deltas(self, *a, **k):
            self.calls += 1
            raise AssertionError("LLM 不应在 skip_analyze=True 时被调用")

    tl = BookStoryline(genre="玄幻")
    tl.basic_info = {
        "_world_generated": True,
        "world_building": {"era": "灵气复苏后2030年", "power_system": "异能九级",
                           "geography": "九大灵域", "culture": "觉醒者学院",
                           "core_conflict": "家族争权", "rules": ["一条"], "factions": ["灵调局"]},
        "protagonist": {"name": "林野", "identity": "学生"},
    }
    fake = CountingLLM()
    gen = OutlineGenerator(llm_client=fake)
    events = list(gen.generate(genre="玄幻", storyline=tl, skip_analyze=True, max_outlines=1))
    prog = [e[1] for e in events if e[0] == "progress"]
    check("skip 后不调 LLM", fake.calls == 0, f"calls={fake.calls}")
    check("Phase1 跳过提示", any("复用已生成" in p for p in prog))
    check("扩展字段保留", tl.basic_info["world_building"].get("geography") == "九大灵域")
    check("_basic_info_is_rich", gen._basic_info_is_rich(tl.basic_info) is True)

    # ── 5) merge_basic_info 单元 ──
    print("\n[5] merge_basic_info 单元")
    existing = {"world_building": {"description": "一句话种子", "era": ""},
                "protagonist": {"name": "张三"}}
    generated = {"world_building": {"era": "灵气复苏后2030年", "geography": "九大灵域",
                                    "world_summary": "概述"},
                 "protagonist": {"name": "李四", "gender": "男"},
                 "tone": "轻松"}
    merged = merge_basic_info(existing, generated)
    wb = merged["world_building"]
    check("description 老字段保留", wb.get("description") == "一句话种子", str(wb.get("description")))
    check("扩展维度加入", wb.get("geography") == "九大灵域")
    # 角色已统一为 characters 数组（protagonist/supporting_cast 迁移）
    merged_chars = merged.get("characters", [])
    mc = merged_chars[0] if merged_chars else {}
    check("用户已填字段不覆盖", mc.get("name") == "张三", f"name={mc.get('name')}")

    # ── 真实 LLM 部分 ──
    if not have_llm():
        print("\n(api.json 不存在，跳过真实 LLM 用例 1-3)")
    else:
        from ui.web_blueprints.ctx import get_llm
        from libraries.world_builder import WorldBuildingGenerator
        llm = get_llm()
        gen = WorldBuildingGenerator(llm_client=llm, harness=PromptHarness())
        idea = "灵气复苏后我觉醒了复制异能，绑定了一个专坑宿主的菜鸡系统"

        # 1) 示例候选
        print("\n[1] 示例候选")
        cands = gen.generate_candidates(genre="玄幻", idea=idea, count=3)
        check("候选 2-3 个", 2 <= len(cands) <= 3, f"count={len(cands)}")
        if cands:
            check("候选含 one_liner/world_brief",
                  all(c.get("one_liner") and c.get("world_brief") for c in cands))

        # 2) 一句话链式
        print("\n[2] 一句话链式生成")
        tl2 = BookStoryline(genre="玄幻", pen_name="枫落")
        tl2.basic_info["world_building"]["description"] = idea
        events = list(gen.generate(genre="玄幻", idea=idea, storyline=tl2))
        kinds = [e[0] for e in events]
        done = [e for e in events if e[0] == "done"]
        check("有 world_summary 事件", "world_summary" in kinds)
        check("有 done 且无 error", done and "error" not in kinds, str(kinds))
        if done:
            bi = done[0][2]["basic_info"]
            w = bi["world_building"]
            filled = sum(1 for k in ("era", "power_system", "geography", "culture",
                                     "history", "social_structure", "core_conflict")
                         if str(w.get(k, "") or "").strip())
            check("扩展维度 ≥5", filled >= 5, f"filled={filled}")
            check("主角名非空", bool((bi["protagonist"] or {}).get("name")))
            check("_world_generated", bi.get("_world_generated") is True)
            check("description 种子保留", w.get("description") == idea,
                  str(w.get("description", ""))[:20])

            # 3) 借鉴变体
            print("\n[3] 从已有书借鉴")
            seed = WorldBuildingGenerator.extract_seed(bi)
            check("extract_seed 抽到 world_building", "world_building" in seed)
            seed_txt = WorldBuildingGenerator.seed_to_text(seed)
            check("seed_to_text 非空", bool(seed_txt))
            tl3 = BookStoryline(genre="玄幻")
            ev3 = list(gen.generate(genre="玄幻", idea="主角改成穿越来的程序员",
                                    seed_basic_info=seed, storyline=tl3))
            done3 = [e for e in ev3 if e[0] == "done"]
            check("借鉴生成成功", done3 and "error" not in [e[0] for e in ev3])
            if done3:
                bi3 = done3[0][2]["basic_info"]
                w3 = bi3["world_building"]
                filled3 = sum(1 for k in ("era", "power_system", "geography", "culture",
                                          "history", "social_structure", "core_conflict")
                              if str(w3.get(k, "") or "").strip())
                check("借鉴后维度 ≥3", filled3 >= 3, f"filled={filled3}")
                check("主角名非空", bool((bi3["protagonist"] or {}).get("name")))

    # ── 汇总 ──
    print("\n" + "=" * 60)
    passed = sum(1 for x in RESULTS if x)
    print(f"PASSED {passed}/{len(RESULTS)}")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
