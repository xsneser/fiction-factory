"""硬禁句式表 STYLE_BAN_LIST — 单一来源，进 WRITER_SYSTEM + reviewer + de_ai。

竞品借鉴：本项目 roadmap P0-3 + creative-writing-skills「Punctuation Tells」反面。
检测（reviewer）与替换（de_ai）策略分离：本表只做「生成时禁 + 生成后检测」，
不参与 de_ai 的机械替换（避免把活文改僵，见 LANGUAGE_DISCIPLINE）。
"""
import re

# (正则, 描述, 严重度) — warning 级硬禁，info 级提示
STYLE_BAN_LIST = [
    (re.compile(r'不是[^。！？]{1,20}而是[^。！？]{1,20}'), "「不是……而是……」对比句式", "warning"),
    (re.compile(r'——'), "破折号（em dash）", "warning"),
    (re.compile(r'值得一提(的是)?'), "「值得一提」说教句式", "warning"),
    (re.compile(r'值得(注意|关注)的是'), "「值得注意」说教句式", "warning"),
    (re.compile(r'更重要的是'), "「更重要的是」AI 过渡句式", "info"),
    (re.compile(r'可以说'), "「可以说」AI 冗余铺垫", "info"),
    (re.compile(r'从某种(程度|意义)上说'), "「从某种程度/意义上说」AI 句式", "info"),
    (re.compile(r'这意味着'), "「这意味着」AI 解释句式", "info"),
    (re.compile(r'不仅如此'), "「不仅如此」AI 递进句式", "info"),
]

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
    banned = "、".join(desc for _, desc, sev in STYLE_BAN_LIST if sev == "warning")
    return f"6) 严禁使用以下句式：{banned}。"


def check_style_bans(text: str) -> list[dict]:
    """检测硬禁句式，返回 [{severity, category, description, location, suggestion}]。

    返回 dict 而非 ReviewIssue，避免与 reviewer 循环导入（reviewer 负责包装成 ReviewIssue）。
    """
    issues = []
    for pat, desc, sev in STYLE_BAN_LIST:
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
