#!/usr/bin/env python3
"""无书大纲预览验收 — generate_outline_preview 核心逻辑（OutlineGenerator 无书跑管线）零 LLM 成本。

验证 2026-08-23 建书流程重构（步3内先生成大纲+桥段，书创建即 phase=ready）：
  1) 无书模式（on_save=None 纯内存）跑完整管线：outlines/plots 非空、phase=ready
  2) Phase 1 临时人物保留（种子主角不被 _default_basic_info 覆盖）
  3) annotate_plot_roles 幂等（跑两次 roles 一致）
  4) _outline_preview_text 序列化助手产出非空文本且含大纲名
  5) to_dict 含 generate_outline_preview 返回面键（outlines/plots/threads/themes/basic_info/phase）

用法：python tools/test_outline_preview.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from libraries.outline_generator import OutlineGenerator  # noqa: E402
from libraries.prompt_harness import PromptHarness  # noqa: E402
from libraries.storyline import BookStoryline, annotate_plot_roles  # noqa: E402
from agent_tools import struct_lib, plot_lib, gag_lib, _profile_for, _outline_preview_text  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}  {detail}")


def build_gen(tl):
    profile = _profile_for(tl)
    harness = PromptHarness(storyline=tl, profile=profile,
                            gag_lib=gag_lib, plot_lib=plot_lib)
    return OutlineGenerator(llm_client=None, structure_lib=struct_lib,
                            plot_lib=plot_lib, gag_lib=gag_lib,
                            profile=profile, harness=harness)


def run_preview():
    """复刻 generate_outline_preview 的无书流程（on_save=None 纯内存）。"""
    tl = BookStoryline(
        words_per_chapter=3000, pen_name="测试",
        basic_info={
            "characters": [{"name": "王小明", "role": "主角", "importance": 1,
                            "identity": "程序员", "golden_finger": "读心术"}],
            "world_building": {"description": "都市爽文开挂升级",
                               "tags": ["都市", "爽文"],
                               "core_conflict": "主角被系统选中，在都市中逆袭"},
            "tone": "", "target_audience": "", "pov": "第三人称", "era_language": "",
        },
    )
    gen = build_gen(tl)
    events = []
    for ev in gen.generate(
            custom_context="核心矛盾：主角被系统选中，在都市中逆袭；世界观：都市爽文开挂升级",
            pen_name="测试", storyline=tl, skip_analyze=False, on_save=None):
        events.append(ev)
    return tl, events


def main():
    tl, events = run_preview()
    d = tl.to_dict()

    check("outlines 非空（规则回退）", len(tl.outlines) >= 1, f"outlines={len(tl.outlines)}")
    check("plots 非空（规则回退）", len(tl.plots) >= 1, f"plots={len(tl.plots)}")
    check("phase=ready（无书管线完成置位）", tl.phase == "ready", f"phase={tl.phase}")
    check("Phase 1 临时人物保留（种子主角不被覆盖）",
          any(c.get("name") == "王小明" for c in (tl.basic_info or {}).get("characters", [])),
          f"chars={len((tl.basic_info or {}).get('characters', []))}")
    check("to_dict 含 generate_outline_preview 返回面键",
          all(k in d for k in ("outlines", "plots", "threads", "themes", "basic_info", "phase")),
          f"keys={sorted(d.keys())}")

    # annotate_plot_roles 幂等：跑两次 roles 一致
    annotate_plot_roles(tl)
    roles1 = [list(p.roles) for p in tl.plots]
    annotate_plot_roles(tl)
    roles2 = [list(p.roles) for p in tl.plots]
    check("annotate_plot_roles 幂等（两次 roles 一致）", roles1 == roles2,
          f"plots={len(tl.plots)}")

    # _outline_preview_text 序列化助手
    text = _outline_preview_text(d)
    check("_outline_preview_text 产出非空文本", bool(text.strip()), f"len={len(text)}")
    first_name = (d.get("outlines") or [{}])[0].get("name", "")
    check("_outline_preview_text 含大纲名", bool(first_name) and first_name in text,
          f"首行={text.splitlines()[0] if text else ''}")

    print("\n" + "=" * 50)
    print(f"  无书大纲预览验收: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("  失败项:", "、".join(FAIL))
        sys.exit(1)
    print("  ✅ 无书大纲预览验收通过（步3内生成大纲+桥段，书创建即 ready）")
    print("=" * 50)


if __name__ == "__main__":
    main()
