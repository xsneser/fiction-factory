"""语义环熔断（LoopGuard）—— 进程内检测「无进展循环」与「连续失败」。

背景：dsh 在 phase 未达 ready 时会反复调 get_book_detail 轮询直到死循环
（docs/agent-sidecar-spike-2026-08-20.md）。MCP 是外部 agent → 平台的唯一咽喉，
在这里做确定性熔断（纯规则、零 LLM），所有 MCP 客户端（dsh / Claude Code）都受益。

熔断规则（防误杀）：
  - 近 5 步内同一 (tool, args_digest) 出现 ≥3 次 且 结果摘要不变（无进展）→ 熔断。
    write_next_bridge 同参但结果摘要不同不会误杀；get_book_detail 同参同结果轮询会命中。
  - 连续 ≥5 次工具调用失败 → 熔断。

逃生阀：环境变量 `NOVEL_DISABLE_LOOP_GUARD=1` 关闭（调试图）。
"""
import hashlib
import json
import os
import threading

_DISABLED = os.environ.get("NOVEL_DISABLE_LOOP_GUARD") == "1"

_REPEAT_WINDOW = 5      # 观察近 N 步
_REPEAT_COUNT = 3       # 同签名同结果出现 ≥3 次
_MAX_CONSEC_FAIL = 5    # 连续失败熔断


def _digest(value) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(value)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


class LoopGuard:
    """进程内循环熔断状态机。MCP 进程一个实例；headless 每任务=新进程=干净态。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._steps = []        # 近 window 步 (tool, args_digest, result_digest)
        self._consec_fail = 0

    def before_call(self, tool: str, args: dict) -> None:
        """调用前预检：连续失败已超限 → 熔断报错。"""
        if _DISABLED:
            return
        with self._lock:
            if self._consec_fail >= _MAX_CONSEC_FAIL:
                raise RuntimeError(
                    f"LoopGuard 熔断：连续 {self._consec_fail} 次工具调用失败"
                    "（疑似死循环）。请检查 phase 状态或改变策略后重试。")

    def after_call(self, tool: str, args: dict, ok: bool, result_summary: str) -> None:
        """调用后记账：更新近 window 步与连续失败计数；命中无进展循环 → 熔断。"""
        if _DISABLED:
            return
        with self._lock:
            if not ok:
                self._consec_fail += 1
                return
            self._consec_fail = 0
            arg_digest = _digest(args)
            res_digest = _digest({"s": (result_summary or "")[:200]})
            self._steps.append((tool, arg_digest, res_digest))
            if len(self._steps) > _REPEAT_WINDOW:
                self._steps.pop(0)

            # 近 window 步内同 (tool, args_digest) 出现 ≥3 次 且 结果摘要一致（无进展）→ 熔断
            matches = [(t, a, r) for (t, a, r) in self._steps
                       if t == tool and a == arg_digest]
            if len(matches) >= _REPEAT_COUNT:
                results = {r for (_t, _a, r) in matches}
                if len(results) == 1:   # 全部同参调用结果相同 = 无进展
                    raise RuntimeError(
                        f"LoopGuard 熔断：{tool} 同参调用 ≥{_REPEAT_COUNT} 次且结果无进展"
                        f"（最近 {len(matches)} 次摘要一致）。疑似死循环轮询，请检查 phase 状态或改变策略。")


_guard = LoopGuard()


def get_loop_guard() -> LoopGuard:
    return _guard
