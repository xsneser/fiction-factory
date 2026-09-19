#!/usr/bin/env python3
"""Critic 判决凭据账本回归（不变量 I1）。

评审门禁要成立，服务端必须能证明「这个 accept 真的来自 Critic」。凭据是那条证明链：
绑定 plot_id + gate_digest、一次性、正文一变即 stale。本测试锁定这四条语义。

用法：python tools/test_plot_review_receipts.py
"""
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from libraries import plot_review_receipts as R  # noqa: E402

BID = "book_zz_receipts_probe"


def _expect_raise(fn, needle, label):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        assert needle in str(exc), f"{label}: 报错不含「{needle}」→ {exc}"
        return
    raise AssertionError(f"{label}: 应被拒绝但没有")


def main():
    shutil.rmtree(os.path.join(_ROOT, "books", BID), ignore_errors=True)
    try:
        rid = R.issue(BID, "p1", "g1", {"verdict": "accept", "confidence": "high",
                                        "issues": [], "rewrite_brief": {}, "rationale": ""},
                      critic_run_id="run-9")
        assert rid.startswith("rr_"), rid
        rec = R.load(BID, rid)
        assert rec["plot_id"] == "p1" and rec["gate_digest"] == "g1", rec
        assert rec["critic_run_id"] == "run-9", rec          # 可追溯到是哪次 Critic run 签的

        # 正常校验通过
        assert R.verify(BID, rid, "p1", "g1")["verdict"] == "accept"

        # ① plot 不匹配（拿 A 段的判决去接受 B 段）
        _expect_raise(lambda: R.verify(BID, rid, "p2", "g1"), "不是针对当前 Plot", "plot 不匹配")
        # ② 正文变化 → digest 变化 → 旧凭据自动 stale（不需要额外失效逻辑）
        _expect_raise(lambda: R.verify(BID, rid, "p1", "g2"), "已失效", "正文变化后 stale")
        # ③ 不存在 / 空 id
        _expect_raise(lambda: R.verify(BID, "rr_nope", "p1", "g1"), "无效", "不存在的凭据")
        _expect_raise(lambda: R.verify(BID, "", "p1", "g1"), "无效", "空凭据")

        # ④ 状态视图：未消费时能被读到（root 从状态里拿它，而不是听 Critic 的口头结论）
        assert (R.latest_for_plot(BID, "p1") or {}).get("receipt_id") == rid
        assert R.latest_for_plot(BID, "p2") is None

        # ⑤ 一次性：消费后再校验/再接受都拒
        R.consume(BID, rid, {"accepted": True})
        _expect_raise(lambda: R.verify(BID, rid, "p1", "g1"), "已使用", "凭据复用")
        assert R.latest_for_plot(BID, "p1") is None, "已消费的凭据不该再出现在状态里"

        # ⑥ 过期即拒（TTL 兜底）
        old = R.TTL_SECONDS
        R.TTL_SECONDS = -1
        try:
            stale = R.issue(BID, "p3", "g3", {"verdict": "accept"})
        finally:
            R.TTL_SECONDS = old
        _expect_raise(lambda: R.verify(BID, stale, "p3", "g3"), "已过期", "过期凭据")
        assert R.latest_for_plot(BID, "p3") is None

        # ⑦ 同一 Plot 多枚凭据时取最新（Critic 可能评审多次，取最后一次）
        a = R.issue(BID, "p4", "g4", {"verdict": "revise_text"})
        b = R.issue(BID, "p4", "g4", {"verdict": "accept"})
        assert R.latest_for_plot(BID, "p4")["receipt_id"] == b != a

        # ⑧ 有界回收：过期且未消费的被清掉，账本不会无限增长
        R._save(BID, {"schema_version": 1, "receipts": {
            "rr_old": {"plot_id": "p9", "gate_digest": "g", "issued_at": 0,
                       "expires_at": 0, "consumed": False}}})
        R.issue(BID, "p5", "g5", {"verdict": "accept"})
        assert R.load(BID, "rr_old") is None, "过期未消费的凭据应被回收"

        print("[OK] 凭据绑定 plot_id + gate_digest，可追溯到 critic_run_id")
        print("[OK] 正文变化 → 旧凭据自动 stale（无需额外失效逻辑）")
        print("[OK] 一次性消费：复用与已消费读取都被拒")
        print("[OK] 过期即拒 + 有界回收 + 同 Plot 取最新")
    finally:
        shutil.rmtree(os.path.join(_ROOT, "books", BID), ignore_errors=True)


if __name__ == "__main__":
    main()
