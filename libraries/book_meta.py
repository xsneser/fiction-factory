"""
书名 / 简介 / 平台约束（Book Metadata）— 纯函数，无 LLM、无状态

从原 new_book.py 抢救迁移（删除节拍/新书写作后保留元数据生成）。
接入故事线流：第 1 章写完由 engine._generate_book_meta 调用生成书名+简介。
"""


def build_title_prompt(genre: str, sub_genre: str, platform: str,
                       chapter_123: str) -> str:
    """生成建议书名（需要 LLM）。chapter_123：第1章正文（前 1000 字会被用到）。"""
    return f"""你是一位专业的网文编辑，擅长为小说起名字。

根据以下信息，为这本小说提供 5 个备选书名：

【流派】：{genre}/{sub_genre}
【平台】：{platform}
【开篇内容摘要】：
{chapter_123[:1000]}...

要求：
1. 书名要有网感，能吸引点击
2. 不建议使用"之""录""传"等传统书名列字（除非是玄幻正剧）
3. 每个书名 4-10 字
4. 优选出你认为最好的一两个

请以 JSON 格式返回：
{{"titles": ["书名1", "书名2", ...], "best": "最佳书名", "reason": "理由"}}"""


def build_synopsis_prompt(genre: str, sub_genre: str, platform: str,
                          chapter_123: str) -> str:
    """生成简介（需要 LLM）。"""
    return f"""你是一位专业的网文编辑。

根据以下开篇内容，为这本小说写一段简介（100-200字）：

【流派】：{genre}/{sub_genre}
【开篇内容摘要】：
{chapter_123[:1000]}...

要求：
1. 简介要吸睛，有钩子
2. 不要剧透太多关键情节
3. 适合放在 {platform} 的小说详情页

请以 JSON 格式返回：
{{"synopsis": "简介内容"}}"""


def platform_constraints(platform: str) -> str:
    """平台特定的写作约束（供开场/写作 prompt 用）。"""
    constraints = {
        "fanqie": (
            "【番茄小说约束】\n"
            "- 每章 2500-3500 字\n"
            "- 每章 6-8 个场景切换（换行隔开）\n"
            "- 章末必须有钩子\n"
            "- 对话比例不低于 30%\n"
            "- 开篇前 500 字必须有冲突或危机"
        ),
        "qidian": (
            "【起点中文网约束】\n"
            "- 每章 3000-5000 字\n"
            "- 描写可以更细致\n"
            "- 章末有悬念即可，不强求钩子"
        ),
    }
    return constraints.get(platform, "")


def publish_thresholds(platform: str, min_total_words: int | None = None,
                       min_chapters: int | None = None) -> dict:
    """上架所需的结构化平台阈值（纯函数，无 LLM）。

    供上架检查（libraries/publisher.py）与模拟脚本使用；调用方可用
    min_total_words / min_chapters 覆盖默认值（如模拟脚本压到 800 字跑通全流程）。
    """
    defaults = {
        "fanqie": {"min_total_words": 20000, "min_chapters": 5},
        "qidian": {"min_total_words": 30000, "min_chapters": 5},
    }
    thresholds = dict(defaults.get(platform, {"min_total_words": 10000, "min_chapters": 3}))
    if min_total_words is not None:
        thresholds["min_total_words"] = min_total_words
    if min_chapters is not None:
        thresholds["min_chapters"] = min_chapters
    return thresholds
