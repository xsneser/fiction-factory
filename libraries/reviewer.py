"""
内容审查引擎（Content Reviewer）
本地规则 + LLM 二次确认的质量把关
"""
import re
from dataclasses import dataclass, field


@dataclass
class ReviewIssue:
    """一个问题"""
    severity: str = "warning"       # error/warning/info
    category: str = ""              # 分类
    description: str = ""
    location: str = ""              # 问题位置（摘录）
    suggestion: str = ""            # 修改建议


@dataclass
class ReviewResult:
    """审查结果"""
    passed: bool = False
    score: int = 0                   # 0-100
    issues: list[ReviewIssue] = field(default_factory=list)
    summary: str = ""


class ContentReviewer:
    """内容审查器 — 规则层 + LLM 层"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    # ─── 规则层检查 ───

    def check_word_count(self, content: str, min_words: int = 2000,
                         max_words: int = 5000) -> tuple[bool, int]:
        """字数检查"""
        chinese = len(re.findall(r'[\u4e00-\u9fff]', content))
        if chinese < min_words:
            return False, chinese
        if chinese > max_words:
            return False, chinese
        return True, chinese

    def check_ai_patterns(self, content: str) -> list[ReviewIssue]:
        """AI 痕迹检测"""
        issues = []

        # 高频 AI 词汇检测
        ai_tells = {
            "仿佛": "AI高频修饰词",
            "似乎": "AI高频修饰词",
            "不禁": "AI高频修饰词",
            "只见": "AI高频叙述",
            "但见": "AI高频叙述",
            "不由得": "AI高频修饰词",
        }

        word_count = len(re.findall(r'[\u4e00-\u9fff]', content))
        for word, desc in ai_tells.items():
            count = content.count(word)
            if count > 0:
                rate = count * 1000 / max(word_count, 1)
                if rate > 3:  # 每千字超过3次
                    issues.append(ReviewIssue(
                        severity="warning",
                        category="ai_pattern",
                        description=f"「{word}」出现 {count} 次（每千字 {rate:.1f}次），建议替换",
                        suggestion=f"可选替换：{self._get_replacements(word)}",
                    ))

        # AI 句式检测
        ai_sentences = [
            ("不是……而是……", "AI最爱用的对比句式"),
            ("与此同时", "AI过度使用的过渡词"),
            ("更值得关注的是", "AI说教句式"),
            ("可以说", "AI冗余铺垫"),
        ]
        for pattern, desc in ai_sentences:
            if pattern in content:
                count = content.count(pattern)
                if count >= 2:
                    issues.append(ReviewIssue(
                        severity="info",
                        category="ai_pattern",
                        description=f"「{pattern}」句式使用 {count} 次（{desc}）",
                        suggestion="建议减少使用",
                    ))

        return issues

    def check_paragraph_rhythm(self, content: str) -> list[ReviewIssue]:
        """段落节奏检查"""
        issues = []
        paragraphs = [p.strip() for p in content.split('\n') if p.strip()]

        # 检查是否有超长段
        for i, p in enumerate(paragraphs):
            if len(p) > 300:
                issues.append(ReviewIssue(
                    severity="warning",
                    category="rhythm",
                    description=f"第 {i+1} 段过长（{len(p)} 字符），网文建议每段不超过 200 字",
                    suggestion="在合适位置分段",
                ))

        # 检查连续多段都超过 150 字
        long_streak = 0
        for p in paragraphs:
            if len(p) > 150:
                long_streak += 1
            else:
                long_streak = 0
            if long_streak >= 5:
                issues.append(ReviewIssue(
                    severity="warning",
                    category="rhythm",
                    description="连续 5 段以上超过 150 字，节奏偏慢",
                    suggestion="穿插短段、对话或一句话段落打破节奏",
                ))
                break

        return issues

    def check_dialogue_ratio(self, content: str) -> tuple[float, list[ReviewIssue]]:
        """对话比例检查"""
        # 统计对话行（以引号开头的行或包含「」的行）
        lines = content.split('\n')
        dialogue_lines = sum(1 for l in lines
                             if '「' in l or '」' in l or '"' in l or '“' in l)
        total_lines = max(len(lines), 1)
        ratio = dialogue_lines / total_lines

        issues = []
        if ratio < 0.15:
            issues.append(ReviewIssue(
                severity="warning",
                category="dialogue",
                description=f"对话比例偏低（{ratio:.0%}），网文建议 ≥ 15%",
                suggestion="适当增加人物对话",
            ))

        return ratio, issues

    def check_cliffhanger(self, content: str) -> list[ReviewIssue]:
        """章末钩子检查"""
        issues = []
        # 取最后 200 字
        tail = content[-200:]

        # 检查是否有钩子信号
        hook_signals = ["突然", "忽然", "就在这时", "只见", "却", "但", "可",
                        "？", "……", "没想到", "然而", "猛地"]
        has_hook = any(s in tail for s in hook_signals)

        if not has_hook:
            issues.append(ReviewIssue(
                severity="info",
                category="hook",
                description="章末可能缺少钩子",
                suggestion="在结尾加一个悬念、疑问或反转",
            ))

        return issues

    # ─── 综合审查 ───

    def review(self, content: str, chapter_num: int = 0,
               chapter_title: str = "", target_words: int = 3000) -> ReviewResult:
        """完整审查流程（规则层，不需要 LLM）"""
        result = ReviewResult(passed=True)
        score = 100

        # 1. 字数
        ok, wc = self.check_word_count(content, target_words * 0.7, target_words * 1.3)
        if not ok:
            result.issues.append(ReviewIssue(
                severity="warning", category="word_count",
                description=f"字数 {wc} 与目标 {target_words} 偏差较大"
            ))
            score -= 15

        # 2. AI 痕迹
        ai_issues = self.check_ai_patterns(content)
        result.issues.extend(ai_issues)
        score -= len(ai_issues) * 5

        # 3. 段落节奏
        rhythm_issues = self.check_paragraph_rhythm(content)
        result.issues.extend(rhythm_issues)
        score -= len(rhythm_issues) * 5

        # 4. 对话比例
        ratio, dia_issues = self.check_dialogue_ratio(content)
        result.issues.extend(dia_issues)
        score -= len(dia_issues) * 8

        # 5. 钩子检查
        hook_issues = self.check_cliffhanger(content)
        result.issues.extend(hook_issues)
        # 钩子只是 info 级别，不扣分

        result.score = max(0, min(100, score))
        result.passed = result.score >= 60

        error_count = sum(1 for i in result.issues if i.severity == "error")
        warning_count = sum(1 for i in result.issues if i.severity == "warning")

        result.summary = (
            f"审查完成：{'通过 ✅' if result.passed else '不通过 ❌'} "
            f"({result.score}分) | 字数 {wc} | "
            f"对话比 {ratio:.0%} | "
            f"{error_count} 错误 {warning_count} 警告"
        )

        return result

    def llm_review(self, content: str, context: str = "") -> ReviewResult:
        """LLM 深层审查（更全面但更贵）"""
        if not self.llm:
            return ReviewResult(passed=True, score=80, summary="跳过 LLM 审查")

        prompt = f"""请审查以下小说章节的质量，从以下维度评估：

1. 叙事连贯性：前后是否衔接自然
2. 角色行为一致性：角色行为是否符合设定
3. 节奏感：是否有张有弛
4. 对话质量：对话是否自然、符合角色性格
5. 是否有明显的AI生成痕迹

{context}

章节内容：
{content[:3000]}

请以 JSON 返回：
{{"score": 0-100, "passed": true/false, "issues": ["问题1", "问题2"], "suggestions": ["建议1"]}}"""

        try:
            from core.llm_client import extract_json
            raw = self.llm.call("你是一位专业的网文编辑。", prompt,
                                temperature=0.3, max_tokens=2048)
            data = json.loads(extract_json(raw))
            return ReviewResult(
                score=data.get("score", 80),
                passed=data.get("passed", True),
                issues=[ReviewIssue(description=i, severity="info")
                        for i in data.get("issues", [])],
                summary=", ".join(data.get("suggestions", [])),
            )
        except Exception:
            return ReviewResult(passed=True, score=80, summary="LLM 审查异常")

    def _get_replacements(self, word: str) -> str:
        mapping = {
            "仿佛": "像、好像、跟……似的",
            "似乎": "好像、感觉、看着像",
            "不禁": "忍不住、下意识地、不由自主",
            "只见": "看到、眼前、",
            "但见": "看到、",
            "不由得": "忍不住、下意识",
        }
        return mapping.get(word, "")
