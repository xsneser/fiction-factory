#!/usr/bin/env python3
"""建书转场端点端到端（Flask test client：真实路由 + 真实写盘）。

不起 58080、不打扰已运行的实例——用 app.test_client() 直接打真实路由。验证本次收口的
**第一条链路**：前端点「已挑选完毕」→ `POST /api/build/transition` 原子落阶段 → 服务端
（`_build_fsm`）据此定 profile。回归的是 2026-09-10 那次「先派发 agent、后上报状态」：
状态没落服务端就派发，服务端按旧快照起了 build-candidates profile。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from libraries import build_draft as BD  # noqa: E402
from ui.web_ui import app  # noqa: E402

SID = "selftest-build-transition"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else f"  {detail}"))
    (PASS if cond else FAIL).append(name)


def main():
    path = BD.path_for(SID)
    if os.path.exists(path):
        os.remove(path)
    c = app.test_client()

    def post(url, body):
        r = c.post(url, data=json.dumps(body), content_type="application/json")
        return r.status_code, r.get_json()

    try:
        # ① 转场：一次原子写 step + 表单事实（idea/标签/笔名/选中候选）
        code, d = post("/api/build/transition", {
            "build_session_id": SID, "step": 3,
            "idea": "2050 军工工程师穿越流放营", "tags": ["穿越", "星际", "基地"],
            "pen_name": "星烬",
            "selected_candidate": {"title": "2050：铁陨要塞", "one_liner": "用钢筋混凝土造要塞"}})
        check("POST /api/build/transition ok", code == 200 and d.get("ok"), (code, d))
        rec = BD.load(SID)
        check("转场后 canonical step=3", rec["step"] == 3, rec.get("step"))
        check("转场带上 idea/标签/笔名",
              rec["idea"].startswith("2050") and rec["tags"] == ["穿越", "星际", "基地"]
              and rec["pen_name"] == "星烬", (rec["idea"], rec["tags"], rec["pen_name"]))
        check("转场带上选中候选",
              (rec.get("selected_candidate") or {}).get("title") == "2050：铁陨要塞")
        check("转场 bump revision", int(rec.get("revision") or 0) >= 1)

        # ② 选候选落服务端
        code, d = post("/api/build/pick", {"build_session_id": SID,
                                           "candidate": {"title": "另一方向", "one_liner": "x"}})
        check("POST /api/build/pick ok", code == 200 and d.get("ok"), (code, d))
        check("选择被服务端记住",
              (BD.load(SID).get("selected_candidate") or {}).get("title") == "另一方向")

        # ③ 参数校验：报错必须可读（模型/用户能据此自救）
        code, d = post("/api/build/transition", {"build_session_id": SID, "step": 9})
        check("非法 step 被拒且信息可读",
              code == 400 and "步号" in (d.get("error") or ""), (code, d))
        code, d = post("/api/build/transition", {"step": 3})
        check("缺 session 被拒", code == 400 and "build_session_id" in (d.get("error") or ""), (code, d))
        code, d = post("/api/build/pick", {"build_session_id": SID, "idx": 5})
        check("pick idx 越界被拒", code == 400 and "越界" in (d.get("error") or ""), (code, d))

        # ④ 回退同步（前端 WZ.prev 走同一端点）：step 回到 2
        code, d = post("/api/build/transition", {"build_session_id": SID, "step": 2})
        check("回退步 2 同样原子", code == 200 and BD.load(SID)["step"] == 2, (code, d))

        # ⑤ 既有端点未被破坏
        r = c.post("/api/agent/build-status",
                   data=json.dumps({"cur": 3, "buildSessionId": SID}),
                   content_type="application/json")
        check("POST /api/agent/build-status 仍 200", r.status_code == 200, r.status_code)
    finally:
        if os.path.exists(path):
            os.remove(path)

    print(f"\n=== 建书转场端到端: {len(PASS)} 通过 / {len(FAIL)} 失败 ===")
    if FAIL:
        print("失败:", "、".join(FAIL))
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
