"""预置题材标签库（番茄式【双强、末日】硬约束）。

供新书启动向导步骤②渲染 chips（dashboard.py GET 传入 start_book.html），
及 prompt_harness 校验/展示（_tags_block 读 world_building.tags 时用）。
标签是读者预期：世界观/大纲/写作 prompt 必须严格契合所选标签的网文套路。
"""

WORLD_TAG_GROUPS = [
    {"group": "主角与套路", "tags": [
        "重生", "穿越", "系统", "无敌流", "扮猪吃虎", "逆袭",
        "退婚流", "强者归来", "废柴逆袭", "打脸",
    ]},
    {"group": "世界与背景", "tags": [
        "末世", "星际", "异界", "修仙", "灵气复苏", "无限流",
        "快穿", "诸天", "基建", "种田",
    ]},
    {"group": "关系与氛围", "tags": [
        "双强", "双洁", "甜宠", "权谋", "复仇", "团宠",
        "万人迷", "爽文", "脑洞", "悬念",
    ]},
]

WORLD_TAGS = [t for g in WORLD_TAG_GROUPS for t in g["tags"]]
