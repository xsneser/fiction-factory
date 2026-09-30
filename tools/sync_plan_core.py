#!/usr/bin/env python3
"""把规划流程内核刷进两个入口的 SKILL.md 标记块，再同步 `.dsh/skills` 镜像。

`agent-sidecar/skills/_shared/plan-core.md` 是**唯一 authoring 真源**——建书内容构建
（novel-build）与续写大纲（novel-replan）共用同一段"怎么想"的流程。dsh 的 skill 注入
路径只读单个 SKILL.md（`dsh_bridge._skill_text_for_profile`），所以内核必须内联：

    <!-- plan-core:begin -->
    …与 plan-core.md 逐字节相同…
    <!-- plan-core:end -->

本脚本只替换两个标记之间的内容，**绝不碰标记块之外的文本**（块外是各入口自己的
phase policy / 落点映射 / 提交护栏）。契约测试 `tools/test_plan_core.py` 负责防漂移。

用法：python tools/sync_plan_core.py [--check]
  --check  只报告是否需要同步，不写盘（退出码非 0 表示有漂移）
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries.skill_profile import (  # noqa: E402
    PLAN_CORE_SKILLS, extract_plan_core_block, plan_core_path, plan_core_text,
    skill_path, splice_plan_core,
)


def sync(*, check_only: bool = False) -> int:
    kernel = plan_core_text()
    print(f"  内核：{os.path.relpath(plan_core_path(), _ROOT)}（{len(kernel)} 字符）")
    changed = []
    for skill in PLAN_CORE_SKILLS:
        path = str(skill_path(skill))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if extract_plan_core_block(text) == kernel:
            print(f"  ·  {skill}: 内核块已是最新")
            continue
        changed.append(skill)
        if check_only:
            print(f"  ⚠️  {skill}: 内核块需要刷新")
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(splice_plan_core(text, kernel))
        print(f"  ✅ {skill}: 内核块已刷新")
    if changed and check_only:
        print(f"\n  {len(changed)} 个入口待刷新：{', '.join(changed)}")
        return 1

    from tools.sync_skills_mirror import sync as sync_mirror
    print("")
    rc = sync_mirror(check_only=check_only)
    return rc


if __name__ == "__main__":
    sys.exit(sync(check_only="--check" in sys.argv[1:]))
