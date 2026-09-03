"""文本工具 — v2 引擎共用的轻量文本处理函数。"""

import re


def count_prose_units(text: str) -> int:
    """中文字数统计（中文按字，英文按词）"""
    chinese = len(re.findall(r'[一-鿿]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    return chinese + english_words


def cjk_char_count(text: str) -> int:
    """纯中文字符数（英文/数字不计）——锁章门 / FREE 阈值等需纯 CJK 口径。"""
    return len(re.findall(r'[一-鿿]', text or ""))
