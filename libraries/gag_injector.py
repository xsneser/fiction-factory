"""
灵机一动探测环 — 笑点完全涌现，不写入大纲

理念：人类写作时"灵机一动"才产生笑点。写作时每写完一组短句，跑一个轻量
探测器判断当前这段实际内容有没有天然的笑点缝隙：有 → 把【灵机一动】段
注入下一组写作 prompt（用主角口吻顺势落地）；没有 → 静默续写、绝不硬造。

约定：保持 deepseek-v4-flash；探测器调用输入 ≤500 token、输出 ≤150；
      temperature 0.3；解析失败/越权模式一律视为未命中。
"""
import json
import logging

from core.llm_client import extract_json

logger = logging.getLogger("novel-engine.gag_injector")

DETECTOR_TEMPERATURE = 0.3
# 探测器输出上限：JSON 正文 + flash 推理余量（推理过长会吃掉 max_tokens 导致空返回）
DETECTOR_MAX_TOKENS = 400
DETECTOR_MAX_HINT_CHARS = 60


class GagInjector:
    """灵机一动探测环。"""

    def __init__(self, llm=None, harness=None, gag_lib=None):
        self.llm = llm
        self.harness = harness
        self.gag_lib = gag_lib
        # 情节段内最近命中（供 SSE gag_hit 事件 / 日志）
        self._last_hits: list[dict] = []

    def prescreen_pool(self, plot, book_id: str = "") -> list:
        """免费规则预筛候选笑点模式池（委托 harness，无 harness 时回退按库全量）。"""
        if self.harness and hasattr(self.harness, "prescreen_gag_pool"):
            return self.harness.prescreen_gag_pool(plot, book_id)
        if self.gag_lib:
            patterns = getattr(self.gag_lib, "patterns", [])
            return [g for g in patterns if getattr(g, "enabled", True)][:6]
        return []

    def detect(self, item, recent_text: str, humor_style: str = "",
               pool: list | None = None) -> dict:
        """探测器。返回 {has_opportunity, gag_ids[], reason, deploy_hint}。

        任何异常/缺字段/越权模式 → 一律视为未命中（静默，不打断写作）。
        """
        empty = {"has_opportunity": False, "gag_ids": [], "reason": "", "deploy_hint": ""}
        if not self.llm or not self.harness:
            return empty
        pool = pool or self.prescreen_pool(item["plot"])
        if not pool:
            return empty
        d = self.harness.render_detector_prompt(recent_text, humor_style, pool)
        try:
            raw = self.llm.call(d["system"], d["user"],
                                temperature=DETECTOR_TEMPERATURE,
                                max_tokens=DETECTOR_MAX_TOKENS)
            data = json.loads(extract_json(raw))
        except Exception as e:
            logger.debug("探测器调用/解析失败，视为未命中: %s", e)
            return empty
        if not isinstance(data, dict) or not data.get("has_opportunity"):
            return empty

        pool_ids = {g.id for g in pool}
        gag_ids = [g for g in (data.get("gag_ids") or []) if g in pool_ids][:1]
        if not gag_ids:
            # 报告"有戏"但没选池内模式 → 宁缺毋滥
            return empty

        reason = (str(data.get("reason", "")) or "").strip()[:120]
        deploy_hint = (str(data.get("deploy_hint", "")) or "").strip()
        if not deploy_hint:
            return empty
        if len(deploy_hint) > DETECTOR_MAX_HINT_CHARS:
            deploy_hint = deploy_hint[:DETECTOR_MAX_HINT_CHARS]

        hit = {"has_opportunity": True, "gag_ids": gag_ids,
               "reason": reason, "deploy_hint": deploy_hint}
        self._last_hits.append(hit)
        return hit

    def build_inspiration_hint(self, hit: dict, pool: list | None = None) -> str:
        """命中 → 生成【灵机一动】段文本，注入下一组写作 prompt。"""
        hint = (hit.get("deploy_hint") or "").strip()
        if not hint:
            return ""
        # 落点纪律：用主角口吻、一句收进当前场景、不解释、不标注"笑点"
        return hint + "（用主角口吻、一句收进当前场景、不解释、不标注\"笑点\"）"
