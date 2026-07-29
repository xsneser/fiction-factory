"""写作风格分析器 — 从小说章节中提取作者的写作风格特征"""
import json
import logging
from typing import Optional

logger = logging.getLogger("style-analyzer")


def extract_writing_style(llm, novel_title: str, chapters: list[dict]) -> dict:
    """分析小说的写作风格，返回风格特征字典"""
    if not llm or not chapters:
        return _default_style()

    # 取前3章+中间2章+后2章作为样本
    samples = _select_samples(chapters, 7)

    text = "\n\n".join(
        f"【第{s.get('index','?')}章 {s.get('title','')}】\n{s.get('content','')[:1500]}"
        for s in samples
    )

    prompt = f"""分析以下小说《{novel_title}》的写作风格，提取作者的核心写作特征。

请从以下维度分析：

1. **句子节奏**：长句为主还是短句为主？节奏快还是慢？
2. **对话风格**：对话多吗？对话标签用"说""道"还是动作+对话？
3. **描写偏好**：环境描写多还是动作描写多？细节程度如何？
4. **情绪表达**：直白热烈还是含蓄内敛？幽默感如何？
5. **常用词/禁用词**：作者喜欢用哪些词？回避哪些词？
6. **叙事视角**：第一人称还是第三人称？有心理描写吗？
7. **开篇特色**：直接冲突开局还是缓缓展开？
8. **节奏把控**：几章一个小高潮？日常与战斗的比例？

【小说样本】
{text}

返回 JSON：
{{"style_analysis": {{
  "sentence_rhythm": "短句为主/长句为主/混合",
  "dialogue_style": "对话标签偏好和特点",
  "description_focus": "环境/动作/心理描写偏好",
  "emotional_expression": "直白/含蓄/幽默风格",
  "common_words": ["常用词1", "常用词2"],
  "avoid_words": ["禁用词1", "禁用词2"],
  "narrative_perspective": "叙事视角",
  "opening_style": "开篇特点",
  "pacing": "节奏把控特点",
  "summary": "一句话总结写作风格"
}}}}
"""

    try:
        from core.llm_client import extract_json
        raw = llm.call("你是一位专业的文学风格分析师。只返回JSON。",
                       prompt, temperature=0.5, max_tokens=2048)
        data = json.loads(extract_json(raw))
        style = data.get("style_analysis", {})
        logger.info(f"风格分析完成: {style.get('summary','')[:50]}")
        return style
    except Exception as e:
        logger.warning(f"风格分析失败: {e}")
        return _default_style()


def _default_style() -> dict:
    return {
        "sentence_rhythm": "",
        "dialogue_style": "",
        "description_focus": "",
        "emotional_expression": "",
        "common_words": [],
        "avoid_words": [],
        "narrative_perspective": "",
        "opening_style": "",
        "pacing": "",
        "summary": "分析失败",
    }


def _select_samples(chapters: list[dict], count: int = 7) -> list[dict]:
    """从章节列表中抽取有代表性的样本"""
    total = len(chapters)
    if total <= count:
        return chapters

    indices = set()
    # 前3章
    for i in range(min(3, total)):
        indices.add(i)
    # 中间2章
    mid = total // 2
    indices.add(mid)
    if mid + 1 < total:
        indices.add(mid + 1)
    # 后2章
    for i in range(max(0, total - 2), total):
        indices.add(i)

    return [chapters[i] for i in sorted(indices) if i < total]
