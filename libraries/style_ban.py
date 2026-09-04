"""硬禁句式表 — 单一来源（style_rules 库），进 WRITER_SYSTEM + reviewer + de_ai。

竞品借鉴：本项目 roadmap P0-3 + creative-writing-skills「Punctuation Tells」反面。
检测（reviewer）与替换（de_ai）策略分离：本模块只做「生成时禁 + 生成后检测」，
不参与 de_ai 的机械替换（避免把活文改僵，见 LANGUAGE_DISCIPLINE）。
规则表已迁移到 style_rules 库（可编辑）：STYLE_BAN_LIST 保留为内置种子兼容导出，
运行时 check/build 走 StyleRuleLibrary（用户编辑后生效）。
"""
import re

from .style_rules import BAN_SEED, StyleRuleLibrary

# (正则, 描述, 严重度) — 内置种子兼容导出（warning 级硬禁，info 级提示）
STYLE_BAN_LIST = [(re.compile(p), d, s) for p, d, s in BAN_SEED]


def _bans():
    """运行时禁则（用户编辑后=覆盖种子）。"""
    return StyleRuleLibrary().get_bans()

# 语言纪律条款（creative-writing-skills llm-writing + Trust the Reader）：
# 防 de_ai 词表机械替换把活文改僵，保留刻意的省略/重复/半截话/节奏
LANGUAGE_DISCIPLINE = (
    "【语言纪律】1) 不机械替换：保留刻意的省略、重复、半截话、节奏停顿——"
    "它们是有意的表达，不是错误；2) Trust the Reader：不要替读者解释、点破、"
    "补全一切——留白让读者自己完成；3) 对话用日常语气，内心独白可口语化；"
    "4) 动作描写不要每句都带修饰副词。"
)


def build_style_ban_prompt() -> str:
    """生成「严禁使用」文本，供 WRITER_SYSTEM / render_bridge_prompt 注入。"""
    banned = "、".join(desc for _, desc, sev in _bans() if sev == "warning")
    return f"6) 严禁使用以下句式：{banned}。"


def check_style_bans(text: str) -> list[dict]:
    """检测硬禁句式，返回 [{severity, category, description, location, suggestion}]。

    返回 dict 而非 ReviewIssue，避免与 reviewer 循环导入（reviewer 负责包装成 ReviewIssue）。
    """
    issues = []
    for pat, desc, sev in _bans():
        m = pat.search(text or "")
        if m:
            start = max(0, m.start() - 10)
            location = text[start:m.end() + 10]
            issues.append({
                "severity": sev,
                "category": "style_ban",
                "description": f"命中硬禁句式：{desc}",
                "location": location,
                "suggestion": "改写为更自然的表达（如破折号改为逗号/句号断开）",
            })
    return issues
