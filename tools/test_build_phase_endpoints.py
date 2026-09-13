#!/usr/bin/env python3
"""分阶段端点的端到端（Flask test client：真实路由 + 真实写盘 + 真实建书门禁）。

不起 58080、不打扰已运行实例——`app.test_client()` 直接打真实路由。

覆盖：
  · `POST /api/build/phase-save`：用户表单回写 canonical，CAS 不等 → 409 且一字不写；
    结构身份字段不可锁 → 400；上游改动使下游失效；
  · `POST /api/build/phase-ack`：只在 `stop_A` 有效（其它 phase no-op，防双推进）；
  · `POST /books/start` 的**四条件提交门禁**：没跑通校验 / 校验后内容又改过 /
    还有失效阶段 → 一律 409，不建出"半成品书"。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import agent_tools as AT  # noqa: E402
from libraries import build_draft as BD  # noqa: E402
from libraries import build_phases as BP  # noqa: E402
from tools.test_build_step3_tools import CHARACTERS, STORYLINE, WORLD  # noqa: E402
from ui.web_ui import app  # noqa: E402

SID = "selftest-build-phase-endpoints"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else f"  {detail}"))
    (PASS if cond else FAIL).append(name)


def main():
    if os.path.exists(BD.path_for(SID)):
        os.remove(BD.path_for(SID))
    c = app.test_client()

    def post(url, body):
        r = c.post(url, data=json.dumps(body), content_type="application/json")
        return r.status_code, r.get_json()

    AT._current_build_session = lambda explicit="": str(explicit or SID)

    try:
        BD.transition(SID, step=3, idea="端点验收", tags=["测试"], pen_name="枫落")

        # ── 1) phase-save：CAS 不等 → 409 且一字不写 ─────────────────────────
        BD.update(SID)                                   # revision +1
        cur = BD.load(SID)["revision"]
        code, d = post("/api/build/phase-save",
                       {"build_session_id": SID, "expected_revision": cur - 1,
                        "sections": {"world": {"world_building": {"core_conflict": "x" * 20}}}})
        check("phase-save 版本不一致 → 409", code == 409 and d.get("error") == "revision_conflict",
              f"{code} {d}")
        check("409 时一个字不写", BD.load(SID)["revision"] == cur)
        check("未知会话 → 404",
              post("/api/build/phase-save", {"build_session_id": "nope", "expected_revision": 0,
                                             "sections": {}})[0] == 404)

        # ── 2) phase-save：结构身份字段不可锁 ────────────────────────────────
        cur = BD.load(SID)["revision"]
        code, d = post("/api/build/phase-save",
                       {"build_session_id": SID, "expected_revision": cur,
                        "sections": {"storyline": STORYLINE},
                        "lock_fields": ["storyline.plots[id=p1].start_word"]})
        check("锁结构身份字段 → 400", code == 400 and d.get("error") == "unlockable_fields",
              f"{code} {d}")

        # ── 3) phase-save：正常回写 + 上游改动使下游失效 ─────────────────────
        cur = BD.load(SID)["revision"]
        code, d = post("/api/build/phase-save",
                       {"build_session_id": SID, "expected_revision": cur,
                        "sections": {"world": WORLD, "characters": CHARACTERS}})
        check("phase-save 成功回写", code == 200 and d.get("ok") is True, f"{code} {d}")
        check("phase-save **不推进** phase（推进只由 agent 落盘 / 用户显式 ack 触发）",
              d["plan_meta"]["phase"] == BP.P_THESIS, d["plan_meta"]["phase"])
        cl = d.get("checklist") or {}
        check("phase-save 顺带把清单带回来（页面据此刷新）",
              isinstance(cl, dict) and cl.get("items")
              and cl["gates"]["passed"] is False and "h0" in cl["gates"]["blocking"],
              str(cl.get("gates")))

        # ── 4) phase-ack：只在 stop_A 有效 ───────────────────────────────────
        code, d = post("/api/build/phase-ack", {"build_session_id": SID})
        check("非 stop_A 调 phase-ack → no-op（防双推进）",
              code == 200 and d.get("advanced") is False and d["phase"] == BP.P_THESIS,
              f"{code} {d}")
        BD.update(SID, on_meta=lambda m: {**m, "phase": BP.P_STOP_A})
        code, d = post("/api/build/phase-ack", {"build_session_id": SID})
        check("stop_A 调 phase-ack → 推进到 executable_horizon",
              d.get("advanced") is True and d["phase"] == BP.P_H0, f"{code} {d}")

        # ── 5) 提交门禁：没有校验回执 → 409 ──────────────────────────────────
        def submit(rev):
            return post("/books/start", {
                "build_session_id": SID, "build_source": "canonical", "build_revision": rev,
                "title": "端点验收之书", "words_per_chapter": 3000,
                "pen_name": "枫落", "idea": "端点验收",
            })

        rev = BD.load(SID)["revision"]
        code, d = submit(rev)
        check("还没跑通完整校验 → 409（不建半成品书）",
              code == 409 and "校验" in (d.get("error") or ""), f"{code} {d.get('error')}")

        # ── 6) 走完规划 + 落一次通过校验的草稿 → 门禁放行 ────────────────────
        BD.update(SID, on_meta=lambda m: {**m, "phase": BP.P_VALIDATE})
        r = AT.save_build_draft(world=WORLD, characters=CHARACTERS, storyline=STORYLINE,
                               build_session_id=SID)
        check("完整草稿落盘并跑通校验", r.get("saved") is True, str(r.get("validation", {}).get("issues")))
        rec = BD.load(SID)
        check("回执绑定当前 content_revision",
              rec["plan_meta"]["validated"]["content_revision"] == rec["content_revision"])
        check("无失效阶段", rec["plan_meta"]["stale_phases"] == [],
              str(rec["plan_meta"]["stale_phases"]))

        # ── 7) 门禁的"纯流程写不失效 / 内容改过就失效" ───────────────────────
        BD.update(SID, on_meta=lambda m: {**m, "phase_ack": {"phase": "stop_B"}})
        rec = BD.load(SID)
        check("纯流程写之后回执仍有效（content_revision 没动）",
              rec["plan_meta"]["validated"]["content_revision"] == rec["content_revision"])
        code, d = submit(rec["revision"])
        check("纯流程写后仍可提交（不会因为点了个确认就提交不了）",
              code != 409 or "校验" not in (d.get("error") or ""), f"{code} {d.get('error')}")

        code, d = post("/api/build/phase-save",
                       {"build_session_id": SID, "expected_revision": BD.load(SID)["revision"],
                        "sections": {"storyline": {**STORYLINE, "threads": [
                            {"id": "t1", "name": "改过的线程"}]}}})
        rec = BD.load(SID)
        code, d = submit(rec["revision"])
        # 改内容会让回执失效 OR 让阶段失效——两条都是门禁的分支，409 都必须发生
        check("内容改过之后 → 回执/阶段失效 → 409",
              code == 409 and ("校验" in (d.get("error") or "")
                               or "重新检查" in (d.get("error") or "")),
              f"{code} {d.get('error')}")
    finally:
        if os.path.exists(BD.path_for(SID)):
            os.remove(BD.path_for(SID))

    print("\n=== 分阶段端点端到端: %d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败:", "、".join(FAIL))
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
