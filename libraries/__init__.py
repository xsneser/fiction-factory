"""
四大核心库 + 引擎 — 网文工厂的内容资产与调度系统
"""

from .plot import PlotLibrary, PlotTemplate, PlotSlot
from .structure import StructureLibrary, StructureTemplate
from .gag import GagLibrary, GagPattern
from .theme import ThemeLibrary, ThemeEntry
from .profiles import PenNameProfile, ProfileManager, PRESET_PROFILES
from .book_manager import BookConfig, BookManager
from .new_book import NewBookPipeline, NewBookConfig, recommend_opening
from .cost_tracker import CostTracker, CostRecord, estimate_cost
from .de_ai import DeAIEngine, DeAIResult
from .character_state import CharacterStateMachine, CharacterState
from .reviewer import ContentReviewer, ReviewResult, ReviewIssue
from .engine import NovelEngine, EngineState, Instruction, Op
