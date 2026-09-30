#!/usr/bin/env python3
"""建书状态快照（libraries/build_status.py）单测。

**绝不碰 storage/build_status.json**——那是运行中向导的真实快照，覆盖它会污染
用户当前这次建书（test_all.py 就是在那上面「恢复空态」的）。这里把 `_STATUS_PATH`
指到临时文件。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import libraries.build_status as BS  # noqa: E402

TAGS = ["穿越", "星际", "异界", "基建", "军事"]


def main():
    failed = []

    def check(name, cond, detail=""):
        print(f"[{'OK' if cond else 'FAIL'}] {name}{(' ' + detail) if not cond else ''}")
        if not cond:
            failed.append(name)

    real_path = BS._STATUS_PATH
    tmp = os.path.join(tempfile.gettempdir(), "build_status_test.json")
    BS._STATUS_PATH = tmp
    try:
        # ── 1) idea/tags 往返 ──
        BS.set_build_status({"cur": 2, "bookId": "", "buildSessionId": "sid-1",
                             "idea": "2050", "tags": TAGS, "pen_selected": True})
        got = BS.get_build_status()
        check("idea 往返", got["idea"] == "2050", f"idea={got['idea']!r}")
        check("tags 往返且是 list", isinstance(got["tags"], list) and got["tags"] == TAGS,
              f"tags={got['tags']!r}")
        check("会话 id / 步 仍正常", got["build_session_id"] == "sid-1" and got["cur"] == 2)

        # ── 2) 浅拷贝陷阱：改返回值不能污染 _DEFAULTS ──
        got["tags"].append("污染")
        check("改返回值不污染 _DEFAULTS", BS._DEFAULTS["tags"] == [])
        check("再读仍是落盘值", BS.get_build_status()["tags"] == TAGS)

        # ── 3) 空 state（测试清理/宽松阀）→ 复位且不盖章 ──
        BS.set_build_status({})
        empty = BS.get_build_status()
        check("空 state 复位 idea/tags", empty["idea"] == "" and empty["tags"] == [])
        check("空 state 不盖章 updated_at", empty["updated_at"] == "")
        check("空 state 后 _DEFAULTS 仍干净",
              BS._DEFAULTS["tags"] == [] and BS._DEFAULTS["idea"] == "")

        # ── 4) 旧快照（没有这两个键）兼容读 ──
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cur": 3, "build_session_id": "sid-old"}, f)
        old = BS.get_build_status()
        check("旧快照兼容读（补默认值）",
              old["cur"] == 3 and old["idea"] == "" and old["tags"] == [])

        # ── 5) 脏 tags 清洗：只留非空字符串（null 不能变成 "None" 这种假标签） ──
        BS.set_build_status({"tags": ["穿越", "", "  ", None, 42, " 星际 "]})
        cleaned = BS.get_build_status()["tags"]
        check("tags 只留非空字符串且去空白", cleaned == ["穿越", "星际"], f"tags={cleaned!r}")
    finally:
        BS._STATUS_PATH = real_path
        if os.path.exists(tmp):
            os.unlink(tmp)

    # 收尾自证：真实快照没被动过
    live = json.load(open(os.path.join(ROOT, "storage", "build_status.json"), encoding="utf-8"))
    check("真实 build_status.json 未被本测试触碰", "build_session_id" in live)

    if failed:
        raise AssertionError(failed)
    print("\n全部通过")


if __name__ == "__main__":
    main()
