#!/usr/bin/env python3
"""验证「全服务单任务」：A 长任务运行中，B 新任务启动应打断 A（error+done），B 正常完成。

只读验证，不建书不改数据。
"""
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

BASE = "http://127.0.0.1:58080"
result = {}


def _consume(body):
    types = []
    try:
        r = requests.post(BASE + "/api/agent/chat", json=body, stream=True, timeout=120)
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            import json as j
            types.append(j.loads(line[6:])["type"])
    except Exception as e:
        types.append("ERR:" + str(e)[:60])
    return types


def task_a():
    result["A"] = _consume({"messages": [{"role": "user", "content": "给书「魔君的外卖」生成完整大纲"}]})


def main():
    t = threading.Thread(target=task_a, daemon=True)
    t.start()
    time.sleep(3)   # A 在跑（dsh 推理中）
    result["B"] = _consume({"messages": [{"role": "user", "content": "列出所有书（只读）"}]})
    t.join(timeout=10)
    a = result.get("A") or []
    b = result.get("B") or []
    print("A(长任务) 事件:", a)
    print("B(新任务) 事件:", b)
    ok = "error" in a and "done" in a and "reply" in b and "done" in b
    print("✅ 单任务语义正确（A 被打断 error+done，B 正常完成）" if ok else "⚠️ 语义不符")


if __name__ == "__main__":
    main()
