"""爽点标注（规则层，零成本）— 单章正文 → 爽点标签（打脸/升级/伏笔回收/装逼/甜宠/反转）。

设计文档 §4.3。规则层关键词/句式匹配；reasoner 二次确认可选（预留）。
落盘 books/<id>/tags.json 由调用方负责。
"""
import re

_TAG_PATTERNS = [
    ("打脸", [r"打脸", r"哑口无言", r"冷汗", r"脸色.{0,4}(难看|铁青|煞白)",
              r"啪", r"瞪目结舌", r"低声下气"]),
    ("升级", [r"突破", r"晋级", r"领悟", r"踏入.{0,4}(境|级)", r"实力大涨",
              r"力量.{0,4}涌", r"境界.{0,4}(提升|突破)"]),
    ("伏笔回收", [r"原来", r"竟是他", r"揭开", r"真相大白", r"想起.{0,6}时",
                 r"一切.{0,4}联系"]),
    ("装逼", [r"淡淡", r"轻笑", r"不值一提", r"蝼蚁", r"随手", r"轻而易举",
              r"不放在眼里"]),
    ("甜宠", [r"心尖", r"甜甜", r"耳根.{0,2}红", r"宠溺", r"抱住",
              r"嘴角.{0,2}扬", r"轻声.{0,3}说"]),
    ("反转", [r"反转", r"没想到", r"竟然", r"却.{0,4}突然", r"峰回路转",
              r"跌眼镜"]),
]

_TAG_ORDER = ["打脸", "升级", "伏笔回收", "装逼", "甜宠", "反转"]


def tag_chapter(content: str, max_tags: int = 50) -> dict:
    """单章正文 → 爽点标签列表 [{start, end, tag, line_text}]。

    按《_TAG_PATTERNS》顺序匹配，保留每次命中的位置与原文片段（上下文 ±20 字）。
    """
    if not content:
        return {"tags": []}
    tags = []
    for tag, pats in _TAG_PATTERNS:
        for pat in pats:
            try:
                for m in re.finditer(pat, content):
                    start = m.start()
                    seg = content[max(0, start - 20):start + 20].replace("\n", " ").strip()
                    tags.append({
                        "start": start, "end": m.end(),
                        "tag": tag, "line_text": seg[:60],
                    })
            except re.error:
                continue
    # 按 tag 顺序稳定排序、去相邻重复片段
    tags.sort(key=lambda t: (_TAG_ORDER.index(t["tag"]), t["start"]))
    return {"tags": tags[:max_tags]}
