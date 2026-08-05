"""
引擎路由器（Engine Router）v2.0
两个完整流程：新书启动 + 现有续写
纯函数路由 + 状态机，所有模块的总调度中心
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
import json
import logging
from datetime import datetime

logger = logging.getLogger("novel-engine.engine")

from libraries.plot import PlotLibrary
from libraries.structure import StructureLibrary
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.profiles import PenNameProfile, ProfileManager
from libraries.book_manager import BookManager, BookConfig
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.character_state import CharacterStateMachine
from libraries.reviewer import ContentReviewer
from libraries.assembler import BookAssemblerPlan
from libraries.prompt_harness import PromptHarness
from libraries.gag_injector import GagInjector
from core.text_utils import count_prose_units


# ═══════════════════════════════════════════════
# 枚举定义
# ═══════════════════════════════════════════════

class BookMode(Enum):
    """图书模式"""
    CONTINUE = "continue"  # 现有续写


class Phase(Enum):
    """引擎阶段"""
    # 通用阶段
    IDLE = "idle"
    OUTLINE = "outline"
    WRITING = "writing"
    REVIEWING = "reviewing"
    DE_AI = "de_ai"
    COMPLETE = "complete"


class Op(Enum):
    """操作指令"""
    COMPLETE = "complete"              # 全书完成
    PAUSE = "pause"                    # 暂停（预算不足等）

    # 续写专用
    PLAN_OUTLINE = "plan_outline"      # 生成大纲
    WRITE_CHAPTER = "write_chapter"    # 写正文
    REVIEW_CHAPTER = "review_chapter"  # 审查
    DE_AI_PASS = "de_ai_pass"          # 去AI味
    CONFIRM_CHAPTER = "confirm"        # 确认发布

    # 蓝图写作（时间线驱动）
    WRITE_TIMELINE_CHAPTER = "write_timeline_chapter"  # 按蓝图写一章


@dataclass
class Instruction:
    """路由指令"""
    op: Op
    chapter_num: int = 0
    chapter_title: str = ""
    reason: str = ""


# ═══════════════════════════════════════════════
# 引擎状态
# ═══════════════════════════════════════════════

@dataclass
class EngineState:
    """引擎全局状态"""
    book_id: str = ""
    book_mode: BookMode = BookMode.CONTINUE   # 当前模式
    phase: Phase = Phase.IDLE

    # 书目信息
    title: str = ""
    pen_name: str = ""
    genre: str = ""
    sub_genre: str = ""
    platform: str = "fanqie"

    # 大纲
    outline_data: dict = field(default_factory=dict)
    structure_template_id: str = ""
    chapters: list[dict] = field(default_factory=list)

    # 组装计划（库材料注入的神器）
    assembler_plan: Optional[BookAssemblerPlan] = None

    # 进度
    current_chapter: int = 0
    total_chapters: int = 500

    # 当前章
    current_content: str = ""
    current_plot_id: str = ""
    current_plot_variables: dict = field(default_factory=dict)

    # 元信息
    started_at: str = ""
    updated_at: str = ""


# ═══════════════════════════════════════════════
# 总引擎
# ═══════════════════════════════════════════════

class NovelEngine:
    """
    小说工厂总引擎 v2.0 — 唯一写作核心 = 桥段写作（timeline_writer）

    入口：
        engine.start_new_book_timeline(timeline, config)  → 时间线书启动（桥段写作）
        engine.continue_book(book_id)                     → 续写（桥段写作）

    流程：时间线书按桥段逐章写作（大纲=故事线）→ 第1章写完自动生成书名/简介。
    """

    def __init__(self, llm_client=None):
        # 子模块
        self.llm = llm_client
        self.plot_lib = PlotLibrary()
        self.struct_lib = StructureLibrary()
        self.gag_lib = GagLibrary()
        self.theme_lib = ThemeLibrary()
        self.profiles = ProfileManager("profiles")
        self.book_mgr = BookManager("books")
        self._books_dir = self.book_mgr.dir
        self.de_ai = DeAIEngine(llm_client)
        self.reviewer = ContentReviewer(llm_client)
        self.profile: Optional[PenNameProfile] = None
        self.char_states = CharacterStateMachine()

        # 蓝图式写作（时间线驱动，新核心）
        self.timeline: Optional[dict] = None          # BookTimeline
        self.timeline_writer = None                    # TimelineChapterWriter
        self._timeline_config: Optional[dict] = None   # 原始时间线配置

        # 状态
        self.state = EngineState()
        self.cost_tracker = CostTracker()
        self.book: Optional[BookConfig] = None

    # ═══════════════════════════════════════════
    # 入口
    # ═══════════════════════════════════════════

    def start_new_book_timeline(self, timeline: dict, config: dict = None,
                                source_timeline_id: str = "") -> EngineState:
        """
        🔰 蓝图式新书启动（新核心）

        timeline: BookTimeline 对象（多大纲序列 + 桥段嵌套 + 笑点/内涵）
        config: 可选覆盖配置（pen_name/genre/sub_genre/words_per_chapter）
        source_timeline_id: 来源故事线草稿 id（用于「开始写作」时去重，避免同一草稿重复建书）
        """
        if not self.llm:
            raise RuntimeError("LLM 未配置，无法启动新书")

        # 保存时间线配置
        self._timeline_config = timeline
        self.timeline = timeline

        pen_name = (config or {}).get("pen_name") or timeline.pen_name
        genre = (config or {}).get("genre") or timeline.genre
        sub_genre = (config or {}).get("sub_genre") or timeline.sub_genre
        words_per_chapter = (config or {}).get("words_per_chapter") or timeline.words_per_chapter
        platform = (config or {}).get("platform") or timeline.platform or "fanqie"

        # 总章节数：优先按桥段真实规划字数重算（预计=实际），无桥段则退回大纲范围
        total_ch = self._planned_total_chapters() or 0
        if total_ch <= 0:
            total_ch = max((o.end_chapter for o in timeline.outlines), default=0)
        if total_ch <= 0:
            total_ch = 100

        self.state = EngineState(
            book_mode=BookMode.CONTINUE,   # 蓝图模式直接进入写作
            phase=Phase.WRITING,
            title=timeline.book_title or "(待定)",
            pen_name=pen_name,
            genre=genre,
            sub_genre=sub_genre,
            platform=platform,
            total_chapters=total_ch,
            current_chapter=0,
            started_at=datetime.now().isoformat(),
        )

        # 加载笔名档案
        self.profile = None
        try:
            self.profile = self.profiles.get_by_name(pen_name)
        except Exception as e:
            logger.warning("加载笔名档案失败: %s", e)

        # 初始化集中式 harness 与灵机一动探测环
        self.harness = PromptHarness(timeline=timeline, profile=self.profile,
                                     gag_lib=self.gag_lib, theme_lib=self.theme_lib,
                                     plot_lib=self.plot_lib, platform=platform)
        self.gag_injector = GagInjector(llm=self.llm, harness=self.harness,
                                        gag_lib=self.gag_lib)

        # 灵机一动探测频率旋钮（每 N 短句组探测一次）；持久化到 book.json 供续写读取
        detector_frequency = int((config or {}).get("detector_frequency", 1) or 1)

        # 初始化蓝图写作器
        from .timeline_writer import TimelineChapterWriter

        self.timeline_writer = TimelineChapterWriter(
            timeline=timeline,
            llm_client=self.llm,
            de_ai_engine=self.de_ai,
            reviewer=self.reviewer,
            gag_lib=self.gag_lib,
            plot_lib=self.plot_lib,
            profile=self.profile,
            harness=self.harness,
            gag_injector=self.gag_injector,
            book_id="",
            detector_frequency=detector_frequency,
            budget_checker=self._remaining_budget,
        )

        # 注册主角/配角到角色状态机（含性别/性格/惯用语句/简介；重置防引擎实例复用残留）
        self.char_states = CharacterStateMachine()
        self._register_timeline_characters(timeline)

        # 初始化成本追踪
        self.cost_tracker = CostTracker()
        self.cost_tracker.book_id = "timeline_book"

        # 创建正式图书记录（蓝图模式章节直接落盘）
        try:
            from .book_manager import BookConfig
            book = self.book_mgr.create(
                title=timeline.book_title or "(待定)",
                pen_name=pen_name,
                genre=genre,
                sub_genre=sub_genre,
                platform=platform,
                chapter_count=total_ch,
                structure_template_id="timeline",
                style_profile_id=self.profile.id if self.profile else "",
                source_timeline_id=source_timeline_id,
            )
            self.book = book
            self.state.book_id = book.book_id
            self.cost_tracker.book_id = book.book_id
            book.detector_frequency = detector_frequency
            self.book_mgr.update(book)
            if self.timeline_writer:
                self.timeline_writer.book_id = book.book_id
            # 保存时间线配置到图书目录（统一入口，确保 book.json 与 timeline.json 同目录）
            self.book_mgr.save_timeline(book.book_id, timeline)
        except Exception as e:
            logger.error("创建图书记录失败: %s", e)
            self.book = None

        return self.state

    def _register_timeline_characters(self, tl):
        """从 timeline.basic_info 注册主角与配角到 char_states（性别/性格/惯用语句/简介）。

        register 重名去重保证续写不覆盖动态状态（location/mood/goal）。
        """
        if not tl:
            return
        bi = tl.basic_info or {}
        protag = bi.get("protagonist") or {}
        if protag.get("name"):
            self.char_states.register(
                protag["name"], identity=protag.get("identity", ""),
                gender=protag.get("gender", ""),
                personality=protag.get("personality", ""),
                brief=protag.get("background", ""),
                relationship_to_mc="主角",
            )
        for c in (bi.get("supporting_cast") or []):
            if not isinstance(c, dict) or not c.get("name"):
                continue
            self.char_states.register(
                c["name"], identity=c.get("role", ""),
                gender=c.get("gender", ""),
                personality=c.get("personality", ""),
                catchphrase=c.get("catchphrase", ""),
                brief=c.get("brief", ""),
                relationship_to_mc=c.get("relation", ""),
            )

    def continue_book(self, book_id: str) -> EngineState:
        """
        ♻️ 现有续写入口

        从图书目录恢复全部状态：大纲、角色、伏笔、进度、成本。
        """
        self.book = self.book_mgr.get(book_id)
        if not self.book:
            raise ValueError(f"图书 {book_id} 不存在")

        self.state = EngineState(
            book_mode=BookMode.CONTINUE,
            phase=Phase.IDLE,
            book_id=book_id,
            title=self.book.title,
            pen_name=self.book.pen_name,
            genre=self.book.genre,
            sub_genre=self.book.sub_genre,
            platform=self.book.platform,
            current_chapter=self.book.current_chapter,
            total_chapters=self.book.chapter_count,
            structure_template_id=self.book.structure_template_id,
        )

        # 加载风格档案
        self.profile = None
        if self.book.style_profile_id:
            try:
                self.profile = self.profiles.get(self.book.style_profile_id)
            except Exception as e:
                logger.warning("按 ID 加载笔名档案失败: %s", e)

        # 加载角色状态
        char_path = self._books_dir / book_id / "character_states.json"
        if char_path.exists():
            try:
                self.char_states.load(str(char_path))
            except Exception as e:
                logger.warning("加载角色状态失败 (%s): %s", char_path, e)

        # 加载成本
        cost_path = self._books_dir / book_id / "cost.json"
        try:
            self.cost_tracker = CostTracker.load(str(cost_path))
        except Exception as e:
            logger.warning("加载成本失败 (%s)，使用空追踪器: %s", cost_path, e)
            self.cost_tracker = CostTracker()
        self.cost_tracker.book_id = book_id

        # 加载大纲
        outline = self.book_mgr.get_outline(book_id)
        if outline:
            self.state.outline_data = outline
            self.state.chapters = outline.get("chapters", [])
            self.state.phase = Phase.WRITING

        # 恢复组装计划（桥段/笑点/内涵注入），保证重启后旧书写作不丢失
        plan_path = self._books_dir / book_id / "assembler_plan.json"
        if plan_path.exists():
            try:
                from libraries.assembler import load_plan
                self.state.assembler_plan = BookAssemblerPlan.from_dict(
                    load_plan(str(plan_path)),
                    structure_lib=self.struct_lib,
                    plot_lib=self.plot_lib,
                    gag_lib=self.gag_lib,
                    theme_lib=self.theme_lib,
                )
            except Exception as e:
                logger.warning("恢复组装计划失败 (%s): %s", plan_path, e)

        # 加载时间线（唯一写作核心 = 桥段写作）：无故事线直接报错
        tl = self.book_mgr.load_timeline(book_id)
        if tl is None or not tl.outlines:
            raise ValueError(
                "该书未生成故事线（timeline）。请先在时间线编辑器生成并确认故事线，再进行写作。")
        if tl and tl.outlines:
            self.timeline = tl
            self._derive_chapters_from_timeline(tl)
            # 总章节数按桥段真实规划重算（让书库/详情/写作台进度与实际写作计划一致）
            planned_total = self._planned_total_chapters()
            if planned_total:
                self.state.total_chapters = planned_total
                if self.book and self.book.chapter_count != planned_total:
                    self.book.chapter_count = planned_total
                    self.book_mgr.update(self.book)
            # 时间线书：创建桥段驱动的写作者（撰写/续写统一同一套，支持断点续写）
            if self.timeline_writer is None:
                from .timeline_writer import TimelineChapterWriter
                self.harness = PromptHarness(timeline=tl, profile=self.profile,
                                             gag_lib=self.gag_lib,
                                             theme_lib=self.theme_lib,
                                             plot_lib=self.plot_lib,
                                             platform=self.state.platform)
                self.gag_injector = GagInjector(llm=self.llm, harness=self.harness,
                                                gag_lib=self.gag_lib)
                self.timeline_writer = TimelineChapterWriter(
                    timeline=tl, llm_client=self.llm, de_ai_engine=self.de_ai,
                    reviewer=self.reviewer, gag_lib=self.gag_lib,
                    plot_lib=self.plot_lib, profile=self.profile,
                    harness=self.harness, gag_injector=self.gag_injector,
                    book_id=book_id, budget_checker=self._remaining_budget,
                    detector_frequency=getattr(self.book, "detector_frequency", 1) or 1)
            # 注册主角/配角（续写：register 重名去重，不覆盖已存的动态状态）
            self._register_timeline_characters(tl)

        # 确定当前阶段
        if self.book.current_chapter >= self.book.chapter_count:
            self.state.phase = Phase.COMPLETE
        elif self.state.chapters and self.book.current_chapter > 0:
            self.state.phase = Phase.WRITING
        elif tl and tl.outlines:
            # 时间线书即使一章未写也直接进写作（用 timeline 大纲，永不发 PLAN_OUTLINE）
            self.state.phase = Phase.WRITING
        else:
            self.state.phase = Phase.OUTLINE

        return self.state

    def _derive_chapters_from_timeline(self, tl) -> None:
        """从 BookTimeline 维护"章节 → (大纲, 阶段)"索引，供续写定位使用。

        大纲本身就是故事线配置（timeline.json），不在这里拍平成"每章一段文本"；
        每章写作时由 _storyline_chapter_context 直接从故事线现算大纲/桥段/笑点。
        """
        max_end = max((o.end_chapter for o in tl.outlines), default=0)
        chapters = []
        for ch in range(1, max_end + 1):
            owner = next(
                (o for o in tl.outlines if o.start_chapter <= ch <= o.end_chapter),
                None,
            )
            if not owner:
                chapters.append({"num": ch, "title": f"第{ch}章",
                                 "outline_id": "", "stage_index": -1})
                continue
            stage_index, _ = self._locate_stage(owner, ch)
            chapters.append({
                "num": ch,
                "title": owner.name or f"第{ch}章",
                "outline_id": owner.id,
                "stage_index": stage_index,
            })

        self.state.chapters = chapters
        self.state.total_chapters = max(self.state.total_chapters, max_end)
        self.state.outline_data = {
            "structure": "timeline",
            "chapters": chapters,
            "total_chapters": max_end,
        }

    @staticmethod
    def _locate_stage(owner, ch):
        """返回 (stage_index, stage_dict)：章节 ch 落在大纲 owner 的哪个阶段。"""
        acc = owner.start_chapter
        for si, s in enumerate(owner.stages or []):
            max_ch = s.get("max_ch", 30) if isinstance(s, dict) else 30
            if ch <= acc + max_ch - 1:
                return si, s
            acc += max_ch
        stages = owner.stages or []
        return max(0, len(stages) - 1), (stages[-1] if stages else {})

    def _storyline_chapter_context(self, chapter_num: int):
        """从故事线配置定位本章覆盖的大纲/阶段/桥段/笑点/内涵。

        返回 dict（outline/stage_index/stage/plots/gags/themes/hooks），
        无 timeline 或章节无覆盖时返回 None。
        """
        if not self.timeline or not self.timeline.outlines:
            return None
        owner = next(
            (o for o in self.timeline.outlines if o.start_chapter <= chapter_num <= o.end_chapter),
            None,
        )
        if not owner:
            return None
        stage_index, stage = self._locate_stage(owner, chapter_num)
        plots = [p for p in self.timeline.plots
                 if p.outline_id == owner.id and (p.stage_index == stage_index or stage_index < 0)]
        gags, themes, hooks = [], [], []
        for p in plots:
            for gid in (p.gag_ids or []):
                g = self.gag_lib.get_by_id(gid) if self.gag_lib else None
                gags.append(g.name if g else gid)
            themes.extend(p.theme_hints or [])
            hooks.extend(p.hook_points or [])
        gags = list(dict.fromkeys(gags))
        themes = list(dict.fromkeys(themes))
        hooks = list(dict.fromkeys(hooks))
        return {
            "outline": owner,
            "stage_index": stage_index,
            "stage": stage or {},
            "plots": plots,
            "gags": gags,
            "themes": themes,
            "hooks": hooks,
        }

    def _render_chapter_outline(self, ctx: dict) -> str:
        """把故事线上下文渲染成节拍写作的 chapter_outline（内部写作提示，不是大纲本身）。"""
        o = ctx["outline"]
        stage = ctx.get("stage") or {}
        stage_name = stage.get("name", "") if isinstance(stage, dict) else ""
        events = stage.get("events", []) if isinstance(stage, dict) else []
        parts = [f"【本章来自故事线大纲】{o.name}（第{o.start_chapter}-{o.end_chapter}章）"]
        if stage_name:
            parts.append(f"【阶段】{stage_name}")
        if events:
            parts.append(f"【阶段关键事件】{'、'.join(events[:5])}")
        if ctx.get("plots"):
            lines = []
            for p in ctx["plots"]:
                seg = p.name
                if p.template_structure:
                    seg += f"（{p.template_structure[:80]}）"
                lines.append(seg)
            parts.append(f"【本章桥段】{'；'.join(lines)}")
        if ctx.get("gags"):
            parts.append(f"【可注入笑点】{'、'.join(ctx['gags'][:5])}")
        if ctx.get("themes"):
            parts.append(f"【内涵提示】{'、'.join(ctx['themes'][:3])}")
        return "\n".join(parts)

    # ═══════════════════════════════════════════
    # 路由（纯函数）
    # ═══════════════════════════════════════════

    def route(self) -> Instruction:
        """
        纯函数路由：统一走续写路由（唯一写作核心 = 桥段写作）
        """
        return self._route_continue()

    def _remaining_budget(self) -> float:
        """剩余 LLM 费用预算（元）；成本追踪器未就绪时不限制。"""
        if not self.cost_tracker:
            return float("inf")
        return self.cost_tracker.remaining()

    @staticmethod
    def _review_to_dict(review) -> dict:
        """把 ReviewResult 转成可落盘 JSON 的 dict（含 issues 列表）。"""
        return {
            "passed": bool(getattr(review, "passed", False)),
            "score": int(getattr(review, "score", 0) or 0),
            "summary": str(getattr(review, "summary", "") or ""),
            "issues": [
                {"severity": i.severity, "category": i.category,
                 "description": i.description, "suggestion": i.suggestion}
                for i in (getattr(review, "issues", None) or [])
            ],
        }

    @staticmethod
    def _review_to_hint(review) -> str:
        """把审查问题压成一句句修复提示（注入下一桥段写作 prompt）。"""
        issues = [i for i in (getattr(review, "issues", None) or [])
                  if i.severity in ("error", "warning")]
        hints = [i.description.strip() for i in issues[:3] if (i.description or "").strip()]
        return "；".join(hints)

    @staticmethod
    def _promise_type(category: str) -> str:
        """桥段 category → 承诺类型（免费规则）。"""
        c = category or ""
        if any(k in c for k in ("悬疑", "阴谋", "诡计", "调查")):
            return "mystery"
        if "复仇" in c or "打脸" in c:
            return "payback"
        if "成长" in c or "拜师" in c:
            return "growth"
        if any(k in c for k in ("情感", "羁绊", "日常")):
            return "relationship"
        return "hook"

    def _estimate_bridge_chapter(self, plot) -> int:
        """估算桥段所在章节：大纲起始 + 前面各阶段 max_ch 累计（免费规则，粗略即可）。"""
        o = next((x for x in self.timeline.outlines if x.id == plot.outline_id),
                 None) if self.timeline else None
        if not o:
            return 0
        acc = o.start_chapter
        for si, s in enumerate(o.stages or []):
            if si >= plot.stage_index:
                break
            acc += (s.get("max_ch", 30) if isinstance(s, dict) else 30)
        return acc

    def _build_promise_from_setup(self, p) -> dict:
        """为设局桥段生成读者承诺条目（免费规则）：desc 从桥段名+吸睛点推导，deadline 用收局桥段估算章。"""
        payoff = next((q for q in self.timeline.plots if q.resolves_plot_id == p.id), None)
        desc = f"「{p.name}」埋下的钩子"
        if getattr(p, "hook_points", None):
            desc += "（" + p.hook_points[0] + "）"
        return {
            "id": f"promise_{len(getattr(self.timeline, 'promises', None) or []) + 1:04d}",
            "setup_plot_id": p.id,
            "type": self._promise_type(p.category),
            "desc": desc,
            "status": "pending",
            "setup_chapter": getattr(p, "written_chapter", 0) or 0,
            "deadline_chapter": self._estimate_bridge_chapter(payoff) if payoff else 0,
            "payoff_plot_id": payoff.id if payoff else "",
            "payoff_chapter": 0,
        }

    def _update_promises_ledger(self, chapter_num: int) -> None:
        """桥段写完后的免费规则承诺登记：设局桥段 → 新增 pending；收局桥段 → 标记 fulfilled。

        模拟人类作者的"伏笔账本"：埋了记下，还了勾销；逾期由 render_bridge_prompt 注入提醒。
        """
        if not self.timeline:
            return
        promises = list(getattr(self.timeline, "promises", None) or [])
        by_setup = {p.get("setup_plot_id", ""): p for p in promises if p.get("setup_plot_id")}
        changed = False
        for p in self.timeline.plots:
            if (getattr(p, "written_chapter", 0) or 0) != chapter_num:
                continue
            rpid = getattr(p, "resolves_plot_id", "") or ""
            # 收局：兑现对应承诺
            if rpid and rpid in by_setup and by_setup[rpid].get("status") != "fulfilled":
                by_setup[rpid]["status"] = "fulfilled"
                by_setup[rpid]["payoff_plot_id"] = p.id
                by_setup[rpid]["payoff_chapter"] = chapter_num
                changed = True
            # 设局：被收局桥段引用且未登记 → 新增 pending 承诺
            if (not rpid and p.id not in by_setup
                    and any(q.resolves_plot_id == p.id for q in self.timeline.plots if q.id != p.id)):
                promises.append(self._build_promise_from_setup(p))
                changed = True
        if changed:
            self.timeline.promises = promises
            try:
                self.book_mgr.save_timeline(self.state.book_id, self.timeline)
            except Exception as e:
                logger.warning("保存读者承诺台账失败: %s", e)

    def _route_continue(self) -> Instruction:
        """♻️ 续写路由"""
        s = self.state

        # 1. 完本
        if s.current_chapter >= s.total_chapters and s.current_content:
            return Instruction(Op.COMPLETE, reason="全书完成")

        # 2. 还没有大纲
        if not s.outline_data and not s.chapters:
            return Instruction(Op.PLAN_OUTLINE, reason="需要生成大纲")

        # 3. 预算检查
        if self.cost_tracker.remaining() <= 0:
            return Instruction(Op.PAUSE,
                               reason=f"预算耗尽 ({self.cost_tracker.spent:.2f}/{self.cost_tracker.budget})")

        # 4. 当前章需要审查
        if s.current_content and s.phase == Phase.REVIEWING:
            return Instruction(Op.REVIEW_CHAPTER, s.current_chapter,
                               reason="审查本章")

        # 5. 审查通过 → 去AI味
        if s.current_content and s.phase == Phase.DE_AI:
            return Instruction(Op.DE_AI_PASS, s.current_chapter,
                               reason="去AI味处理")

        # 6. 写下一章
        next_ch = s.current_chapter + 1
        return Instruction(Op.WRITE_CHAPTER, next_ch,
                           reason=f"写第 {next_ch} 章")

    # ═══════════════════════════════════════════
    # 执行
    # ═══════════════════════════════════════════

    def execute(self, inst: Instruction) -> dict:
        """执行路由指令，分发到对应处理器"""
        handlers = {
            Op.COMPLETE:        self._exec_complete,
            Op.PAUSE:           self._exec_pause,
            Op.WRITE_CHAPTER:   self._exec_write_chapter,
            Op.WRITE_TIMELINE_CHAPTER: self._exec_write_timeline_chapter,
        }
        handler = handlers.get(inst.op)
        if handler:
            return handler(inst)
        return {"status": "unknown_op", "op": inst.op.value}

    # ─── 通用 ───

    def _exec_complete(self, inst: Instruction) -> dict:
        return {"status": "complete", "message": "全书完成"}

    def _exec_pause(self, inst: Instruction) -> dict:
        return {"status": "paused", "reason": inst.reason}
    def _exec_write_chapter(self, inst: Instruction) -> dict:
        """写一章（唯一写作核心 = 桥段写作）。

        统一委托给桥段驱动的 timeline_writer（按桥段生成、满章切分）；
        无故事线桥段时报错（需先在时间线编辑器生成并确认桥段）。
        """
        if not (self.timeline_writer and self.timeline and self.timeline.plots):
            raise RuntimeError(
                "该书未生成故事线桥段（timeline.plots 为空）。请先在时间线编辑器生成并确认桥段，再进行写作。")
        return self._exec_write_timeline_chapter(inst)

    def _prepare_chapter_context(self, chapter_num: int):
        """取本章写作上下文：上文结尾 + 角色状态 + 进行中章节草稿 + 已完成章节语义摘要。

        返回 (prev_ending, char_states, chapter_buffer, chapter_words, summaries_context)。
        """
        prev_ending = ""
        if chapter_num > 1:
            try:
                prev_ch = self.book_mgr.load_chapter(
                    self.state.book_id, chapter_num - 1)
                if prev_ch:
                    prev_ending = prev_ch.get("content", "")[-500:]
            except Exception as e:
                logger.warning("加载上一章结尾失败（继续写作）: %s", e)

        char_states = ""
        try:
            char_states = self.char_states.build_context_prompt(chapter_num=chapter_num)
        except Exception as e:
            logger.warning("构建角色上下文失败（继续写作）: %s", e)

        buffer, words = [], 0
        try:
            draft = self._load_draft()
            if draft and draft.get("chapter_num") == chapter_num:
                buffer = draft.get("buffer", []) or []
                words = int(draft.get("words", 0) or 0)
        except Exception as e:
            logger.warning("恢复章节草稿失败: %s", e)

        # 跨章长程记忆：最近 3-5 章语义摘要（升序，从旧到新）
        summaries_context = ""
        if self.book:
            try:
                summaries = self.book_mgr.load_chapter_summaries(
                    self.state.book_id, chapter_num, limit=5)
                if summaries:
                    lines = [f"- 第{s['num']}章：{s['summary']}" for s in reversed(summaries)]
                    summaries_context = "\n".join(lines)
            except Exception as e:
                logger.warning("加载章节摘要失败（继续写作）: %s", e)
        return prev_ending, char_states, buffer, words, summaries_context

    def _summarize_chapter(self, chapter_num: int, full_text: str, ctx=None) -> str:
        """为刚写完的一章生成 80-150 字语义摘要（跨章长程记忆）。

        ctx：_storyline_chapter_context 的结果（含本桥段名），可为 None。
        输入 ≤500 token、输出 ≤256、temperature 0.3；失败返回空串。
        """
        if not self.llm or not full_text or not self.harness:
            return ""
        content_tail = full_text[-600:]
        bridge_line = ""
        if ctx:
            outline_name = ctx.get("outline").name if ctx.get("outline") else ""
            plot_names = "；".join(p.name for p in (ctx.get("plots") or [])[:3])
            bridge_line = " / ".join(x for x in [outline_name, plot_names] if x)
        d = self.harness.render_summary_prompt(content_tail, bridge_line)
        try:
            from core.llm_client import extract_json
            raw = self.llm.call(d["system"], d["user"],
                                temperature=0.3, max_tokens=1024)
            data = json.loads(extract_json(raw))
            s = (data.get("summary") or "").strip()
            return s[:150] if s else ""
        except Exception as e:
            logger.warning("生成章节摘要失败: %s", e)
            return ""

    def _generate_book_meta(self, chapter1_text: str) -> dict:
        """第 1 章写完后自动生成书名+简介，落盘 book.json / timeline.json / outline.json。

        从老书名生成器抢救改造，去掉旧新书状态依赖，接入时间线流。
        异常兜底为 best-effort：书名缺省保留原标题，简介缺省为空。
        """
        if not self.llm or not self.book:
            return {"status": "skip"}
        genre = self.state.genre or ""
        sub_genre = self.state.sub_genre or ""
        platform = self.state.platform or "fanqie"
        ch1 = chapter1_text or ""

        from core.llm_client import extract_json
        import libraries.book_meta as book_meta

        best = self.book.title or ""
        synopsis = ""
        raw_title = raw_syn = ""
        title_prompt = synopsis_prompt = ""
        # 书名
        try:
            title_prompt = book_meta.build_title_prompt(genre, sub_genre, platform, ch1)
            raw_title = self.llm.call(
                "你是一位专业的网文编辑。请只返回JSON，不要加任何额外文字。",
                title_prompt, temperature=0.8, max_tokens=1024)
            title_data = json.loads(extract_json(raw_title))
            best = (title_data.get("best") or "").strip() or best
        except Exception as e:
            logger.warning("书名生成失败，沿用原标题: %s", e)
        # 简介
        try:
            synopsis_prompt = book_meta.build_synopsis_prompt(genre, sub_genre, platform, ch1)
            raw_syn = self.llm.call(
                "你是一位专业的网文编辑。请只返回JSON，不要加任何额外文字。",
                synopsis_prompt, temperature=0.8, max_tokens=1024)
            syn_data = json.loads(extract_json(raw_syn))
            synopsis = (syn_data.get("synopsis") or "").strip()
        except Exception as e:
            logger.warning("简介生成失败: %s", e)

        if raw_title or raw_syn:
            self.cost_tracker.record("book_meta", (title_prompt or "") + (synopsis_prompt or ""),
                                     raw_title + raw_syn)

        # 落盘
        if best:
            self.state.title = best
            if self.book.title != best:
                self.book.title = best
                try:
                    self.book_mgr.update(self.book)
                except Exception as e:
                    logger.warning("更新 book.title 失败: %s", e)
            if self.timeline and self.timeline.book_title != best:
                try:
                    self.timeline.book_title = best
                    self.book_mgr.save_timeline(self.state.book_id, self.timeline)
                except Exception as e:
                    logger.warning("更新 timeline.book_title 失败: %s", e)
        if synopsis:
            try:
                outline = self.book_mgr.get_outline(self.state.book_id) or {}
                outline["synopsis"] = synopsis
                self.book_mgr.save_outline(self.state.book_id, outline)
            except Exception as e:
                logger.warning("保存简介失败: %s", e)

        return {"status": "book_meta_generated", "title": best, "synopsis": synopsis}

    def _finalize_written_chapter(self, chapter_num: int, result: dict) -> dict:
        """桥段写完后的收尾：持久化进度/角色/章节/成本。"""
        full_text = result["text"]
        # timeline 路径补一次免费规则层去AI味（词替换+段落节奏），与节拍路径行为一致；
        # 仅在桥段写完落盘前处理，word_count 仍以写作时统计为准。
        try:
            full_text = self.de_ai.process_rule_based(full_text).processed
        except Exception as e:
            logger.warning("去AI味失败: %s", e)
        self.state.current_chapter = chapter_num
        # 进度写回 book.json：否则书库/详情页看不到已写章节与进度
        if self.book and self.book.current_chapter < chapter_num:
            try:
                self.book.current_chapter = chapter_num
                self.book.status = "writing"
                self.book_mgr.update(self.book)
            except Exception as e:
                logger.warning("更新图书进度失败: %s", e)

        # 持久化桥段写入进度（written_chapter），供断点续写
        try:
            if self.book and self.timeline:
                self.book_mgr.save_timeline(self.state.book_id, self.timeline)
        except Exception as e:
            logger.warning("保存时间线进度失败: %s", e)

        self.state.current_content = full_text
        self.state.phase = Phase.REVIEWING

        # 更新角色状态
        try:
            self.char_states.update_from_chapter(chapter_num, full_text)
        except Exception as e:
            logger.warning("更新角色状态失败: %s", e)

        self._save_continue_state()

        # 生成章节语义摘要（跨章长程记忆），随章节一起落盘
        summary = ""
        try:
            ctx = self._storyline_chapter_context(chapter_num) if self.timeline else None
            summary = self._summarize_chapter(chapter_num, full_text, ctx)
        except Exception as e:
            logger.warning("生成章节摘要失败: %s", e)

        # 章节门禁：规则审查（免费规则层，不调 LLM）。失败不阻断（不加写完重写），
        # 只把修复提示注入下一桥段写作 prompt，让模型带着教训继续写。
        review = None
        review_hint = ""
        try:
            target_words = (self.timeline.words_per_chapter
                            if getattr(self, "timeline", None) and self.timeline else 3000)
            review = self.reviewer.review(full_text, chapter_num,
                                          chapter_title=f"第{chapter_num}章",
                                          target_words=target_words)
            review_hint = self._review_to_hint(review)
            if review_hint and self.timeline_writer:
                self.timeline_writer.review_hint = review_hint
        except Exception as e:
            logger.warning("章节规则审查失败: %s", e)
        review_dict = self._review_to_dict(review) if review else None

        # 读者承诺台账登记：设局→pending、收局→fulfilled（免费规则）
        try:
            self._update_promises_ledger(chapter_num)
        except Exception as e:
            logger.warning("更新读者承诺台账失败: %s", e)

        # 保存章节
        if self.book:
            try:
                self.book_mgr.save_chapter(
                    self.state.book_id, chapter_num,
                    f"第{chapter_num}章", full_text, summary,
                    review=review_dict)
            except Exception as e:
                logger.warning("保存章节失败: %s", e)

        # 记录成本
        self.cost_tracker.record(f"ch{chapter_num}_timeline", (result.get("input_text") or ""), full_text)

        return {
            "status": "chapter_written",
            "chapter": chapter_num,
            "word_count": result["word_count"],
            "plot_used": "timeline_writer",
            "beats": result["beats"],
            "beat_details": result.get("beat_details", []),
            "blueprint": result.get("blueprint", {}),
            "review": review_dict,
            "cost": round(self.cost_tracker.spent, 4),
        }

    def _write_timeline_chapter_stream(self, chapter_num: int):
        """流式写一章（生成器）：逐桥段 yield bridge_start/group_chunk/bridge_done 事件，
        结束 yield chapter_done。供 SSE 写作端点（批处理/整章）使用。"""
        if not self.timeline_writer:
            raise RuntimeError("蓝图写作器未初始化，请先调用 start_new_book_timeline()")
        self.state.current_chapter = chapter_num
        prev_ending, char_states, buffer, words, summaries = self._prepare_chapter_context(chapter_num)

        gen = self.timeline_writer.write_chapter_stepwise(
            chapter_num, prev_ending, char_states,
            chapter_buffer="\n\n".join(buffer), chapter_words=words,
            summaries_context=summaries)
        result = None
        last_skip = {}
        try:
            while True:
                evt = next(gen)
                if evt.get("type") == "bridge_skip":
                    last_skip = evt
                yield evt
        except StopIteration as si:
            result = si.value

        # 预算耗尽 = 暂停待续：本批次已写内容存为草稿（不固化），与桥段端点行为一致
        if last_skip.get("code") == "budget_exhausted":
            if result and result.get("text"):
                self._save_draft(chapter_num, result["text"].split("\n\n"),
                                 result.get("word_count", 0))
            yield {"type": "budget_paused",
                   "message": last_skip.get("reason", "预算耗尽，暂停写作")}
            return

        # 无剩余桥段 → 全书完成
        if not result or not result.get("text") or result["text"].startswith("["):
            yield {"type": "complete", "message": "没有更多桥段可写（全书完成）"}
            return

        final = self._finalize_written_chapter(chapter_num, result)
        self._clear_draft()
        yield {"type": "chapter_done",
               "chapter": final.get("chapter"),
               "word_count": final.get("word_count"),
               "beats": final.get("beats"),
               "review": final.get("review"),
               "promises": self.timeline.promises if self.timeline else None,
               "cost": final.get("cost")}

    def _write_next_bridge_stream(self):
        """流式写「一个」桥段（生成器）— 新核心：按桥段撰写。

        事件：bridge_start / group_chunk / bridge_done / chapter_done / complete。
        - 桥段写完：持久化 timeline（written_chapter）+ 章节草稿 draft_chapter.json；
        - 若本章累计字数达标：合成全文落盘为章节、清草稿、yield chapter_done。
        """
        if not self.timeline_writer:
            raise RuntimeError("蓝图写作器未初始化，请先调用 start_new_book_timeline()")
        # current_chapter 语义 = 最后「已完成」章节；进行中的章节不递增，
        # 这样下一桥段仍回到本章累计（draft_chapter.json 恢复）。切章时才由
        # _finalize_written_chapter 更新 current_chapter。
        chapter_num = self.state.current_chapter + 1
        total_ch = self.state.total_chapters or self.timeline_writer._total_chapters
        if chapter_num > total_ch:
            yield {"type": "complete", "message": "已写完全部章节"}
            return
        prev_ending, char_states, buffer, words, summaries = self._prepare_chapter_context(chapter_num)

        gen = self.timeline_writer.write_bridge_stepwise(
            chapter_num, prev_ending, char_states,
            chapter_buffer="\n\n".join(buffer), chapter_words=words,
            summaries_context=summaries)
        result = None
        last_skip = {}
        try:
            while True:
                evt = next(gen)
                if evt.get("type") == "bridge_skip":
                    last_skip = evt
                yield evt
        except StopIteration as si:
            result = si.value

        if result is None:
            # 预算耗尽 = 暂停待续：保留进行中草稿（不固化），调高预算后可继续 write-bridge
            if last_skip.get("code") == "budget_exhausted":
                yield {"type": "budget_paused",
                       "message": last_skip.get("reason", "预算耗尽，暂停写作")}
                return
            # complete / 本章已满：若还有进行中的草稿，收尾固化为最后一章，避免半章文本丢失
            self._finalize_leftover_draft()
            return

        # 持久化桥段进度 + 进行中章节草稿
        self.book_mgr.save_timeline(self.state.book_id, self.timeline)
        buffer = buffer + [result["text"]]
        self._save_draft(chapter_num, buffer, result["chapter_words"])

        if result.get("cut_chapter"):
            final = self._finalize_written_chapter(chapter_num, {
                "text": "\n\n".join(buffer),
                "word_count": result["chapter_words"],
                "beats": 0,
                "beat_details": [],
                "input_text": result.get("input_text", ""),
                "blueprint": {
                    "chapter_title": f"第{chapter_num}章",
                    "chapter_num": chapter_num,
                    "total_chapters": total_ch,
                },
            })
            self._clear_draft()
            yield {"type": "chapter_done",
                   "chapter": final.get("chapter"),
                   "word_count": final.get("word_count"),
                   "beats": final.get("beats"),
                   "review": final.get("review"),
                   "promises": self.timeline.promises if self.timeline else None,
                   "cost": final.get("cost")}
        else:
            yield {"type": "chapter_progress",
                   "chapter": chapter_num,
                   "words": result["chapter_words"],
                   "target": self.timeline.words_per_chapter or 3000}

    def _exec_write_timeline_chapter(self, inst: Instruction) -> dict:
        """按蓝图（时间线）写一章 — 新核心（同步版，供非 SSE 路径）"""
        if not self.timeline_writer:
            return {"error": "蓝图写作器未初始化，请先调用 start_new_book_timeline()"}
        chapter_num = inst.chapter_num
        prev_ending, char_states, buffer, words, summaries = self._prepare_chapter_context(chapter_num)
        result = self.timeline_writer.write_chapter(
            chapter_num=chapter_num,
            previous_chapter_ending=prev_ending,
            character_states=char_states,
            chapter_buffer="\n\n".join(buffer),
            chapter_words=words,
            summaries_context=summaries,
        )
        final = self._finalize_written_chapter(chapter_num, result)
        self._clear_draft()
        return final

    def _save_continue_state(self):
        """保存续写状态"""
        if not self.state.book_id:
            return
        book_dir = self._books_dir / self.state.book_id

        # 角色状态
        self.char_states.save(str(book_dir / "character_states.json"))

        # 成本
        self.cost_tracker.save(str(book_dir / "cost.json"))

    # ═══════════════════════════════════════════
    # 章节草稿持久化（按桥段撰写：跨 HTTP 调用/重启恢复进行中的章节）
    # ═══════════════════════════════════════════

    def _draft_path(self) -> Optional[Path]:
        if not self.state.book_id:
            return None
        return self._books_dir / self.state.book_id / "draft_chapter.json"

    def _load_draft(self) -> Optional[dict]:
        p = self._draft_path()
        if not p or not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("加载章节草稿失败: %s", e)
            return None

    def _save_draft(self, chapter_num: int, buffer: list, words: int):
        p = self._draft_path()
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(
                {"chapter_num": chapter_num, "buffer": buffer, "words": words},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("保存章节草稿失败: %s", e)

    def _clear_draft(self):
        p = self._draft_path()
        if p and p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    def _finalize_leftover_draft(self):
        """把进行中的章节草稿固化为最后一章（全书桥段写完/本章已满时的收尾）。"""
        draft = self._load_draft()
        if not draft or not draft.get("buffer"):
            return
        ch = int(draft.get("chapter_num", 0) or 0)
        if ch <= 0:
            return
        buffer = draft.get("buffer", []) or []
        text = "\n\n".join(buffer)
        words = count_prose_units(text)
        try:
            self._finalize_written_chapter(ch, {
                "text": text, "word_count": words, "beats": 0,
                "beat_details": [],
                "input_text": "\n".join(
                    getattr(self.timeline_writer, "_input_texts", []) or []),
                "blueprint": {
                    "chapter_title": f"第{ch}章",
                    "chapter_num": ch,
                    "total_chapters": self.state.total_chapters,
                },
            })
        except Exception as e:
            logger.warning("收尾草稿章节失败: %s", e)
        self._clear_draft()

    def _planned_total_chapters(self) -> Optional[int]:
        """按桥段真实规划字数重算全书章节数（让"进度 X/Y 章"与写作计划一致）。
        无 timeline/桥段时返回 None（沿用原章节数）。"""
        if not (self.timeline and self.timeline.plots):
            return None
        try:
            from libraries.timeline_writer import planned_words
            import math
            total_w = sum(planned_words(p) for p in self.timeline.plots)
            wpc = self.timeline.words_per_chapter or 3000
            return max(1, math.ceil(total_w / wpc))
        except Exception as e:
            logger.warning("重算总章节数失败: %s", e)
            return None
