"""风格规则库（可编辑）— 禁句式 + 去AI词表，从模块常量迁移为 JSONL 持久化。

竞品借鉴：AI-NWA promptWorkbench（prompt 可编辑 slot 台）——把硬编码在 style_ban.py /
de_ai.py 的规则表变成用户可增删改的库。消费方（check_style_bans / apply_word_replacements /
reviewer._get_replacements / style_assets._detect_avoid_words）改经本库读取：
无持久文件（未编辑）= 内置种子；有持久文件 = 用户覆盖。
"""
import re
from dataclasses import dataclass, field

from .base_library import JsonLibrary

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
    profile_id: str = ""   # 空 = 全局基线；非空 = 该笔名专属规则
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
        # profile_id 缺省空串 → 旧 jsonl 自动归全局基线，零迁移
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
        n = 0
        for pat, desc, sev in BAN_SEED:
            n += 1
            yield StyleRule(id=f"ban_{n}", kind="ban", pattern=pat, desc=desc, severity=sev)
        for w, reps in WORD_SEED.items():
            n += 1
            yield StyleRule(id=f"word_{n}", kind="word", pattern=w,
                            desc=f"「{w}」AI 高频词", replacements=list(reps))

    def rules_for(self, profile_id: str = "") -> list:
        """该作用域名下的规则（profile_id="" = 全局基线；非空 = 该笔名专属）。"""
        pid = (profile_id or "").strip()
        return [r for r in self.rules if (r.profile_id or "").strip() == pid]

    def get_bans(self, profile_id: str = ""):
        """enabled 禁则 [(compiled_regex, desc, severity)]；未编辑=内置种子。
        profile_id 空 = 全局基线（兜底侧）；非空 = 该笔名专属 + 全局基线（继承聚合，注入侧）。"""
        scope = self.rules_for("")                       # 全局基线在前
        if profile_id:
            scope = scope + self.rules_for(profile_id)   # 笔名专属叠加在后
        out = []
        for r in scope:
            if r.kind != "ban" or not r.enabled or not r.pattern:
                continue
            try:
                out.append((re.compile(r.pattern), r.desc, r.severity))
            except re.error:
                continue
        return out

    def get_word_map(self, profile_id: str = ""):
        """enabled 词表 {词: [替换候选]}；未编辑=内置种子。
        profile_id 空 = 全局基线（兜底侧）；非空 = 全局 + 该笔名（笔名覆盖同名全局词的候选）。"""
        word_map = {}
        for r in self.rules_for(""):
            if r.kind == "word" and r.enabled and r.pattern:
                word_map[r.pattern] = list(r.replacements or [])
        if profile_id:
            for r in self.rules_for(profile_id):
                if r.kind == "word" and r.enabled and r.pattern:
                    word_map[r.pattern] = list(r.replacements or [])
        return word_map

    def build_rules_block(self, profile_id: str = "") -> str:
        """生成前注入的规则段文本（笔名专属 + 全局基线，分段标注；含 prefer 习惯）。"""
        def _word_line(r):
            return f"「{r.pattern}」→{'、'.join(r.replacements) if r.replacements else '改写'}"
        parts = []
        # profile_id 空时 own 为空（全局作用域只显示全局基线段，避免重复标注）
        own = [r for r in self.rules_for(profile_id) if r.enabled and r.pattern] if profile_id else []
        gbl = [r for r in self.rules_for("") if r.enabled and r.pattern]
        own_prefer = [r.pattern for r in own if r.kind == "prefer"]
        own_bans = [r.desc or r.pattern for r in own if r.kind == "ban" and r.severity == "warning"]
        own_words = [_word_line(r) for r in own if r.kind == "word"]
        gbl_prefer = [r.pattern for r in gbl if r.kind == "prefer"]
        gbl_bans = [r.desc or r.pattern for r in gbl if r.kind == "ban" and r.severity == "warning"]
        gbl_words = [_word_line(r) for r in gbl if r.kind == "word"]
        if own_prefer:
            parts.append("【本笔名习惯偏好】" + "；".join(own_prefer))
        if own_bans:
            parts.append("【本笔名专属禁句式（必禁用）】" + "、".join(own_bans))
        if own_words:
            parts.append("【本笔名专属替换词表】" + "；".join(own_words))
        if gbl_prefer:
            parts.append("【全局习惯偏好（所有笔名通用）】" + "；".join(gbl_prefer))
        if gbl_bans:
            parts.append("【全局基线禁句式（所有笔名必禁用）】" + "、".join(gbl_bans))
        if gbl_words:
            parts.append("【全局基线 AI 高频词表（所有笔名通用，见词即替换）】" + "；".join(gbl_words))
        return "\n".join(parts)
