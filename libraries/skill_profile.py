"""skill ↔ MCP profile 契约（P0）—— 共享 parser/映射，供契约测试与运行时预检复用。

原则：Profile = Skill 的可执行依赖闭包。`referenced_tools(skill_text) ⊆ PROFILE_TOOLS[profile]`
不成立即视为契约破损（会让 agent 调未知工具→unknown tool），由 `tools/test_skill_profile_contract.py`
在提交前拦截、`dsh_bridge.run_dsh_task` 在 spawn 前拒绝启动。绝不自动 fail-open 到全 45。
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# dsh skill 名 → 它运行时的 profile
SKILL_PROFILE_MAP = {
    "novel-build-candidates": "build-candidates",
    "novel-build": "build",
    "novel-story": "write",
    "novel-replan": "replan",
    "novel-publish": "publish",
    "novel-scout": "scout",
}


@functools.lru_cache(maxsize=1)
def _registry_names() -> frozenset:
    """TOOL_REGISTRY 真实工具名集合（agent_tools 顶导入重，首次调用后缓存）。"""
    from agent_tools import TOOL_REGISTRY  # noqa: E402
    return frozenset(e["name"] for e in TOOL_REGISTRY)


@functools.lru_cache(maxsize=8)
def profile_allows(profile: str) -> frozenset:
    """某 profile 暴露的工具集（PROFILE_TOOLS，router 正向权威）。"""
    from libraries.agent_tool_router import PROFILE_TOOLS  # noqa: E402
    return frozenset(PROFILE_TOOLS.get(profile) or set())


def skill_path(skill: str) -> Path:
    return ROOT / "agent-sidecar" / "skills" / skill / "SKILL.md"


def skill_text(skill: str) -> str:
    p = skill_path(skill)
    if not p.exists():
        raise FileNotFoundError(f"skill 缺失：{p}")
    return p.read_text(encoding="utf-8")


def referenced_tools(text: str) -> set:
    """提取 SKILL.md 里出现的真实工具名。

    只保留 TOOL_REGISTRY 里存在的工具；写法不限（裸名/反引号/mcp__前缀/带括号调用），
    因为我们都按整词在文本里匹配注册表名。这会把「提及」也算引用——因此 skill 正文不应
    指名 profile 外的工具（连负向「不要调 X」也别写 X，说"列表外工具"即可）。
    """
    names = sorted(_registry_names())
    if not names:
        return set()
    pat = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_])")
    return set(pat.findall(text or ""))


def missing_for_text(text: str, profile: str) -> list:
    """skill 文本引用但该 profile 未暴露的工具（排序，供报错）。"""
    missing = referenced_tools(text) - set(profile_allows(profile))
    return sorted(missing)


def missing_for_skill(skill: str) -> list:
    profile = SKILL_PROFILE_MAP[skill]
    return missing_for_text(skill_text(skill), profile)


def dsh_mirror_mismatch() -> list:
    """agent-sidecar/skills 源 vs .dsh/skills 镜像 byte 不一致的 skill 名单。"""
    out = []
    for skill in SKILL_PROFILE_MAP:
        src = skill_path(skill)
        dst = ROOT / ".dsh" / "skills" / skill / "SKILL.md"
        if not src.exists():
            out.append(f"{skill}: 源缺失")
            continue
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            out.append(f"{skill}: 镜像与源不一致（请 cp agent-sidecar/skills/{skill}/SKILL.md .dsh/skills/{skill}/）")
    return out
