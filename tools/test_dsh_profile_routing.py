#!/usr/bin/env python3
"""dsh 任务文本 → MCP profile 的中文冲突回归。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from libraries.dsh_bridge import _task_tool_profile  # noqa: E402


CASES = {
    "请写下一章，写完本章后跑门禁并读取风格规则": "write",
    "继续写 book_002 第2章正文，写完本章全部情节段": "write",
    "请给这本书标记完本": "publish",
    "检查能否发书并上架": "publish",
    "请续规划下一段弧后再写": "replan",
    "整理枫落的风格规则和禁词": "style",
    "创建新书候选": "build-candidates",
    "继续补全世界观并建书": "build",
    "抓取热榜小说用于侦察": "scout",
}


def main():
    failed = []
    for task, expected in CASES.items():
        actual = _task_tool_profile(task)
        print(f"[{actual == expected and 'OK' or 'FAIL'}] {expected}: {task}")
        if actual != expected:
            failed.append((task, expected, actual))
    if failed:
        raise AssertionError(failed)


if __name__ == "__main__":
    main()
