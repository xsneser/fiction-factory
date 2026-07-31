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
from datetime import datetime

from libraries.plot import PlotLibrary, PlotTemplate
from libraries.structure import StructureLibrary, StructureTemplate
from libraries.gag import GagLibrary
from libraries.theme import ThemeLibrary
from libraries.profiles import PenNameProfile, ProfileManager
from libraries.book_manager import BookManager, BookConfig
from libraries.cost_tracker import CostTracker
from libraries.de_ai import DeAIEngine
from libraries.character_state import CharacterStateMachine, CharacterState
from libraries.reviewer import ContentReviewer, ReviewResult
from libraries.new_book import NewBookPipeline, NewBookConfig, Chapter3Result, recommend_opening
from libraries.beat_writer import ChapterWriter, BeatLibrary
from libraries.assembler import BookAssembler, BookAssemblerPlan, PlanInjector


# ═══════════════════════════════════════════════
# 枚举定义
# ═══════════════════════════════════════════════

class BookMode(Enum):
    """图书模式"""
    NEW = "new"            # 新书启动
    CONTINUE = "continue"  # 现有续写


class Phase(Enum):
    """引擎阶段"""
    # 新书启动阶段
    NEW_PLANNING = "new_planning"       # 规划：大纲+角色+伏笔+书名方案
    NEW_CHAPTER1 = "new_chapter1"       # 第一章：钩子+金手指
    NEW_CHAPTER2 = "new_chapter2"       # 第二章：世界观展开
    NEW_CHAPTER3 = "new_chapter3"       # 第三章：首次冲突
    NEW_TITLE = "new_title"             # 书名+简介生成

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

    # 新书启动专用
    PLAN_BOOK = "plan_book"            # 规划全书
    WRITE_CH1 = "write_ch1"            # 写第一章
    WRITE_CH2 = "write_ch2"            # 写第二章
    WRITE_CH3 = "write_ch3"            # 写第三章
    GENERATE_TITLE = "generate_title"  # 生成书名+简介

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
class NewBookState:
    """新书启动的中间状态"""
    # 规划产物
    outline_generated: bool = False
    characters_created: list[dict] = field(default_factory=list)   # [{name, identity, ...}]
    foreshadows_planned: list[dict] = field(default_factory=list)  # [{desc, plant_ch, resolve_ch, ...}]
    opening_plan: dict = field(default_factory=dict)               # plan_opening 返回的结果

    # 前三章产物
    chapter1: str = ""
    chapter2: str = ""
    chapter3: str = ""

    # 书名+简介
    title_options: list[str] = field(default_factory=list)
    best_title: str = ""
    synopsis: str = ""
    title_finalized: bool = False


@dataclass
class EngineState:
    """引擎全局状态"""
    book_id: str = ""
    book_mode: BookMode = BookMode.NEW        # 当前模式
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

    # 新书子状态（仅在 NEW 模式下使用）
    new_book: NewBookState = field(default_factory=NewBookState)


# ═══════════════════════════════════════════════
# 总引擎
# ═══════════════════════════════════════════════

class NovelEngine:
    """
    小说工厂总引擎 v2.0

    两个入口：
        engine.start_new_book(config)   → 新书启动流程
        engine.continue_book(book_id)   → 现有续写流程

    新书启动流程：
        选题 → 规划(大纲+角色+伏笔+书名) → 第一章(钩子+金手指)
        → 第二章(世界观) → 第三章(首次冲突) → 书名简介定稿
        → 自动转入续写循环

    续写流程：
        恢复状态 → 写→审→去AI→下一章 → 循环
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
        self.new_book_pipeline = NewBookPipeline(llm_client)
        self.de_ai = DeAIEngine(llm_client)
        self.reviewer = ContentReviewer(llm_client)
        self.profile: Optional[PenNameProfile] = None
        self.chapter_writer = ChapterWriter(llm_client, self.de_ai, self.reviewer, self.gag_lib, self.profile)
        self.char_states = CharacterStateMachine()

        # 书籍组装器（连接四大库与生成管线）
        self.assembler = BookAssembler(
            plot_lib=self.plot_lib,
            structure_lib=self.struct_lib,
            gag_lib=self.gag_lib,
            theme_lib=self.theme_lib,
            llm_client=llm_client,
        )

        # 蓝图式写作（时间线驱动，新核心）
        self.timeline: Optional[dict] = None          # BookTimeline
        self.timeline_writer = None                    # TimelineChapterWriter
        self._timeline_config: Optional[dict] = None   # 原始时间线配置

        # 状态
        self.state = EngineState()
        self.cost_tracker = CostTracker()
        self.book: Optional[BookConfig] = None

        # 新书配置（start_new_book 时设置）
        self._new_book_config: Optional[NewBookConfig] = None

    # ═══════════════════════════════════════════
    # 入口
    # ═══════════════════════════════════════════

    def start_new_book(self, config: NewBookConfig) -> EngineState:
        """
        🔰 新书启动入口

        设置所有初始状态，进入规划阶段。
        后续调用 run() 或 step() 逐步推进。
        """
        if not self.llm:
            raise RuntimeError("LLM 未配置，无法启动新书")

        self._new_book_config = config
        self.state = EngineState(
            book_mode=BookMode.NEW,
            phase=Phase.NEW_PLANNING,
            title=config.title or "(待定)",
            pen_name=config.pen_name,
            genre=config.genre,
            sub_genre=config.sub_genre,
            platform=config.platform,
            total_chapters=config.chapter_count,
            current_chapter=0,
            started_at=datetime.now().isoformat(),
        )

        # 加载笔名档案
        self.profile = None
        if config.style_profile_id:
            try:
                self.profile = self.profiles.get(config.style_profile_id)
            except Exception:
                pass
        if not self.profile:
            try:
                self.profile = self.profiles.get_by_name(config.pen_name)
            except Exception:
                pass

        # 初始化成本追踪
        self.cost_tracker = CostTracker()
        self.cost_tracker.book_id = "new_book"

        return self.state

    def start_new_book_timeline(self, timeline: dict, config: dict = None) -> EngineState:
        """
        🔰 蓝图式新书启动（新核心）

        timeline: BookTimeline 对象（多大纲序列 + 桥段嵌套 + 笑点/内涵）
        config: 可选覆盖配置（pen_name/genre/sub_genre/words_per_chapter）
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

        # 总章节数 = 时间线里所有大纲的章节范围之和
        total_ch = 0
        if timeline.outlines:
            total_ch = max(o.end_chapter for o in timeline.outlines)
        if total_ch <= 0:
            total_ch = 100

        self.state = EngineState(
            book_mode=BookMode.CONTINUE,   # 蓝图模式直接进入写作
            phase=Phase.WRITING,
            title=timeline.book_title or "(待定)",
            pen_name=pen_name,
            genre=genre,
            sub_genre=sub_genre,
            platform="fanqie",
            total_chapters=total_ch,
            current_chapter=0,
            started_at=datetime.now().isoformat(),
        )

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
        )

        # 加载笔名档案
        self.profile = None
        try:
            self.profile = self.profiles.get_by_name(pen_name)
        except Exception:
            pass
        if self.timeline_writer:
            self.timeline_writer.executor.profile = self.profile

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
                platform="fanqie",
                chapter_count=total_ch,
                structure_template_id="timeline",
                style_profile_id=self.profile.id if self.profile else "",
            )
            self.book = book
            self.state.book_id = book.book_id
            self.cost_tracker.book_id = book.book_id
            # 保存时间线配置到图书目录
            from .timeline import save_timeline
            save_timeline(timeline, f"books/{book.book_id}/timeline.json")
        except Exception as e:
            print(f"[timeline] 创建图书记录失败: {e}")
            self.book = None

        return self.state

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
            except Exception:
                pass

        # 加载角色状态
        char_path = Path("books") / book_id / "character_states.json"
        if char_path.exists():
            try:
                self.char_states.load(str(char_path))
            except Exception:
                pass

        # 加载成本
        cost_path = Path("books") / book_id / "cost.json"
        try:
            self.cost_tracker = CostTracker.load(str(cost_path))
        except Exception:
            self.cost_tracker = CostTracker()
        self.cost_tracker.book_id = book_id

        # 加载大纲
        outline = self.book_mgr.get_outline(book_id)
        if outline:
            self.state.outline_data = outline
            self.state.chapters = outline.get("chapters", [])
            self.state.phase = Phase.WRITING

        # 确定当前阶段
        if self.book.current_chapter >= self.book.chapter_count:
            self.state.phase = Phase.COMPLETE
        elif self.state.chapters and self.book.current_chapter > 0:
            self.state.phase = Phase.WRITING
        else:
            self.state.phase = Phase.OUTLINE

        return self.state

    # ═══════════════════════════════════════════
    # 路由（纯函数）
    # ═══════════════════════════════════════════

    def route(self) -> Instruction:
        """
        纯函数路由：根据 book_mode 和 phase 决定下一步
        """
        if self.state.book_mode == BookMode.NEW:
            return self._route_new_book()
        else:
            return self._route_continue()

    def _route_new_book(self) -> Instruction:
        """🔰 新书启动路由"""
        s = self.state
        nb = s.new_book
        phase = s.phase

        # 1. 规划阶段
        if phase == Phase.NEW_PLANNING:
            if not nb.outline_generated:
                return Instruction(Op.PLAN_BOOK, reason="规划全书：大纲+角色+伏笔+书名方案")
            # 规划完成 → 开始写第一章
            s.phase = Phase.NEW_CHAPTER1
            return Instruction(Op.WRITE_CH1, chapter_num=1, reason="第一章：钩子+金手指+首次危机")

        # 2. 第一章
        if phase == Phase.NEW_CHAPTER1:
            if not nb.chapter1:
                return Instruction(Op.WRITE_CH1, chapter_num=1, reason="第一章：钩子+金手指+首次危机")
            s.phase = Phase.NEW_CHAPTER2
            return Instruction(Op.WRITE_CH2, chapter_num=2, reason="第二章：世界观展开")

        # 3. 第二章
        if phase == Phase.NEW_CHAPTER2:
            if not nb.chapter2:
                return Instruction(Op.WRITE_CH2, chapter_num=2, reason="第二章：世界观展开")
            s.phase = Phase.NEW_CHAPTER3
            return Instruction(Op.WRITE_CH3, chapter_num=3, reason="第三章：首次核心冲突")

        # 4. 第三章
        if phase == Phase.NEW_CHAPTER3:
            if not nb.chapter3:
                return Instruction(Op.WRITE_CH3, chapter_num=3, reason="第三章：首次核心冲突")
            s.phase = Phase.NEW_TITLE
            return Instruction(Op.GENERATE_TITLE, reason="生成书名+简介")

        # 5. 书名+简介
        if phase == Phase.NEW_TITLE:
            if not nb.title_finalized:
                return Instruction(Op.GENERATE_TITLE, reason="生成书名+简介")

            # 新书启动完成 → 转入续写模式
            s.book_mode = BookMode.CONTINUE
            s.phase = Phase.WRITING
            s.current_chapter = 3  # 从第4章开始续写
            return Instruction(Op.WRITE_CHAPTER, chapter_num=4,
                               reason="新书启动完成，开始续写")

        # 不应该到这里
        return Instruction(Op.COMPLETE, reason="未知状态")

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
            Op.PLAN_BOOK:       self._exec_plan_book,
            Op.WRITE_CH1:       self._exec_write_ch1,
            Op.WRITE_CH2:       self._exec_write_ch2,
            Op.WRITE_CH3:       self._exec_write_ch3,
            Op.GENERATE_TITLE:  self._exec_generate_title,
            Op.PLAN_OUTLINE:    self._exec_plan_outline,
            Op.WRITE_CHAPTER:   self._exec_write_chapter,
            Op.REVIEW_CHAPTER:  self._exec_review_current,
            Op.DE_AI_PASS:      self._exec_de_ai_current,
            Op.CONFIRM_CHAPTER: self._exec_confirm_current,
            Op.WRITE_TIMELINE_CHAPTER: self._exec_write_timeline_chapter,
        }
        handler = handlers.get(inst.op)
        if handler:
            return handler(inst)
        return {"status": "unknown_op", "op": inst.op.value}

    # ─── 通用 ───

    def _get_stage_index(self, chapter_num: int) -> int:
        """
        根据章节号反查当前属于哪个大纲阶段（用于给组装计划取材料）。
        """
        plan = self.state.assembler_plan
        if not plan or not plan.stages:
            return 0

        # 新书前三章 → 阶段 0（觉醒/重生）
        if self.state.book_mode == BookMode.NEW and chapter_num <= 3:
            return 0

        # 续写模式：累计章节数反查阶段
        accumulated = 0
        for i, sp in enumerate(plan.stages):
            min_ch, max_ch = sp.chapter_range
            accumulated += min_ch
            if chapter_num <= accumulated:
                return i
        # 超出范围的取最后一个阶段
        return len(plan.stages) - 1

    def _exec_complete(self, inst: Instruction) -> dict:
        return {"status": "complete", "message": "全书完成"}

    def _exec_pause(self, inst: Instruction) -> dict:
        return {"status": "paused", "reason": inst.reason}

    # ═══════════════════════════════════════════
    # 🔰 新书启动执行器
    # ═══════════════════════════════════════════

    def _exec_plan_book(self, inst: Instruction) -> dict:
        """
        Phase: 规划全书

        完成：
        1. 开篇方案推荐（选桥段+大纲模板）
        2. 展开大纲结构（卷/弧/章层级）
        3. 创建初始角色（主角+反派+主要配角）
        4. 规划伏笔方案
        5. 生成书名方案（先占位，等前三章写好再精调）
        """
        config = self._new_book_config
        if not config:
            raise RuntimeError("没有新书配置，请先调用 start_new_book()")

        nb = self.state.new_book

        # ── 1. 开篇方案 ──
        nb.opening_plan = self.new_book_pipeline.plan_opening(config)

        # ── 2. 大纲结构 ──
        struct = self.struct_lib.search(
            genre=config.genre,
            sub_genre=config.sub_genre,
            chapter_count=config.chapter_count)
        template = struct[0] if struct else None

        if template:
            self.state.structure_template_id = template.id
            self.state.outline_data = {
                "structure": template.id,
                "structure_name": template.name,
                "stages": [s.__dict__ for s in template.stages],
                "total_chapters": template.total_chapters,
            }
            # 从 stages 生成章节列表
            ch_num = 1
            self.state.chapters = []
            for stage in template.stages:
                for _ in range(stage.min_chapters):
                    self.state.chapters.append({
                        "num": ch_num,
                        "title": f"{stage.name} ({ch_num})",
                        "stage": stage.name,
                        "outline": "",
                    })
                    ch_num += 1
            self.state.total_chapters = len(self.state.chapters)

        # ── 3. 初始角色创建 ──
        nb.characters_created = self._generate_initial_characters(config)

        # ── 4. 伏笔规划 ──
        nb.foreshadows_planned = self._generate_foreshadows(config)

        # ── 5. 书名占位（等前三章写好再精调） ──
        nb.title_options = [config.title] if config.title else [f"{config.genre}之{config.pen_name}"]

        nb.outline_generated = True

        # ── 6. 组装计划：按大纲逐阶段匹配桥段/笑点/内涵 ──
        self.state.assembler_plan = self.assembler.assemble_book(
            genre=config.genre,
            sub_genre=config.sub_genre,
            title_hint=config.title,
        )

        self.state.updated_at = datetime.now().isoformat()

        return {
            "status": "book_planned",
            "phase": "new_planning",
            "structure": self.state.structure_template_id,
            "total_chapters": self.state.total_chapters,
            "characters": len(nb.characters_created),
            "foreshadows": len(nb.foreshadows_planned),
            "opening_plot": nb.opening_plan.get("opening_plot").name
                             if nb.opening_plan.get("opening_plot") else "auto",
            "book_title": self.state.assembler_plan.book_title,
            "themes": [t.name for t in self.state.assembler_plan.themes],
            "stages_matched": len(self.state.assembler_plan.stages),
        }

    def _exec_write_ch1(self, inst: Instruction) -> dict:
        """第一章：钩子 + 金手指激活 + 首个危机（节拍级写作）"""
        config = self._new_book_config
        nb = self.state.new_book

        ch1_outline = (
            f"第一章：钩子+金手指+首个危机\n"
            f"流派：{config.genre}/{config.sub_genre}\n"
            f"任务：前200字有强烈钩子→介绍主角处境→触发第一个危机→激活金手指\n"
            f"核心要求：主角用幽默自嘲面对困境，奠定网文爽感基调"
        )

        self.chapter_writer.executor.profile = self.profile
        result = self.chapter_writer.write_chapter(
            chapter_num=1, chapter_outline=ch1_outline,
            target_words=config.words_per_chapter or 3000,
            genre=config.genre, pen_name=config.pen_name,
            assembler_plan=self.state.assembler_plan,
            stage_index=self._get_stage_index(1))

        nb.chapter1 = result["text"]
        self.cost_tracker.record("ch1_draft", "", result["text"])
        self._save_new_book_chapter(1, "钩子·金手指·首次危机", nb.chapter1)
        self.state.updated_at = datetime.now().isoformat()

        return {"status": "ch1_done", "chapter": 1,
                "word_count": result["word_count"], "beats": result["beats"]}

    def _exec_write_ch2(self, inst: Instruction) -> dict:
        """第二章：世界观展开 + 能力初试（节拍级写作）"""
        config = self._new_book_config
        nb = self.state.new_book
        prev_end = nb.chapter1[-500:] if nb.chapter1 else ""

        ch2_outline = (
            f"第二章：世界观展开+能力初试\n"
            f"流派：{config.genre}/{config.sub_genre}\n"
            f"任务：展示世界观→第一次使用金手指→建立日常节奏→章末中等钩子\n"
            f"承接上文：{nb.chapter1[-200:] if nb.chapter1 else ''}..."
        )

        self.chapter_writer.executor.profile = self.profile
        result = self.chapter_writer.write_chapter(
            chapter_num=2, chapter_outline=ch2_outline,
            target_words=config.words_per_chapter or 3000,
            genre=config.genre, pen_name=config.pen_name,
            previous_chapter_ending=prev_end,
            assembler_plan=self.state.assembler_plan,
            stage_index=self._get_stage_index(2))

        nb.chapter2 = result["text"]
        self.cost_tracker.record("ch2_draft", "", result["text"])
        self._save_new_book_chapter(2, "世界观展开", nb.chapter2)
        self.state.updated_at = datetime.now().isoformat()

        return {"status": "ch2_done", "chapter": 2,
                "word_count": result["word_count"], "beats": result["beats"]}

    def _exec_write_ch3(self, inst: Instruction) -> dict:
        """第三章：首次核心冲突 + 展现实力（节拍级写作）"""
        config = self._new_book_config
        nb = self.state.new_book
        prev_end = nb.chapter2[-500:] if nb.chapter2 else ""

        ch3_outline = (
            f"第三章：首次核心冲突+展现实力\n"
            f"流派：{config.genre}/{config.sub_genre}\n"
            f"任务：主角面临第一个真正的对手→展现实力→冲突解决→获得认可→\n"
            f"      埋更大世界的伏笔→章末强钩子"
        )

        self.chapter_writer.executor.profile = self.profile
        result = self.chapter_writer.write_chapter(
            chapter_num=3, chapter_outline=ch3_outline,
            target_words=config.words_per_chapter or 3000,
            genre=config.genre, pen_name=config.pen_name,
            previous_chapter_ending=prev_end,
            previous_summary=f"前两章概要：{nb.chapter1[:200]}... → {nb.chapter2[:200]}...",
            assembler_plan=self.state.assembler_plan,
            stage_index=self._get_stage_index(3))

        nb.chapter3 = result["text"]
        self.cost_tracker.record("ch3_draft", "", result["text"])
        self._save_new_book_chapter(3, "首次核心冲突", nb.chapter3)
        self.state.updated_at = datetime.now().isoformat()

        return {"status": "ch3_done", "chapter": 3,
                "word_count": result["word_count"], "beats": result["beats"]}

    def _exec_generate_title(self, inst: Instruction) -> dict:
        """
        书名 + 简介生成（基于前三章内容）
        """
        config = self._new_book_config
        nb = self.state.new_book

        ch123 = nb.chapter1 + "\n\n" + nb.chapter2 + "\n\n" + nb.chapter3

        # 书名
        title_prompt = self.new_book_pipeline.build_title_prompt(config, ch123)
        from core.llm_client import extract_json
        raw_title = self.llm.call(
            "你是一位专业的网文编辑。请只返回JSON，不要加任何额外文字。",
            title_prompt, temperature=0.8, max_tokens=512)
        try:
            title_data = json.loads(extract_json(raw_title))
            nb.title_options = title_data.get("titles", [])
            nb.best_title = title_data.get("best", nb.title_options[0] if nb.title_options else config.title)
        except Exception:
            nb.title_options = [config.title] if config.title else ["未命名"]
            nb.best_title = nb.title_options[0]

        # 简介
        synopsis_prompt = self.new_book_pipeline.build_synopsis_prompt(config, ch123)
        raw_syn = self.llm.call(
            "你是一位专业的网文编辑。请只返回JSON，不要加任何额外文字。",
            synopsis_prompt, temperature=0.8, max_tokens=512)
        try:
            syn_data = json.loads(extract_json(raw_syn))
            nb.synopsis = syn_data.get("synopsis", "")
        except Exception:
            nb.synopsis = ""

        self.cost_tracker.record("title_synopsis", title_prompt + synopsis_prompt,
                                 raw_title + raw_syn)

        # 更新书籍信息
        self.state.title = nb.best_title
        nb.title_finalized = True
        self.state.updated_at = datetime.now().isoformat()

        return {
            "status": "title_generated",
            "best_title": nb.best_title,
            "options": nb.title_options,
            "synopsis": nb.synopsis[:100] + "..." if len(nb.synopsis) > 100 else nb.synopsis,
        }

    # ─── 新书辅助方法 ───

    def _generate_initial_characters(self, config: NewBookConfig) -> list[dict]:
        """AI 生成初始角色设定"""
        if not self.llm:
            return []

        prompt = (
            f"为一本{config.genre}/{config.sub_genre}类网络小说设计核心角色阵容。\n"
            f"平台：{config.platform}\n"
            f"总章节：约{config.chapter_count}章\n\n"
            "请设计：\n"
            "1. 主角（姓名、性格、背景、金手指类型、成长路线）\n"
            "2. 1-2个反派\n"
            "3. 3-5个主要配角（伙伴/导师/红颜/对手）\n"
            "4. 每个角色的核心冲突和弧线\n\n"
            "以JSON返回：{\"characters\": [{"
            "\"name\": \"\", \"identity\": \"\", \"personality\": \"\", "
            "\"background\": \"\", \"arc\": \"\", \"role\": \"protagonist/antagonist/supporting\""
            "}]}"
        )
        raw = self.llm.call(
            "你是一位专业的网络小说设定师。请只返回JSON。",
            prompt, temperature=0.7, max_tokens=4096)
        try:
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            chars = data.get("characters", [])
            # 注册到状态机
            for c in chars:
                self.char_states.register(
                    name=c.get("name", ""),
                    identity=c.get("identity", ""),
                    power_level=c.get("power_level", ""),
                )
            return chars
        except Exception:
            return []

    def _generate_foreshadows(self, config: NewBookConfig) -> list[dict]:
        """AI 规划伏笔方案"""
        if not self.llm:
            return []

        prompt = (
            f"为一本{config.genre}/{config.sub_genre}类网络小说规划伏笔方案。\n"
            f"总章节：约{config.chapter_count if config.chapter_count else 500}章\n\n"
            "请设计 5-8 条伏笔，覆盖不同层级：\n"
            "- 全局伏笔（贯穿全书的大谜团）\n"
            "- 弧级伏笔（每个卷/弧的关键线索）\n"
            "- 章级伏笔（单章内的悬念设置）\n\n"
            "每条伏笔标注：描述、建议埋设章节、预计回收章节、重要程度(major/minor/background)\n\n"
            "以JSON返回：{\"foreshadows\": [{"
            "\"description\": \"\", \"plant_chapter\": 0, \"resolve_chapter\": 0, "
            "\"importance\": \"major|minor|background\""
            "}]}"
        )
        raw = self.llm.call(
            "你是一位专业的小说策划编辑。请只返回JSON。",
            prompt, temperature=0.7, max_tokens=2048)
        try:
            from core.llm_client import extract_json
            data = json.loads(extract_json(raw))
            return data.get("foreshadows", [])
        except Exception:
            return []

    def _save_new_book_chapter(self, ch_num: int, title: str, content: str):
        """保存新书启动阶段的章节到临时路径"""
        save_dir = Path("books") / "new_book_temp"
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"chapter_{ch_num:04d}.json").write_text(
            json.dumps({
                "num": ch_num, "title": title, "content": content,
                "created_at": datetime.now().isoformat(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def finalize_new_book(self) -> BookConfig:
        """
        新书启动完成后，正式创建图书记录。

        将临时数据迁移到 books/ 目录，返回 BookConfig。
        """
        nb = self.state.new_book

        # 创建正式图书
        book = self.book_mgr.create(
            title=nb.best_title or self.state.title,
            pen_name=self.state.pen_name,
            genre=self.state.genre,
            sub_genre=self.state.sub_genre,
            platform=self.state.platform,
            chapter_count=self.state.total_chapters,
            structure_template_id=self.state.structure_template_id,
            style_profile_id=self._new_book_config.style_profile_id if self._new_book_config else "",
        )
        book.first_three_chapters = {
            "chapter1": nb.chapter1[:500] + "...",
            "chapter2": nb.chapter2[:500] + "...",
            "chapter3": nb.chapter3[:500] + "...",
        }
        book.synopsis_ = nb.synopsis  # 非 dataclass 字段暂存

        # 保存前三章
        self.book_mgr.save_chapter(book.book_id, 1, "钩子·金手指·首次危机", nb.chapter1)
        self.book_mgr.save_chapter(book.book_id, 2, "世界观展开", nb.chapter2)
        self.book_mgr.save_chapter(book.book_id, 3, "首次核心冲突", nb.chapter3)
        book.current_chapter = 3

        # 保存大纲
        self.book_mgr.save_outline(book.book_id, {
            "structure": self.state.structure_template_id,
            "chapters": self.state.chapters,
            "characters": nb.characters_created,
            "foreshadows": nb.foreshadows_planned,
            "synopsis": nb.synopsis,
        })

        # 保存组装计划
        if self.state.assembler_plan:
            from libraries.assembler import save_plan
            plan_path = Path("books") / book.book_id / "assembler_plan.json"
            save_plan(self.state.assembler_plan, str(plan_path))

        # 保存角色状态
        self.char_states.save(str(Path("books") / book.book_id / "character_states.json"))

        # 保存成本
        cost_path = Path("books") / book.book_id / "cost.json"
        self.cost_tracker.save(str(cost_path))

        self.book_mgr.update(book)
        self.state.book_id = book.book_id
        self.book = book

        return book

    # ═══════════════════════════════════════════
    # ♻️ 续写执行器
    # ═══════════════════════════════════════════

    def _exec_plan_outline(self, inst: Instruction) -> dict:
        """生成大纲（续写模式下首次使用或重置大纲）"""
        if not self.llm:
            raise RuntimeError("LLM 未配置")

        struct = self.struct_lib.search(
            genre=self.state.genre,
            sub_genre=self.state.sub_genre,
            chapter_count=self.state.total_chapters)
        template = struct[0] if struct else None

        if template:
            self.state.structure_template_id = template.id
            self.state.outline_data = {
                "structure": template.id,
                "stages": [s.__dict__ for s in template.stages],
                "total_chapters": template.total_chapters,
            }
            ch_num = 1
            self.state.chapters = []
            for stage in template.stages:
                for _ in range(stage.min_chapters):
                    self.state.chapters.append({
                        "num": ch_num,
                        "title": f"{stage.name} ({ch_num})",
                        "stage": stage.name,
                        "outline": "",
                    })
                    ch_num += 1
            self.state.total_chapters = len(self.state.chapters)

        # 生成组装计划（桥段/笑点/内涵匹配）
        if not self.state.assembler_plan:
            self.state.assembler_plan = self.assembler.assemble_book(
                genre=self.state.genre,
                sub_genre=self.state.sub_genre,
            )

        self.state.phase = Phase.WRITING
        return {
            "status": "outline_planned",
            "structure": self.state.structure_template_id,
            "chapters": len(self.state.chapters),
            "book_title": self.state.assembler_plan.book_title if self.state.assembler_plan else "",
        }

    def _exec_write_chapter(self, inst: Instruction) -> dict:
        """写一章（续写模式 — 节拍级写作）"""
        chapter_num = inst.chapter_num
        self.state.current_chapter = chapter_num

        # 获取本章大纲
        ch_data = {}
        if 0 < chapter_num <= len(self.state.chapters):
            ch_data = self.state.chapters[chapter_num - 1]
        chapter_outline = ch_data.get("outline", f"第{chapter_num}章")

        # 获取上文结尾
        prev_ending = ""
        if chapter_num > 1 and self.book:
            prev_ch = self.book_mgr.load_chapter(self.state.book_id, chapter_num - 1)
            if prev_ch:
                prev_ending = prev_ch.get("content", "")[-500:]

        # 角色状态
        char_states = self.char_states.build_context_prompt(chapter_num=chapter_num)

        # 使用节拍级写作管线
        target_words = self.book.words_per_chapter if self.book else 3000
        self.chapter_writer.executor.profile = self.profile  # 更新 profile

        result = self.chapter_writer.write_chapter(
            chapter_num=chapter_num,
            chapter_outline=chapter_outline,
            target_words=target_words,
            genre=self.state.genre,
            pen_name=self.state.pen_name,
            previous_chapter_ending=prev_ending,
            character_states=char_states,
            assembler_plan=self.state.assembler_plan,
            stage_index=self._get_stage_index(chapter_num),
        )

        full_text = result["text"]
        self.state.current_content = full_text
        self.state.current_plot_id = "beat_writer"
        self.state.phase = Phase.REVIEWING

        # 更新角色状态
        self.char_states.update_from_chapter(chapter_num, full_text)

        self._save_continue_state()

        # 保存章节
        if self.book:
            self.book_mgr.save_chapter(
                self.state.book_id, chapter_num,
                ch_data.get("title", f"第{chapter_num}章"),
                full_text)

        # 记录成本
        self.cost_tracker.record(f"ch{chapter_num}_beat", "", full_text)

        return {
            "status": "chapter_written",
            "chapter": chapter_num,
            "word_count": result["word_count"],
            "plot_used": "beat_writer",
            "beats": result["beats"],
            "beat_details": result.get("beat_details", []),
            "cost": round(self.cost_tracker.spent, 4),
        }

    def _exec_write_timeline_chapter(self, inst: Instruction) -> dict:
        """按蓝图（时间线）写一章 — 新核心"""
        chapter_num = inst.chapter_num
        self.state.current_chapter = chapter_num

        if not self.timeline_writer:
            return {"error": "蓝图写作器未初始化，请先调用 start_new_book_timeline()"}

        # 获取上文结尾
        prev_ending = ""
        if chapter_num > 1:
            try:
                prev_ch = self.book_mgr.load_chapter(
                    self.state.book_id, chapter_num - 1)
                if prev_ch:
                    prev_ending = prev_ch.get("content", "")[-500:]
            except Exception:
                pass

        # 角色状态
        char_states = ""
        try:
            char_states = self.char_states.build_context_prompt(chapter_num=chapter_num)
        except Exception:
            pass

        result = self.timeline_writer.write_chapter(
            chapter_num=chapter_num,
            previous_chapter_ending=prev_ending,
            character_states=char_states,
        )

        full_text = result["text"]
        self.state.current_content = full_text
        self.state.phase = Phase.REVIEWING

        # 更新角色状态
        try:
            self.char_states.update_from_chapter(chapter_num, full_text)
        except Exception:
            pass

        self._save_continue_state()

        # 保存章节
        if self.book:
            try:
                self.book_mgr.save_chapter(
                    self.state.book_id, chapter_num,
                    f"第{chapter_num}章", full_text)
            except Exception:
                pass

        # 记录成本
        self.cost_tracker.record(f"ch{chapter_num}_timeline", "", full_text)

        return {
            "status": "chapter_written",
            "chapter": chapter_num,
            "word_count": result["word_count"],
            "plot_used": "timeline_writer",
            "beats": result["beats"],
            "beat_details": result.get("beat_details", []),
            "blueprint": result.get("blueprint", {}),
            "cost": round(self.cost_tracker.spent, 4),
        }

    def _exec_review_current(self, inst: Instruction) -> dict:
        """审查当前章"""
        result = self.reviewer.review(
            self.state.current_content,
            self.state.current_chapter,
            target_words=self.book.words_per_chapter if self.book else 3000,
        )

        if result.passed:
            self.state.phase = Phase.DE_AI
            return {
                "status": "review_passed",
                "score": result.score,
                "issues": len(result.issues),
            }
        else:
            return {
                "status": "review_failed",
                "score": result.score,
                "issues": [i.description for i in result.issues],
            }

    def _exec_de_ai_current(self, inst: Instruction) -> dict:
        """去 AI 味处理"""
        result = self.de_ai.process_rule_based(self.state.current_content)
        self.state.current_content = result.processed
        self.state.phase = Phase.IDLE

        # 更新进度
        if self.book:
            self.book.current_chapter = self.state.current_chapter
            self.book_mgr.update(self.book)

        self._save_continue_state()
        return {
            "status": "de_ai_done",
            "word_replacements": result.word_replacements,
            "processed_chars": len(result.processed),
        }

    def _exec_confirm_current(self, inst: Instruction) -> dict:
        """确认当前章"""
        self.state.current_content = ""
        self._save_continue_state()
        return {"status": "confirmed", "chapter": self.state.current_chapter}

    def _save_continue_state(self):
        """保存续写状态"""
        if not self.state.book_id:
            return
        book_dir = Path("books") / self.state.book_id

        # 角色状态
        self.char_states.save(str(book_dir / "character_states.json"))

        # 成本
        self.cost_tracker.save(str(book_dir / "cost.json"))

    # ═══════════════════════════════════════════
    # 自动运行
    # ═══════════════════════════════════════════

    def step(self) -> dict:
        """执行一步（route → execute）"""
        inst = self.route()
        return {"instruction": inst.op.value, "reason": inst.reason,
                **self.execute(inst)}

    def run(self, max_steps: int = 10) -> list[dict]:
        """
        自动跑最多 max_steps 步。

        对于新书模式：会跑完规划→前三章→书名
        对于续写模式：会跑写→审→去AI 循环
        """
        results = []
        for _ in range(max_steps):
            inst = self.route()
            if inst.op in (Op.COMPLETE, Op.PAUSE):
                results.append({
                    "status": inst.op.value,
                    "reason": inst.reason,
                })
                break
            result = self.execute(inst)
            results.append(result)

            # 审查失败 → 暂停等人工干预
            if result.get("status") == "review_failed":
                break

        return results

    def run_full_cycle(self) -> dict:
        """
        跑完一章的完整周期：写 → 审 → 去AI
        （仅适用于续写模式）
        """
        if self.state.book_mode != BookMode.CONTINUE:
            return {"status": "error", "reason": "run_full_cycle 仅适用于续写模式"}

        inst = self.route()

        if inst.op == Op.WRITE_CHAPTER:
            write_result = self.execute(inst)

            review_inst = self.route()
            review_result = self.execute(review_inst)

            if review_result.get("status") == "review_passed":
                deai_inst = Instruction(Op.DE_AI_PASS, self.state.current_chapter)
                deai_result = self.execute(deai_inst)
                return {
                    "write": write_result,
                    "review": review_result,
                    "de_ai": deai_result,
                }
            else:
                return {"write": write_result, "review": review_result}

        return {"status": inst.op.value, "reason": inst.reason}

    def run_new_book_full(self) -> dict:
        """
        一键跑完新书启动全流程：
        规划 → 第一章 → 第二章 → 第三章 → 书名简介

        返回完整结果，包含所有中间产物。
        """
        if self.state.book_mode != BookMode.NEW:
            return {"status": "error", "reason": "当前不是新书模式"}

        results = []
        for _ in range(10):
            inst = self.route()
            if inst.op == Op.WRITE_CHAPTER:
                # 新书启动完成，转入续写模式
                results.append({"status": "new_book_startup_complete"})
                break
            result = self.execute(inst)
            results.append(result)

        return {
            "status": "new_book_complete",
            "steps": len(results),
            "results": results,
            "summary": {
                "title": self.state.title,
                "chapters": [1, 2, 3],
                "characters": len(self.state.new_book.characters_created),
                "foreshadows": len(self.state.new_book.foreshadows_planned),
                "title_options": self.state.new_book.title_options,
            },
        }
