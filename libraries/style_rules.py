"""风格规则库（可编辑）— 禁句式 + 去AI词表，从模块常量迁移为 JSONL 持久化。

竞品借鉴：AI-NWA promptWorkbench（prompt 可编辑 slot 台）——把硬编码在 style_ban.py /
de_ai.py 的规则表变成用户可增删改的库。消费方（check_style_bans / apply_word_replacements /
reviewer._get_replacements / style_assets._detect_avoid_words）改经本库读取：
无持久文件（未编辑）= 默认笔名内置种子；有持久文件 = 用户覆盖。

无全局基线：规则一律归属某个笔名（profile_id）。内置反 AI 种子归属 DEFAULT_PROFILE_ID
（枫落），生成后去 AI 味兜底（get_bans/get_word_map 空参）即读取默认笔名的规则。
"""
import re
from dataclasses import dataclass, field

from .base_library import JsonLibrary

# 默认笔名：内置反 AI 规则种子（原「全局基线」）的归属。生成后去 AI 味兜底读取它。
DEFAULT_PROFILE_ID = "profile_001"

# ── 内置种子（原 style_ban.STYLE_BAN_LIST / de_ai.AI_WORD_MAP 同源，迁移至此）──
BAN_SEED = [
    (r'不是[^。！？]{1,20}而是[^。！？]{1,20}', "「不是……而是……」对比句式", "warning"),
    (r'——', "破折号（em dash）", "warning"),
    (r'值得一提(的是)?', "「值得一提」说教句式", "warning"),
    (r'值得(注意|关注)的是', "「值得注意」说教句式", "warning"),
    (r'更重要的是', "「更重要的是」AI 过渡句式", "info"),
    (r'可以说', "「可以说」AI 冗余铺垫", "info"),
    (r'从某种(程度|意义)上说', "「从某种程度/意义上说」AI 句式", "info"),
    (r'这意味着', "「这意味着」AI 解释句式", "info"),
    (r'不仅如此', "「不仅如此」AI 递进句式", "info"),
]

WORD_SEED = {
    "然而": ["但", "可", "不过"],
    "此外": ["另外", "还有", "再说"],
    "因此": ["所以", "于是"],
    "总之": ["一句话", "说白了"],
    "尽管如此": ["话虽如此", "即便如此"],
    "仿佛": ["像", "好像", "跟……似的"],
    "似乎": ["好像", "感觉", "看着像"],
    "不禁": ["忍不住", "下意识地", "不由自主地"],
    "不由得": ["忍不住", "下意识"],
    "只见": ["看到", "眼前", ""],
    "但见": ["看到", ""],
    "微微一笑": ["笑了笑", "嘴角一扬", "淡笑"],
    "心中一动": ["心里一跳", "心念一动", "怔了一下"],
    "眼中闪过一丝": ["眼里闪过", "目光中带着"],
    "不由得倒吸一口凉气": ["倒吸一口气", "吸了口冷气"],
    "心中暗道": ["心想", "暗想", "心里嘀咕"],
    "缓缓": ["慢慢", "轻轻", "逐渐"],
    "忽然": ["突然", "一下子", "猛地"],
    "顿时": ["立刻", "马上", "瞬间"],
    "竟然": ["居然", "真就", "愣是"],
    "与此同时": ["另一边", "同一时间", "这个时候"],
    "就在这时": ["正想着", "刚说完", "话没落"],
    "转眼间": ["很快", "没多久", "过了一阵"],
}


@dataclass
class StyleRule:
    id: str
    kind: str              # "ban" 禁句式 | "word" AI 词 | "prefer" 偏好/习惯（正向模仿）
    profile_id: str = ""   # 归属笔名 id；空 = 无主（不注入，见 rules_for）
    pattern: str = ""      # ban=正则串；word=词本身；prefer=指令文本
    desc: str = ""
    severity: str = "warning"   # ban: warning/info
    replacements: list = field(default_factory=list)  # word 的替换候选
    enabled: bool = True

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "profile_id": self.profile_id,
                "pattern": self.pattern, "desc": self.desc, "severity": self.severity,
                "replacements": list(self.replacements or []), "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict):
        # profile_id 缺省空串 → 无主（兼容旧 jsonl；读取方按 DEFAULT_PROFILE_ID 兜底）
        return cls(id=str(d.get("id", "")), kind=str(d.get("kind", "ban")),
                   profile_id=str(d.get("profile_id", "")),
                   pattern=str(d.get("pattern", "")), desc=str(d.get("desc", "")),
                   severity=str(d.get("severity", "warning")),
                   replacements=list(d.get("replacements") or []),
                   enabled=bool(d.get("enabled", True)))


class StyleRuleLibrary(JsonLibrary):
    _instance = None
    _list_attr = "rules"
    _key = "rules"
    _file_name = "style_rules.jsonl"

    @classmethod
    def _from_dict(cls, d: dict):
        return StyleRule.from_dict(d)

    def _builtin(self):
        """内置反 AI 规则种子，归属默认笔名（DEFAULT_PROFILE_ID），不再有全局基线。"""
        n = 0
        for pat, desc, sev in BAN_SEED:
            n += 1
            yield StyleRule(id=f"ban_{n}", kind="ban", profile_id=DEFAULT_PROFILE_ID,
                            pattern=pat, desc=desc, severity=sev)
        for w, reps in WORD_SEED.items():
            n += 1
            yield StyleRule(id=f"word_{n}", kind="word", profile_id=DEFAULT_PROFILE_ID,
                            pattern=w, desc=f"「{w}」AI 高频词", replacements=list(reps))

    def rules_for(self, profile_id: str = "") -> list:
        """该笔名名下的规则（精确按 profile_id 过滤；空 = 无主规则，正常应为空）。"""
        pid = (profile_id or "").strip()
        return [r for r in self.rules if (r.profile_id or "").strip() == pid]

    def _scope_id(self, profile_id: str) -> str:
        """空参 → 默认笔名（保存兜底侧：生成后去 AI 味对所有书生效）。"""
        return (profile_id or "").strip() or DEFAULT_PROFILE_ID

    def get_bans(self, profile_id: str = ""):
        """enabled 禁则 [(compiled_regex, desc, severity)]；未编辑=默认笔名内置种子。
        profile_id 空 = 默认笔名（枫落）的规则；非空 = 仅该笔名自己的规则。"""
        out = []
        for r in self.rules_for(self._scope_id(profile_id)):
            if r.kind != "ban" or not r.enabled or not r.pattern:
                continue
            try:
                out.append((re.compile(r.pattern), r.desc, r.severity))
            except re.error:
                continue
        return out

    def get_word_map(self, profile_id: str = ""):
        """enabled 词表 {词: [替换候选]}；未编辑=默认笔名内置种子。
        profile_id 空 = 默认笔名（枫落）的词表；非空 = 仅该笔名自己的词表。"""
        word_map = {}
        for r in self.rules_for(self._scope_id(profile_id)):
            if r.kind == "word" and r.enabled and r.pattern:
                word_map[r.pattern] = list(r.replacements or [])
        return word_map

    def build_rules_block(self, profile_id: str = "") -> str:
        """生成前注入的规则段文本（仅该笔名的规则；profile_id 空 = 默认笔名；含 prefer 习惯）。"""
        def _word_line(r):
            return f"「{r.pattern}」→{'、'.join(r.replacements) if r.replacements else '改写'}"
        own = [r for r in self.rules_for(self._scope_id(profile_id)) if r.enabled and r.pattern]
        parts = []
        prefer = [r.pattern for r in own if r.kind == "prefer"]
        bans = [r.desc or r.pattern for r in own if r.kind == "ban" and r.severity == "warning"]
        words = [_word_line(r) for r in own if r.kind == "word"]
        if prefer:
            parts.append("【本笔名习惯偏好】" + "；".join(prefer))
        if bans:
            parts.append("【本笔名专属禁句式（必禁用）】" + "、".join(bans))
        if words:
            parts.append("【本笔名专属替换词表】" + "；".join(words))
        return "\n".join(parts)
