"""确定性连续性检测（规则层，零成本）— 生成后扫描，替代纯 prompt 铁律。

竞品借鉴：Novel-OS 12 项连续性引擎 / OpenNovel canon 检查 / deep-novel-system irreversible_changes。
把 prompt_harness.CONSISTENCY_RULES 从「prompt 铁律」升级为「生成后规则扫描」。

可行性标注（checks 每项带 status）：
  · ok/warn  = 确定性可行（正则/台账，误报低）
  · partial  = 弱启发（无语义理解，高误报，仅 info 级参考）
  · 不可行项（身份穿帮/已死角色复现/敌对同框）需 NER/知识图谱/关系元数据，不实现
"""
import re

# 系统/金手指绑定激活（确定性可行：全书只发生一次）
_SYSTEM_BIND_PATTERNS = [
    re.compile(r'绑定(成功|系统|完成)'),
    re.compile(r'系统(激活|启动|开启)'),
    re.compile(r'激活(成功|完成)'),
]

# 数值词弱启发（部分可行）：形如「压迫值/劳动值/经验值/属性点/30%」
_NUMERIC_TERM = re.compile(r'([一-鿿]{2,4}(?:值|点|力|度|%|％))')

# 时间过渡正向信号词
_TIME_WORDS = ("当天夜里", "第二天", "三天后", "次日", "当晚", "一周后",
               "半个月后", "第二天一早", "当天晚上", "清晨", "傍晚")

# 反性别代词（男→她 / 女→他）
_FEMALE_PRONOUNS = ("她", "她的")
_MALE_PRONOUNS = ("他", "他的")


class ContinuityChecker:
    """确定性连续性检测器 — 纯规则，零 LLM。"""

    def check_system_binding(self, chapters) -> list[dict]:
        """跨章扫「绑定/激活」，命中 ≥2 章 → warning（系统绑定全书只一次）。"""
        hit = [c.get("num", 0) for c in chapters
               if any(p.search(c.get("content", "") or "") for p in _SYSTEM_BIND_PATTERNS)]
        if len(hit) >= 2:
            return [{
                "severity": "warning", "category": "system_binding",
                "description": f"系统/金手指「绑定/激活」在 {len(hit)} 章出现（第 {hit} 章），全书应只发生一次",
                "location": f"第 {hit} 章",
                "suggestion": "后续同类事件改用「新模块/新功能解锁」，禁止重复「绑定成功」",
            }]
        return []

    def check_numeric_closure(self, chapters) -> list[dict]:
        """数值词全书只出现 1 次 → info（弱启发：疑似引入未闭环）。"""
        term_chapters = {}
        for c in chapters:
            content = c.get("content", "") or ""
            for m in _NUMERIC_TERM.finditer(content):
                term_chapters.setdefault(m.group(1), []).append(c.get("num", 0))
        issues = []
        for term, chs in term_chapters.items():
            if len(chs) == 1:
                issues.append({
                    "severity": "info", "category": "numeric_closure",
                    "description": f"数值「{term}」全书只出现 1 次（第 {chs[0]} 章），疑似引入未闭环",
                    "location": f"第 {chs[0]} 章",
                    "suggestion": "引入的数值应在后续情节有回响闭环",
                })
        return issues

    def check_pronoun_gender(self, chapters, characters) -> list[dict]:
        """角色名 ±20 字窗口内出现反性别代词 → warning（防「她字错误」）。"""
        issues = []
        for ch in characters:
            name = ch.get("name", "")
            gender = ch.get("gender", "")
            if not name or not gender:
                continue
            wrong = _FEMALE_PRONOUNS if gender == "男" else (_MALE_PRONOUNS if gender == "女" else ())
            if not wrong:
                continue
            for c in chapters:
                content = c.get("content", "") or ""
                if name not in content:
                    continue
                found = False
                for m in re.finditer(re.escape(name), content):
                    start = max(0, m.start() - 20)
                    end = min(len(content), m.end() + 20)
                    if any(w in content[start:end] for w in wrong):
                        found = True
                        break
                if found:
                    issues.append({
                        "severity": "warning", "category": "pronoun_gender",
                        "description": f"角色「{name}」（{gender}）附近出现反性别代词",
                        "location": f"第 {c.get('num', 0)} 章",
                        "suggestion": "检查该处人称指代是否写错",
                    })
        return issues

    def check_time_transition(self, chapters) -> list[dict]:
        """场景切换处普遍无时间词 → info（弱启发，高误报，仅参考）。"""
        issues = []
        for c in chapters:
            content = c.get("content", "") or ""
            paras = [p.strip() for p in content.split("\n\n") if p.strip()]
            if len(paras) < 5:
                continue
            no_time = sum(1 for p in paras[1:]
                          if not any(w in p[:20] for w in _TIME_WORDS))
            if no_time >= len(paras) - 1:
                issues.append({
                    "severity": "info", "category": "time_transition",
                    "description": f"第 {c.get('num', 0)} 章场景切换处普遍缺少时间过渡词",
                    "location": f"第 {c.get('num', 0)} 章",
                    "suggestion": "跨场景/跨天时加自然时间过渡（如「当天夜里」「三天后」）",
                })
        return issues

    def check_character_offline(self, char_states) -> list[dict]:
        """复用 CharacterStateMachine.warnings()：离线 >50 章告警。"""
        if not char_states:
            return []
        return [{
            "severity": "warning", "category": "character_offline",
            "description": msg, "location": "", "suggestion": "重新引入或收尾该角色",
        } for msg in char_states.warnings()]

    def check_overdue_promises(self, promises, current_chapter) -> list[dict]:
        """pending 且 deadline < current → warning（复用台账）。"""
        issues = []
        for q in promises or []:
            if q.get("status") == "pending":
                deadline = int(q.get("deadline_chapter") or 0)
                if deadline and deadline < current_chapter:
                    issues.append({
                        "severity": "warning", "category": "overdue_promises",
                        "description": f"读者承诺「{q.get('desc', '')}」已逾期"
                                       f"（deadline 第 {deadline} 章，当前第 {current_chapter} 章）",
                        "location": f"deadline 第 {deadline} 章",
                        "suggestion": "近期章节推进或兑现该承诺",
                    })
        return issues

    def _characters_from(self, tl, char_states) -> list[dict]:
        """取角色性别清单：优先 character_states，回退 basic_info.characters。"""
        chars = []
        if char_states and getattr(char_states, "characters", None):
            for c in char_states.characters:
                if c.name and c.gender:
                    chars.append({"name": c.name, "gender": c.gender})
        if not chars and tl:
            bi = getattr(tl, "basic_info", None) or {}
            for c in bi.get("characters", []) or []:
                if isinstance(c, dict) and c.get("name") and c.get("gender"):
                    chars.append({"name": c["name"], "gender": c["gender"]})
        return chars

    def check_all(self, tl, chapters, current_chapter, char_states) -> dict:
        """全量扫描，返回 {issue_count, issues[], suggestions[], scanned_chapters, checks{}}。

        checks 每项带 status（ok/warn/partial），agent 见 partial 即知是弱启发、仅供参考。
        """
        sb = self.check_system_binding(chapters)
        nc = self.check_numeric_closure(chapters)
        pg = self.check_pronoun_gender(chapters, self._characters_from(tl, char_states))
        tt = self.check_time_transition(chapters)
        co = self.check_character_offline(char_states)
        op = self.check_overdue_promises(getattr(tl, "promises", None), current_chapter)

        issues = sb + pg + co + op + nc + tt
        checks = {
            "system_binding": {"status": "warn" if sb else "ok",
                               "detail": sb[0]["description"] if sb else "未发现重复绑定"},
            "pronoun_gender": {"status": "warn" if pg else "ok",
                               "detail": pg[0]["description"] if pg else "未发现人称-性别错配"},
            "character_offline": {"status": "warn" if co else "ok",
                                  "detail": co[0]["description"] if co else "无角色离线过久"},
            "overdue_promises": {"status": "warn" if op else "ok",
                                 "detail": op[0]["description"] if op else "无逾期读者承诺"},
            "numeric_closure": {"status": "partial",
                                "detail": nc[0]["description"] if nc else "数值词均多次出现（弱启发，仅供参考）"},
            "time_transition": {"status": "partial",
                                "detail": tt[0]["description"] if tt else "场景切换处时间词正常（弱启发，仅供参考）"},
        }
        suggestions = []
        for i in issues:
            if i["severity"] == "warning" and i["suggestion"] not in suggestions:
                suggestions.append(i["suggestion"])
        return {
            "issue_count": len(issues),
            "issues": issues,
            "suggestions": suggestions,
            "scanned_chapters": len(chapters),
            "checks": checks,
        }
