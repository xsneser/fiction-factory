#!/usr/bin/env python3
"""把 agent-sidecar/skills/<name>/SKILL.md 同步到 .dsh/skills/<name>/SKILL.md。

背景：`agent-sidecar/skills/` 是源，`.dsh/skills/` 是运行期镜像（`.dsh/` 被 gitignore）。
profiles-on 时 bridge 直读源注入，但 fallback / dsh 原生 skill 发现仍扫 `.dsh/skills`，
所以两者必须逐字节一致——契约测试 `tools/test_skill_profile_contract.py` 会断言这一点。

用法：python tools/sync_skills_mirror.py [--check]
  --check  只报告是否需要同步，不写盘（退出码非 0 表示有漂移）
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries.skill_profile import SKILL_PROFILE_MAP, skill_path  # noqa: E402


def mirror_path(skill: str) -> str:
    return os.path.join(_ROOT, ".dsh", "skills", skill, "SKILL.md")


def sync(*, check_only: bool = False) -> int:
    drifted = []
    for skill in SKILL_PROFILE_MAP:
        src = str(skill_path(skill))
        if not os.path.exists(src):
            print(f"  ❌ {skill}: 源缺失（{src}）")
            drifted.append(skill)
            continue
        dst = mirror_path(skill)
        with open(src, "rb") as f:
            data = f.read()
        same = os.path.exists(dst) and open(dst, "rb").read() == data
        if same:
            print(f"  ·  {skill}: 一致")
            continue
        drifted.append(skill)
        if check_only:
            print(f"  ⚠️  {skill}: 镜像需要同步")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        print(f"  ✅ {skill}: 已同步 → .dsh/skills/{skill}/SKILL.md")
    if drifted and check_only:
        print(f"\n  {len(drifted)} 个 skill 镜像漂移：{', '.join(drifted)}")
        return 1
    print(f"\n  镜像同步完成（{len(SKILL_PROFILE_MAP)} skill，{len(drifted)} 个更新）")
    return 0


if __name__ == "__main__":
    sys.exit(sync(check_only="--check" in sys.argv[1:]))
