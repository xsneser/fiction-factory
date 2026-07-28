"""
四大核心库 — 网文工厂的内容资产系统
"""

from .plot import PlotLibrary, PlotTemplate, PlotSlot
from .structure import StructureLibrary, StructureTemplate
from .gag import GagLibrary, GagPattern
from .theme import ThemeLibrary, ThemeEntry
from .profiles import PenNameProfile, ProfileManager, PRESET_PROFILES
from .book_manager import BookConfig, BookManager
from .new_book import NewBookPipeline, NewBookConfig, recommend_opening
