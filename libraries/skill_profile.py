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
            out.append(f"{skill}: 镜像与源不一致（跑 `python tools/sync_skills_mirror.py`）")
    return out


# ── 规划流程内核：单一真源 + 标记块内联 ──────────────────────────────────────
# 两个入口（novel-build / novel-replan）共用同一段"怎么想"的流程文本。dsh 的 skill
# 注入路径只读 SKILL.md 一个文件（`dsh_bridge._skill_text_for_profile`），所以内核
# 必须**内联**进各自的 SKILL.md；`_shared/plan-core.md` 是 authoring 真源，
# `tools/sync_plan_core.py` 负责把它刷进两个标记块，本模块只提供抽取与比对，
# 供契约测试（`tools/test_plan_core.py`）与同步脚本共用同一份解析。
PLAN_CORE_SKILLS = ("novel-build", "novel-replan")
PLAN_CORE_BEGIN = "<!-- plan-core:begin -->"
PLAN_CORE_END = "<!-- plan-core:end -->"
PLAN_CORE_RELPATH = "agent-sidecar/skills/_shared/plan-core.md"


def plan_core_path() -> Path:
    return ROOT / PLAN_CORE_RELPATH


@functools.lru_cache(maxsize=1)
def plan_core_text() -> str:
    """内核正文（authoring 真源）。"""
    return plan_core_path().read_text(encoding="utf-8")


def extract_plan_core_block(text: str):
    """SKILL.md 里标记块的内容（不含两行标记本身）；缺失/重复/换行形态异常返回 None。

    约定：`begin` 标记行后必须紧跟一个换行，块正文自下一行起、到 `end` 标记行的行首为止
    ——因此块内容与 `plan-core.md` 逐字节相同（含末尾换行）。
    """
    src = text or ""
    if src.count(PLAN_CORE_BEGIN) != 1 or src.count(PLAN_CORE_END) != 1:
        return None
    _, rest = src.split(PLAN_CORE_BEGIN, 1)
    body, _ = rest.split(PLAN_CORE_END, 1)
    if not body.startswith("\n"):
        return None
    return body[1:]


def plan_core_mismatch() -> list:
    """内核块漂移清单：缺标记 / 与内核文件不一致 / 两入口互相不一致。"""
    out, blocks = [], {}
    try:
        kernel = plan_core_text()
    except FileNotFoundError:
        return [f"内核文件缺失：{PLAN_CORE_RELPATH}"]
    for skill in PLAN_CORE_SKILLS:
        try:
            text = skill_text(skill)
        except FileNotFoundError:
            out.append(f"{skill}: skill 缺失")
            continue
        block = extract_plan_core_block(text)
        if block is None:
            out.append(f"{skill}: 缺少 plan-core 标记块（`{PLAN_CORE_BEGIN}` 与 `{PLAN_CORE_END}` 各一次，"
                       "且 begin 标记行后需换行）")
            continue
        blocks[skill] = block
        if block != kernel:
            out.append(f"{skill}: 内核块与 {PLAN_CORE_RELPATH} 不一致（跑 `python tools/sync_plan_core.py`）")
    if len(blocks) == 2 and blocks[PLAN_CORE_SKILLS[0]] != blocks[PLAN_CORE_SKILLS[1]]:
        out.append("两个入口的内核块互相不一致")
    return out


def plan_core_tool_leaks() -> list:
    """内核里出现的真实工具名（必须为空——内核只写认知步骤，不写能力名）。"""
    try:
        return sorted(referenced_tools(plan_core_text()))
    except FileNotFoundError:
        return []


def splice_plan_core(text: str, kernel: str) -> str:
    """把内核刷进标记块，返回新正文；缺标记抛错（不静默改写）。"""
    if text.count(PLAN_CORE_BEGIN) != 1 or text.count(PLAN_CORE_END) != 1:
        raise ValueError("缺少 plan-core 标记块，无法内联内核")
    head, rest = text.split(PLAN_CORE_BEGIN, 1)
    _, tail = rest.split(PLAN_CORE_END, 1)
    return f"{head}{PLAN_CORE_BEGIN}\n{kernel}{PLAN_CORE_END}{tail}"
