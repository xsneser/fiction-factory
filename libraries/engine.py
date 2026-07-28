"""
引擎路由器（Engine Router）
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
from libraries.writing_pipeline import WritingPipeline, WritingContext, WritingResult
from libraries.character_state import CharacterStateMachine, CharacterState
from libraries.reviewer import ContentReviewer, ReviewResult
from libraries.new_book import NewBookPipeline, NewBookConfig, Chapter3Result


class Op(Enum):
    """操作指令"""
    COMPLETE = "complete"              # 全书完成
    PLAN_OUTLINE = "plan_outline"      # 生成大纲
    PLAN_CHAPTER = "plan_chapter"      # 规划本章（匹配模板+变量）
    WRITE_CHAPTER = "write_chapter"    # 写正文
    REVIEW_CHAPTER = "review_chapter"  # 审查
    DE_AI_PASS = "de_ai_pass"         # 去AI味
    CONFIRM_CHAPTER = "confirm"        # 确认发布
    PAUSE = "pause"                    # 暂停（预算不足等）


@dataclass
class Instruction:
    """路由指令"""
    op: Op
    chapter_num: int = 0
    chapter_title: str = ""
    reason: str = ""


@dataclass
class EngineState:
    """引擎全局状态"""
    book_id: str = ""
    phase: str = "idle"                # idle/outline/writing/reviewing/complete

    # 书目信息
    title: str = ""
    pen_name: str = ""
    genre: str = ""
    sub_genre: str = ""

    # 大纲
    outline_data: dict = field(default_factory=dict)
    structure_template_id: str = ""
    chapters: list[dict] = field(default_factory=list)

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


class NovelEngine:
    """
    小说工厂总引擎

    用法：
        engine = NovelEngine(llm_client)
        engine.load_book("book_001")
        engine.run()              # 自动跑一整个 cycle
        或
        step = engine.route()     # 查看下一步
        engine.execute(step)      # 执行
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
        self.new_book = NewBookPipeline(llm_client)
        self.writing = WritingPipeline(llm_client, self.profiles)
        self.char_states = CharacterStateMachine()
        self.reviewer = ContentReviewer(llm_client)
        self.de_ai = DeAIEngine(llm_client)

        # 状态
        self.state = EngineState()
        self.cost_tracker = CostTracker()
        self.book: Optional[BookConfig] = None
        self.profile: Optional[PenNameProfile] = None

    # ─── 书生命周期 ───

    def load_book(self, book_id: str):
        """加载已有图书"""
        self.book = self.book_mgr.get(book_id)
        if not self.book:
            raise ValueError(f"图书 {book_id} 不存在")

        self.state.book_id = book_id
        self.state.title = self.book.title
        self.state.pen_name = self.book.pen_name
        self.state.genre = self.book.genre
        self.state.sub_genre = self.book.sub_genre
        self.state.current_chapter = self.book.current_chapter
        self.state.total_chapters = self.book.chapter_count

        # 加载风格档案
        if self.book.style_profile_id:
            self.profile = self.profiles.get(self.book.style_profile_id)
        else:
            self.profile = self.profiles.get_by_name(self.book.pen_name)

        # 加载角色状态
        char_path = Path("books") / book_id / "character_states.json"
        if char_path.exists():
            self.char_states.load(str(char_path))

        # 加载成本
        cost_path = Path("books") / book_id / "cost.json"
        self.cost_tracker = CostTracker.load(str(cost_path))
        self.cost_tracker.book_id = book_id

        # 加载大纲
        outline = self.book_mgr.get_outline(book_id)
        if outline:
            self.state.outline_data = outline

        return self.state

    def _save_state(self):
        """保存所有状态"""
        if not self.state.book_id:
            return
        book_dir = Path("books") / self.state.book_id

        # 角色状态
        self.char_states.save(str(book_dir / "character_states.json"))

        # 成本
        self.cost_tracker.save(str(book_dir / "cost.json"))

    def plan_outline(self):
        """生成大纲"""
        if not self.llm:
            raise RuntimeError("LLM 未配置")

        # 选大纲模板
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
            # 从 stages 生成章节列表
            ch_num = 1
            self.state.chapters = []
            for stage in template.stages:
                for _ in range(stage.min_chapters):
                    self.state.chapters.append({
                        "num": ch_num, "title": f"{stage.name} ({ch_num})",
                        "stage": stage.name, "outline": "",
                    })
                    ch_num += 1

        self.state.phase = "outline"

    # ─── 路由（纯函数） ───

    def route(self) -> Instruction:
        """纯函数路由：决定下一步做什么"""
        s = self.state

        # 1. 完本
        if s.current_chapter >= s.total_chapters and s.current_content:
            return Instruction(Op.COMPLETE, reason="全书完成")

        # 2. 还没有大纲
        if not s.outline_data and not s.chapters:
            return Instruction(Op.PLAN_OUTLINE, reason="需要生成大纲")

        # 3. 当前章需要审查
        if s.current_content and s.phase == "reviewing":
            return Instruction(Op.REVIEW_CHAPTER, s.current_chapter,
                               reason="审查本章")

        # 4. 有内容且审查通过 → 去AI味
        if s.current_content and s.phase == "de_ai":
            return Instruction(Op.DE_AI_PASS, s.current_chapter,
                               reason="去AI味处理")

        # 5. 预算检查
        if self.cost_tracker.remaining() <= 0:
            return Instruction(Op.PAUSE, reason=f"预算耗尽 ({self.cost_tracker.spent:.2f}/{self.cost_tracker.budget})")

        # 6. 写下一章
        return Instruction(Op.WRITE_CHAPTER, s.current_chapter + 1,
                           reason=f"写第 {s.current_chapter + 1} 章")

    # ─── 执行 ───

    def execute(self, inst: Instruction) -> dict:
        """执行路由指令"""
        if inst.op == Op.COMPLETE:
            return {"status": "complete", "message": "全书完成"}

        elif inst.op == Op.PAUSE:
            return {"status": "paused", "reason": inst.reason}

        elif inst.op == Op.PLAN_OUTLINE:
            self.plan_outline()
            return {"status": "outline_planned",
                    "structure": self.state.structure_template_id,
                    "chapters": len(self.state.chapters)}

        elif inst.op == Op.WRITE_CHAPTER:
            return self._write_chapter(inst.chapter_num)

        elif inst.op == Op.REVIEW_CHAPTER:
            return self._review_current()

        elif inst.op == Op.DE_AI_PASS:
            return self._de_ai_current()

        elif inst.op == Op.CONFIRM_CHAPTER:
            return self._confirm_current()

        return {"status": "unknown"}

    def _write_chapter(self, chapter_num: int) -> dict:
        """写一章"""
        self.state.current_chapter = chapter_num

        # 匹配桥段模板
        ch_data = {}
        if chapter_num <= len(self.state.chapters):
            ch_data = self.state.chapters[chapter_num - 1]

        ctx = WritingContext(
            chapter_num=chapter_num,
            chapter_title=ch_data.get("title", f"第{chapter_num}章"),
            chapter_outline=ch_data.get("outline", ""),
            style_profile=self.profile,
            target_words=self.book.words_per_chapter if self.book else 3000,
            de_ai=True,
        )

        # 匹配模板
        ctx = self.writing.match_templates(
            f"{self.state.genre} {ch_data.get('outline', '')}",
            self.state.genre, ch_data.get("outline", ""),
            existing_ctx=ctx)

        # 注入角色状态
        ctx.character_states = self.char_states.build_context_prompt(
            chapter_num=chapter_num)

        # 生成
        result = self.writing.generate(ctx, self.cost_tracker)

        self.state.current_content = result.content
        self.state.current_plot_id = ctx.plot_template.id if ctx.plot_template else ""
        self.state.phase = "reviewing"

        # 更新角色状态
        self.char_states.update_from_chapter(chapter_num, result.content)

        self._save_state()

        # 保存章节
        if self.book:
            self.book_mgr.save_chapter(
                self.state.book_id, chapter_num,
                ch_data.get("title", f"第{chapter_num}章"),
                result.content
            )

        return {
            "status": "written",
            "chapter": chapter_num,
            "word_count": result.word_count,
            "plot_used": ctx.plot_template.name if ctx.plot_template else "none",
            "gags": result.applied_gags,
            "themes": result.applied_themes,
            "cost": round(self.cost_tracker.spent, 4),
        }

    def _review_current(self) -> dict:
        """审查当前章"""
        result = self.reviewer.review(
            self.state.current_content,
            self.state.current_chapter,
            target_words=self.book.words_per_chapter if self.book else 3000,
        )

        if result.passed:
            self.state.phase = "de_ai"
            self.state.current_chapter += 1
            return {"status": "review_passed", "score": result.score,
                    "issues": len(result.issues)}
        else:
            return {"status": "review_failed", "score": result.score,
                    "issues": [i.description for i in result.issues]}

    def _de_ai_current(self) -> dict:
        """去 AI 味处理"""
        result = self.de_ai.process_rule_based(self.state.current_content)
        self.state.current_content = result.processed
        self.state.phase = "complete"

        # 更新书籍进度
        if self.book:
            self.book.current_chapter = self.state.current_chapter + 1
            self.book_mgr.update(self.book)

        self._save_state()
        return {
            "status": "de_ai_done",
            "word_replacements": result.word_replacements,
            "processed_chars": len(result.processed),
        }

    def _confirm_current(self) -> dict:
        """确认当前章"""
        self.state.phase = "idle"
        self.state.current_content = ""
        self._save_state()
        return {"status": "confirmed", "chapter": self.state.current_chapter}

    # ─── 自动运行 ───

    def run(self, max_chapters: int = 1) -> list[dict]:
        """自动跑 max_chapters 章"""
        results = []
        for _ in range(max_chapters):
            inst = self.route()
            if inst.op in (Op.COMPLETE, Op.PAUSE):
                results.append({"status": inst.op.value, "reason": inst.reason})
                break
            result = self.execute(inst)
            results.append(result)
            if result.get("status") == "review_failed":
                break  # 审查失败需要人工干预
        return results

    def run_full_cycle(self) -> dict:
        """跑完一章的完整周期：写→审→去AI"""
        inst = self.route()

        # 先写
        if inst.op == Op.WRITE_CHAPTER:
            write_result = self.execute(inst)

            # 再审
            review_inst = self.route()
            review_result = self.execute(review_inst)

            # 再去AI
            if review_result.get("status") == "review_passed":
                deai_inst = Instruction(Op.DE_AI_PASS, self.state.current_chapter)
                deai_result = self.execute(deai_inst)
                return {"write": write_result, "review": review_result, "de_ai": deai_result}
            else:
                return {"write": write_result, "review": review_result}

        return {"status": inst.op.value, "reason": inst.reason}
