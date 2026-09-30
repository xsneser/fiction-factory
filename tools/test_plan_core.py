#!/usr/bin/env python3
"""规划流程内核契约测试。

两个入口（novel-build / novel-replan）共用同一段构思流程，本测试钉死：

  1. 两个 SKILL.md 的 `plan-core` 标记块**互相 byte-equal**；
  2. 该块与 `agent-sidecar/skills/_shared/plan-core.md` 正文 **byte-equal**；
  3. 内核块里**零工具名**——比"⊆ 两 profile 交集"更强：内核只写认知步骤，
     谁往里塞了能力名谁当场红（这也是两个 profile 唯一都不越界的写法）；
  4. 两个 SKILL.md 的**块外部分**照旧走现契约（引用 ⊆ 各自 profile）。

为什么要第 3 条：`referenced_tools()` 是整词匹配注册表名，连
"不要调 X"里的 X 也算引用（见 `libraries/skill_profile.py`），而 build 与 replan 的
工具面交集只有两个查询工具——任何一处真实能力名都会让其中一个入口的契约测试红。

用法：python tools/test_plan_core.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries.skill_profile import (  # noqa: E402
    PLAN_CORE_BEGIN, PLAN_CORE_END, PLAN_CORE_RELPATH, PLAN_CORE_SKILLS,
    SKILL_PROFILE_MAP, extract_plan_core_block, plan_core_mismatch, plan_core_path,
    plan_core_text, plan_core_tool_leaks, profile_allows, referenced_tools, skill_text,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def _outside_core(text: str) -> str:
    """剥掉标记块后的正文（各入口自己的 policy / 落点映射 / 提交护栏）。"""
    head, rest = text.split(PLAN_CORE_BEGIN, 1)
    _, tail = rest.split(PLAN_CORE_END, 1)
    return head + tail


def report() -> int:
    """供 test_skill_profile_contract.py 末尾调用；返回失败数。"""
    print("\n── 规划流程内核（plan-core）──")
    if not os.path.exists(plan_core_path()):
        check(f"内核文件存在（{PLAN_CORE_RELPATH}）", False, "缺失")
        return 1
    kernel = plan_core_text()

    # ① 两个入口的内核块互相一致，且与内核文件逐字节相同（含末尾换行）
    blocks = {}
    for skill in PLAN_CORE_SKILLS:
        text = skill_text(skill)
        block = extract_plan_core_block(text)
        check(f"{skill} 有 plan-core 标记块", block is not None,
              "" if block is not None else f"需要 `{PLAN_CORE_BEGIN}` / `{PLAN_CORE_END}` 各一次")
        if block is not None:
            blocks[skill] = block
    if len(blocks) == 2:
        a, b = (blocks[s] for s in PLAN_CORE_SKILLS)
        check("两个入口的内核块 byte-equal", a == b,
              f"长度 {len(a)} vs {len(b)}")
        check(f"内核块 == {PLAN_CORE_RELPATH}（byte-equal）", a == kernel,
              f"长度 {len(a)} vs 内核 {len(kernel)}（跑 `python tools/sync_plan_core.py`）")

    # ③ 内核零工具名
    leaks = plan_core_tool_leaks()
    check("内核块零工具名", not leaks,
          f"越界：{leaks}（内核只写认知步骤，能力名放各入口小节）" if leaks
          else f"(扫描 TOOL_REGISTRY 全部工具名，0 命中)")

    # ④ 块外部分照旧 ⊆ 各自 profile（内核块本身已由第 3 条保证不引用任何工具）
    for skill in PLAN_CORE_SKILLS:
        profile = SKILL_PROFILE_MAP[skill]
        refs = referenced_tools(_outside_core(skill_text(skill)))
        missing = sorted(refs - set(profile_allows(profile)))
        check(f"{skill} 块外引用 ⊆ profile={profile}", not missing,
              f"越界：{missing}" if missing else f"(引用 {len(refs)} 个)")

    # 汇总：任何漂移都要能在一条消息里看清
    drift = plan_core_mismatch()
    check("plan_core_mismatch() 无漂移", not drift,
          "；".join(drift) if drift else "(0 项)")
    check("PLAN_CORE_SKILLS 覆盖两个入口", set(PLAN_CORE_SKILLS) <= set(SKILL_PROFILE_MAP),
          f"{list(PLAN_CORE_SKILLS)}")
    return len(FAIL)


def main():
    print("=" * 60)
    print("  规划流程内核契约（plan-core）")
    print("=" * 60)
    fails = report()
    print("\n" + "=" * 60)
    if fails:
        print(f"  ❌ {fails} 项失败")
        sys.exit(1)
    print("  ✅ 全部通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
