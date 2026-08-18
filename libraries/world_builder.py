"""
世界观/设定生成器（World Building Generator）— 启动新书前置

把"一句话设定"（或从已有书借鉴的种子）扩展成完整的「设定圣经」：
扩展 world_building 各维度 + 据世界观推导主角/配角/基调等 basic_info。

流程（2 次链式 LLM 调用；保持 deepseek-v4-flash，只优化 prompt，不换模型）：
  Call A 一句话 → 世界观设定短文（流式，叙事化，temp≈0.8）
  Call B 设定短文 → 结构化 JSON（扩展世界观 + 主角/配角推导，temp≈0.5，失败重试≤3）

三种启动方式：
  (a) 一句话自由输入        generate(idea=...)
  (b) 示例候选              generate_candidates() → 点选 one_liner 回填后再 generate
  (c) 从已有书借鉴          extract_seed(源书 basic_info) → generate(seed_basic_info=...)

落盘语义：merge_basic_info(现有, 生成) 保留用户已填非空字段；末尾打 _world_generated 标记。
"""
from typing import Callable, Optional
import json

from .storyline import (
    BookStoryline, merge_basic_info, DEFAULT_WORLD_BUILDING,
    get_mc, get_characters, relation_to_mc, normalize_basic_info,
)
from .prompt_harness import (
    PromptHarness, WORLD_BUILD_SYSTEM, WORLD_BUILD_STRUCT_SYSTEM, WORLD_CANDIDATES_SYSTEM,
)


_WORLD_SCALAR_KEYS = ("description", "era", "power_system", "geography", "culture",
                      "history", "social_structure", "core_conflict", "world_summary")


class WorldBuildingGenerator:
    """世界观/设定生成器 — 从一句话到完整设定圣经。"""

    def __init__(self, llm_client=None, profile=None, harness=None):
        self.llm = llm_client
        self.profile = profile
        self.harness = harness if harness is not None else PromptHarness(profile=profile)

    # ═══════════════════════════════════════════
    # 种子工具（纯函数）
    # ═══════════════════════════════════════════

    @staticmethod
    def extract_seed(basic_info: dict) -> dict:
        """从已有书 basic_info 抽非空字段做借鉴种子（输出旧形态 protagonist/supporting_cast，
        保持 seed_to_text 与『从已有书借鉴』LLM 链路零改动）。"""
        basic_info = basic_info or {}
        seed = {}
        mc = get_mc(basic_info)
        proto_filled = {k: vv for k, vv in mc.items()
                        if k not in ("relations", "archetype_id")
                        and vv not in (None, "", [], {})}
        if proto_filled:
            seed["protagonist"] = {
                "name": mc.get("name", ""), "identity": mc.get("identity", ""),
                "personality": mc.get("personality", ""),
                "golden_finger": mc.get("golden_finger", ""),
                "background": mc.get("brief", "") or mc.get("background", ""),
                "gender": mc.get("gender", ""),
            }
        mc_name = str(mc.get("name", "") or "").strip()
        cast = []
        for c in get_characters(basic_info):
            if str(c.get("name", "") or "").strip() == mc_name:
                continue
            item = {
                "name": c.get("name", ""), "role": c.get("identity", ""),
                "relation": relation_to_mc(c, basic_info),
                "gender": c.get("gender", ""), "title": c.get("title", ""),
                "personality": c.get("personality", ""),
                "catchphrase": c.get("catchphrase", ""),
                "brief": c.get("brief", ""),
            }
            if item["name"]:
                cast.append(item)
        if cast:
            seed["supporting_cast"] = cast
        wb = basic_info.get("world_building") or {}
        filled_wb = {k: vv for k, vv in wb.items() if vv not in (None, "", [], {})}
        if filled_wb:
            seed["world_building"] = filled_wb
        for field in ("tone", "target_audience", "pov", "era_language"):
            v = basic_info.get(field)
            if v not in (None, "", [], {}):
                seed[field] = v
        return seed

    @staticmethod
    def seed_to_text(seed: dict) -> str:
        """把借鉴种子格式化为 prompt 硬约束块文本（供『从已有书借鉴』注入）。"""
        if not seed:
            return ""
        lines = []
        proto = (seed.get("protagonist") or {})
        if proto.get("name"):
            lines.append(f"主角：{proto['name']}"
                         + (f"（{proto.get('identity', '')}）" if proto.get("identity") else ""))
        if proto.get("golden_finger"):
            lines.append(f"金手指：{proto['golden_finger']}")
        wb = (seed.get("world_building") or {})
        wb_label = {
            "description": "世界观种子", "era": "时代", "power_system": "力量体系",
            "geography": "地理", "culture": "文化", "history": "历史",
            "social_structure": "社会结构", "core_conflict": "核心矛盾",
            "world_summary": "设定概述",
        }
        for k, label in wb_label.items():
            v = wb.get(k)
            if v not in (None, "", [], {}):
                lines.append(f"{label}：{str(v)[:120]}")
        factions = wb.get("factions") or []
        if factions:
            fs = [f.get("name", str(f)) if isinstance(f, dict) else str(f) for f in factions[:4]]
            lines.append("势力：" + "、".join(fs))
        rules = wb.get("rules") or []
        if rules:
            lines.append("规则：" + "；".join(str(r)[:60] for r in rules[:5]))
        cast = (seed.get("supporting_cast") or [])
        if cast:
            names = [c.get("name", "") if isinstance(c, dict) else str(c) for c in cast[:3]]
            lines.append("配角：" + "、".join(n for n in names if n))
        for field in ("tone", "target_audience", "pov", "era_language"):
            if seed.get(field):
                lines.append(f"{field}：{seed[field]}")
        return "\n".join(lines)

    # ═══════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════

    def generate(self, genre: str = "", sub_genre: str = "", idea: str = "",
                 pen_name: str = "", platform: str = "",
                 seed_basic_info=None,
                 storyline: Optional[BookStoryline] = None,
                 on_save: Optional[Callable[[BookStoryline], None]] = None):
        """一句话(或借鉴种子) → 世界观设定文 → 结构化 JSON。yield SSE 事件。

        storyline: 传入现有 BookStoryline 则原地累加（保留书名/字数等书配置与用户已填设定）；
                   None 则新建。seed_basic_info 非空即走「借鉴变体」。
        事件: phase/progress/thinking/world_summary/phase_done/done/error
        """
        tl = storyline if storyline is not None else BookStoryline(
            genre=genre or "", sub_genre=sub_genre or "",
            pen_name=pen_name or "", platform=platform or "fanqie",
        )
        self.harness.storyline = tl
        g = genre or tl.genre
        sg = sub_genre or tl.sub_genre
        pn = pen_name or tl.pen_name
        pf = platform or tl.platform or "fanqie"
        bi = tl.basic_info or {}
        idea = (idea or "").strip() or str((bi.get("world_building") or {}).get("description", "") or "").strip()

        if not self.llm:
            yield ("error", "LLM 未配置", {})
            return

        try:
            # ── Call A: 世界观设定短文 ──
            yield ("phase", "世界观设定", {"phase": 1, "total": 2,
                   "desc": "把一句话设定扩展成世界观设定短文..."})
            yield ("progress", "构思世界设定...", {})
            summary = yield from self._draft_world_summary(g, sg, idea, pf, seed_basic_info)
            summary = (summary or "").strip()
            if not summary:
                raise RuntimeError("世界观设定短文生成失败（Call A 无输出）")
            yield ("world_summary", "世界观设定短文已生成", {"summary": summary})
            yield ("phase_done", "世界观设定完成", {"phase": 1, "data": {"chars": len(summary)}})

            # ── Call B: 结构化 JSON ──
            yield ("phase", "结构化提取", {"phase": 2, "total": 2,
                   "desc": "从设定文提取世界观/主角/配角并落盘..."})
            yield ("progress", "提取结构化设定...", {})
            generated = yield from self._extract_world_struct(g, sg, idea, summary, seed_basic_info)
            if not generated:
                raise RuntimeError("结构化设定提取失败（Call B 连续多次解析失败）")

            merged = merge_basic_info(tl.basic_info, generated)
            self._backfill_schema(merged)
            merged["_world_generated"] = True
            tl.basic_info = merged
            yield ("phase_done", "结构化提取完成", {"phase": 2, "data": {
                "protagonist": get_mc(merged),
                "world_building": merged.get("world_building", {}),
            }})
            if on_save:
                on_save(tl)
            yield ("done", "世界观生成完成", {"basic_info": merged})
        except Exception as e:
            import traceback
            yield ("error", str(e), {"traceback": traceback.format_exc()})

    # ═══════════════════════════════════════════
    # 示例候选
    # ═══════════════════════════════════════════

    def generate_candidates(self, genre: str = "", sub_genre: str = "",
                            idea: str = "", count: int = 3) -> list:
        """示例候选：一次产出 count 个差异化世界观候选（非流式 JSON 端点，失败重试≤3）。"""
        if not self.llm:
            return []
        prompt = self.harness.render_world_candidates_prompt(
            idea=idea, genre=genre or "", sub_genre=sub_genre or "", count=count)
        from core.llm_client import extract_json
        for attempt in range(3):
            try:
                # 推理型模型：max_tokens 留足推理+内容余量（同 outline_generator 用 8192）
                raw = self.llm.call(WORLD_CANDIDATES_SYSTEM, prompt,
                                    temperature=0.9, max_tokens=8192)
                data = json.loads(extract_json(raw))
                cands = [c for c in (data.get("candidates") or [])
                         if isinstance(c, dict) and c.get("one_liner")][:count]
                if cands:
                    return cands
            except Exception:
                pass
        return []

    # ═══════════════════════════════════════════
    # 内部：2 次链式 LLM 调用
    # ═══════════════════════════════════════════

    def _draft_world_summary(self, genre: str, sub_genre: str, idea: str,
                             platform: str, seed_basic_info):
        """Call A：一句话 → 世界观设定短文（流式转发 thinking，返回短文全文）。失败重试≤3。"""
        prompt = self.harness.render_world_build_draft_prompt(
            idea=idea, genre=genre, sub_genre=sub_genre,
            seed_basic_info=seed_basic_info, platform=platform)
        last_err = None
        for attempt in range(3):
            collected = []
            try:
                # 推理型模型：max_tokens 必须留足推理余量（同 outline_generator 用 8192）
                for delta_key, text in self.llm.stream_deltas(
                        WORLD_BUILD_SYSTEM, prompt, temperature=0.8, max_tokens=4096):
                    yield ("thinking", "world_draft", {"stream": text, "mode": delta_key})
                    if delta_key == "content":
                        collected.append(text)
                if "".join(collected).strip():
                    return "".join(collected)
                last_err = RuntimeError("Call A 无内容输出")
            except Exception as e:
                last_err = e
            yield ("progress", f"世界观设定文生成失败，重试（{attempt + 1}/3）...", {})
        yield ("error", f"世界观设定短文生成失败：{last_err}", {})
        return ""

    def _extract_world_struct(self, genre: str, sub_genre: str, idea: str,
                              summary: str, seed_basic_info):
        """Call B：设定文 → 结构化 JSON（失败重试 ≤3）。返回 basic_info dict。"""
        from core.llm_client import extract_json
        for attempt in range(3):
            prompt = self.harness.render_world_build_struct_prompt(
                world_summary=summary, idea=idea, genre=genre, sub_genre=sub_genre,
                seed_basic_info=seed_basic_info, profile=self.profile)
            collected = []
            try:
                # 推理型模型：max_tokens 留足推理+大 JSON 余量（同 outline_generator 用 8192）
                for delta_key, text in self.llm.stream_deltas(
                        WORLD_BUILD_STRUCT_SYSTEM, prompt, temperature=0.5, max_tokens=8192):
                    yield ("thinking", "world_struct", {"stream": text, "mode": delta_key})
                    if delta_key == "content":
                        collected.append(text)
            except Exception:
                yield ("progress", f"结构化调用失败，重试（{attempt + 1}/3）...", {})
                continue
            raw = "".join(collected)
            try:
                data = json.loads(extract_json(raw))
                if isinstance(data, dict):
                    return data
            except Exception:
                yield ("progress", f"结构化解析失败，重试（{attempt + 1}/3）...", {})
        return None

    @staticmethod
    def _backfill_schema(basic_info: dict) -> None:
        """兜底补齐 world_building 规范键 + characters 数组规范化（含旧键迁移），避免旧书缺字段。"""
        bi = basic_info or {}
        wb = bi.get("world_building")
        if isinstance(wb, dict):
            for k in DEFAULT_WORLD_BUILDING:
                if k not in wb:
                    v = DEFAULT_WORLD_BUILDING[k]
                    wb[k] = list(v) if isinstance(v, list) else v
            bi["world_building"] = wb
        nb = normalize_basic_info(bi)
        bi.clear()
        bi.update(nb)
