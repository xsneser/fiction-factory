"""
去 AI 味引擎（De-AI Engine）
分层处理：规则替换 → 句式打散 → LLM 重写 → 人工瑕疵注入
"""
import re
import random
from dataclasses import dataclass


# ─── AI 高频词替换表 ───
AI_WORD_MAP = {
    # 连词/转折词
    "然而": ["但", "可", "不过"],
    "此外": ["另外", "还有", "再说"],
    "因此": ["所以", "于是"],
    "总之": ["一句话", "说白了"],
    "尽管如此": ["话虽如此", "即便如此"],

    # 修饰词（过度使用）
    "仿佛": ["像", "好像", "跟……似的"],
    "似乎": ["好像", "感觉", "看着像"],
    "不禁": ["忍不住", "下意识地", "不由自主地"],
    "不由得": ["忍不住", "下意识"],
    "只见": ["看到", "眼前", ""],
    "但见": ["看到", ""],

    # 情感描写
    "微微一笑": ["笑了笑", "嘴角一扬", "淡笑"],
    "心中一动": ["心里一跳", "心念一动", "怔了一下"],
    "眼中闪过一丝": ["眼里闪过", "目光中带着"],
    "不由得倒吸一口凉气": ["倒吸一口气", "吸了口冷气"],
    "心中暗道": ["心想", "暗想", "心里嘀咕"],

    # 动作描写套路
    "缓缓": ["慢慢", "轻轻", "逐渐"],
    "忽然": ["突然", "一下子", "猛地"],
    "顿时": ["立刻", "马上", "瞬间"],
    "竟然": ["居然", "真就", "愣是"],

    # 场景过渡
    "与此同时": ["另一边", "同一时间", "这个时候"],
    "就在这时": ["正想着", "刚说完", "话没落"],
    "转眼间": ["很快", "没多久", "过了一阵"],
}

# ─── 句式模板（AI 最爱用的）───
SENTENCE_PATTERNS = [
    # (正则, 替换策略: "shorten"|"split"|"reorder"|"remove")
    (r"不仅如此，.{0,20}也.{0,30}", "shorten"),
    (r"更重要的是，.{0,30}", "shorten"),
    (r"这意味着.{0,30}", "remove"),
    (r"可以说，.{0,30}", "remove"),
    (r"从某种(程度|意义)上说", "remove"),
    (r"值得(一提|注意)的是", "remove"),
]


def apply_word_replacements(text: str) -> tuple[str, int]:
    """规则层：替换 AI 高频词 → (替换后文本, 替换次数)"""
    count = 0
    result = text
    for old, options in AI_WORD_MAP.items():
        if old in result:
            replacement = random.choice(options)
            # 只替换部分出现（不是全部）
            occurrences = result.count(old)
            replace_count = max(1, occurrences // 2)
            for _ in range(replace_count):
                result = result.replace(old, replacement, 1)
                count += 1
    return result, count


def add_human_imperfections(text: str, typo_rate: float = 0) -> str:
    """人为瑕疵注入：极低概率的'错字'模拟（默认关闭，调用方显式开启）"""
    # 只处理中文，极低概率
    if typo_rate <= 0:
        return text

    common_typos = {
        "的": "地", "地": "的", "得": "的",
        "在": "再", "再": "在",
        "了": "啦", "他": "她",
    }

    result = list(text)
    for i, char in enumerate(result):
        if char in common_typos and random.random() < typo_rate:
            result[i] = common_typos[char]
    return ''.join(result)


def adjust_paragraph_rhythm(text: str, style: str = "chatty") -> str:
    """段落节奏调整"""
    paragraphs = text.split('\n')
    result = []

    for p in paragraphs:
        p = p.strip()
        if not p:
            result.append('')
            continue

        sentences = re.split(r'(?<=[。！？])', p)

        if style == "chatty":
            # 对话/吐槽风格：段落短，频繁回车
            if len(p) > 120:
                # 在合适位置插入换行
                mid = len(p) // 2
                # 找到最近的句号
                for j in range(mid, max(mid - 30, 0), -1):
                    if p[j] in '。！？':
                        p = p[:j+1] + '\n' + p[j+1:]
                        break
                else:
                    # 找逗号
                    for j in range(mid, max(mid - 30, 0), -1):
                        if p[j] == '，':
                            p = p[:j+1] + '\n' + p[j+1:]
                            break

        elif style == "literary":
            # 正剧风格：段落可稍长，但不要太密
            pass

        result.append(p)

    return '\n'.join(result)


@dataclass
class DeAIResult:
    """去 AI 味结果"""
    original: str = ""
    processed: str = ""
    word_replacements: int = 0       # 替换了多少个词
    sentences_split: int = 0         # 拆了多少句
    llm_rewritten: bool = False      # 是否经过了 LLM 重写


class DeAIEngine:
    """去 AI 味引擎"""

    def __init__(self, llm_client=None):
        self.llm = llm_client

    def process_rule_based(self, text: str, style: str = "chatty") -> DeAIResult:
        """纯规则去 AI 味（不需要 LLM，速度快）"""
        result = DeAIResult(original=text)

        # 1. 替换 AI 高频词
        processed, count = apply_word_replacements(text)
        result.word_replacements = count

        # 2. 段落节奏调整
        processed = adjust_paragraph_rhythm(processed, style)

        # 3. 极低概率人为瑕疵
        processed = add_human_imperfections(processed, typo_rate=0)

        result.processed = processed
        return result

    def build_deai_prompt_snippet(self) -> str:
        """生成可注入写作 prompt 的去 AI 味约束"""
        return (
            "\n【去AI味约束——写作时必须遵守】\n"
            "- 禁止使用：仿佛、似乎、不禁、不由得、只见、但见、缓缓、顿时、竟然\n"
            "- 对话用日常语气，不要文绉绉\n"
            "- 每段 2-3 句，不要大段描写\n"
            "- 内心独白可以口语化（如：靠、淦、这TM...）\n"
            "- 不要所有句子主谓宾完整——偶尔留半截话\n"
            "- 动作描写不要每句都带修饰副词\n"
        )
