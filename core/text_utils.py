"""文本工具 — v2 引擎共用的轻量文本处理函数。"""

import re

# 统一字数口径：中文字符按单字计数，连续英文单词按一个词计数；
# 数字、标点、空白及其它符号不计入“字数”。所有展示、门禁和故事线坐标都使用此口径。
_CJK_CHAR_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


def count_prose_units(text: str) -> int:
    """统一正文“字数”：中文字符每个 1 字，连续英文单词每个 1 字。

    数字、标点、空白和其它符号不计数；英文缩写/所有格中的撇号属于同一个英文词。
    """
    value = text or ""
    return len(_CJK_CHAR_RE.findall(value)) + len(_ENGLISH_WORD_RE.findall(value))


def cjk_char_count(text: str) -> int:
    """纯中文字符数（英文/数字不计）——锁章门 / FREE 阈值等需纯 CJK 口径。"""
    return len(_CJK_CHAR_RE.findall(text or ""))
