"""文本工具 — v2 引擎共用的轻量文本处理函数。"""

import re


def count_prose_units(text: str) -> int:
    """中文字数统计（中文按字，英文按词）"""
    chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    return chinese + english_words
