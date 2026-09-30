#!/usr/bin/env python3
"""dsh 任务文本 → MCP profile 的中文冲突回归。

除各阶段正例外，这里锁两件事：

1. **向导自动发出的两条交接任务原文**（步 1 生成候选 / 步 2→3 继续建书）必须各自落到
   正确的 profile。二者文本上不可分——都含「候选」——曾把步 2→3 判成步 1-2 的 profile，
   而那个工具面没有 set_outline/set_world/set_characters（UI_COMMAND_POLICY 拒收），
   于是故事线根本写不进去（表现为「大纲生成失败」）。
2. **未分类不静默回落只读面**：返回哨兵 UNROUTABLE_PROFILE，由 run_dsh_flow 显式报错。
   此前回落 inspect，用户说「大纲生成失败」只换来一串只读工具调用。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from libraries.dsh_bridge import UNROUTABLE_PROFILE, _task_tool_profile  # noqa: E402

# 向导原文（ui/templates/start_book.html）。改向导文案时下面的 _assert_wizard_text 会提醒同步。
STEP1_TASK = ('请为这本新书生成世界观候选（已完成步 1 填表、已自动进步 2）：一句话设定「2050」'
              '题材标签「穿越、星际、异界、基建、军事」笔名「星烬」。'
              '笔名未选就 query_profiles 查一个最匹配的并 drive_ui(set_field pen) 补填。')
STEP3_TASK = ('请继续建这本新书（步 2 已选定候选「2050：星港从零建起」），自主生成填写步 3 '
              '内容表单及故事线：挑选弧 → 构建核心矛盾core_conflict → 创建势力 → '
              '挑选情节段 → 创建各势力人物适配 → 补全其余表单 → 评判合理性及阅读吸引力并修改。'
              '填完表单后停下，向用户汇报设定概要并等待确认；用户明确确认后再提交建书。')

CASES = {
    # —— 各阶段正例（语义不应因本次改动而变化）——
    "请写下一章，写完本章后跑门禁并读取风格规则": "write",
    "继续写 book_002 第2章正文，写完本章全部情节段": "write",
    "请给这本书标记完本": "publish",
    "检查能否发书并上架": "publish",
    "请续规划下一段弧后再写": "replan",
    "整理枫落的风格规则和禁词": "style",
    "创建新书候选": "build-candidates",
    "继续补全世界观并建书": "build",
    "抓取热榜小说用于侦察": "scout",
    # —— 向导两条自动交接原文 ——
    # 步 1 同时含「候选」与「世界观」；步 2→3 同时含「已选定候选」与「步 3/故事线/情节段」。
    # 谁先命中决定了步 3 有没有写工具，这两条是本次修复的核心回归。
    STEP1_TASK: "build-candidates",
    STEP3_TASK: "build",
    # —— 用户口语 ——
    "大纲生成失败": "build",
    "生成故事线": "build",
    "排故事线": "build",
    "生成弧": "build",
    # —— 只读问句仍被正面命中（保留「问一句书的状态」这类正当用法）——
    "查看这本书的状态": "inspect",
    "这本书写到哪了": "inspect",
    # —— 未分类 → 哨兵，不再静默回落只读面 ——
    "今天天气不错": UNROUTABLE_PROFILE,
    "你好": UNROUTABLE_PROFILE,
}


def _assert_wizard_text():
    """向导原文改了要同步本文件的常量，否则上面的回归形同虚设。

    同时守住 [build_session=…] 标记：服务端靠它把任务钉到具体向导会话
    （build_status.json 是全局单快照），向导侧一旦不发，`_build_fsm` 只能退回读快照。
    两条交接任务**都**要带，所以数个数而不是只看存在。
    """
    path = os.path.join(ROOT, "ui", "templates", "start_book.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    for frag, label in (("请为这本新书生成世界观候选", "步 1 交接"),
                        ("请继续建这本新书", "步 2→3 交接")):
        assert frag in html, f"{label}原文已不在 start_book.html，请同步：{frag}"
    n_marker = html.count("[build_session=")
    assert n_marker >= 2, f"两条交接任务都应带 [build_session=…] 标记，实际只有 {n_marker} 处"


def main():
    _assert_wizard_text()
    failed = []
    for task, expected in CASES.items():
        actual = _task_tool_profile(task)
        print(f"[{actual == expected and 'OK' or 'FAIL'}] "
              f"{expected or '<哨兵>'}: {task[:40]}")
        if actual != expected:
            failed.append((task[:40], expected, actual))
    if failed:
        raise AssertionError(failed)
    print(f"\n{len(CASES)} 条全部通过")


if __name__ == "__main__":
    main()
