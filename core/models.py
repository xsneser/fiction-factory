"""
数据模型 — 对齐 show-me-the-story state.go / config.go / settings.go
"""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ─── 章节 ───

class ChapterStatus(str, Enum):
    PENDING = "pending"
    WRITING = "writing"
    REVIEW = "review"
    ACCEPTED = "accepted"


@dataclass
class ChapterState:
    num: int
    title: str
    outline: str = ""
    content: str = ""
    summary: str = ""
    status: ChapterStatus = ChapterStatus.PENDING
    word_count: int = 0


# ─── 伏笔 ───

class ForeshadowStatus(str, Enum):
    PLANTED = "planted"
    PROGRESSING = "progressing"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


@dataclass
class ForeshadowEvent:
    chapter: int
    note: str


@dataclass
class Foreshadow:
    id: int
    name: str = ""
    description: str = ""
    plant_chapter: int = 0
    target_chapter: int = 0
    status: ForeshadowStatus = ForeshadowStatus.PLANTED
    events: list[ForeshadowEvent] = field(default_factory=list)
    resolution: str = ""


# ─── 伏笔大纲冲突 ───

@dataclass
class ForeshadowOutlineConflict:
    foreshadow_id: int
    foreshadow_name: str = ""
    conflict_type: str = ""
    description: str = ""
    suggested_fix: str = ""


@dataclass
class ForeshadowOutlineReport:
    has_conflicts: bool = False
    conflicts: list[ForeshadowOutlineConflict] = field(default_factory=list)
    summary: str = ""


# ─── 写作冲突 ───

@dataclass
class ConflictActionOption:
    id: str
    label: str
    description: str = ""


@dataclass
class WritingConflict:
    chapter_index: int = 0
    chapter_num: int = 0
    chapter_title: str = ""
    issues: list[str] = field(default_factory=list)
    summary: str = ""
    root_cause: str = ""
    reconcilable: bool = False
    extra_constraints: str = ""  # 可注入写作 prompt 的补充约束（reconcilable 为 true 时由 LLM 给出）
    suggested_actions: list[ConflictActionOption] = field(default_factory=list)


# ─── 叙事记忆 ───

@dataclass
class MemoryEntry:
    id: int
    content: str = ""
    category: str = ""  # character|location|item|event|promise|other
    chapter: int = 0
    position: int = 0


# ─── 卷（Arc） ───

@dataclass
class Arc:
    id: int
    title: str = ""
    goal: str = ""
    start_ch: int = 0
    end_ch: int = 0
    summary: str = ""


# ─── 大纲角色检查 ───

@dataclass
class OutlineCharacterSuggestion:
    name: str = ""
    chapter_num: int = 0
    description: str = ""
    role: str = ""


@dataclass
class OutlineCharacterReport:
    has_suggestions: bool = False
    suggestions: list[OutlineCharacterSuggestion] = field(default_factory=list)
    summary: str = ""


# ─── 项目进度 ───

@dataclass
class Progress:
    phase: str = "outline"
    title: str = ""
    core_prompt: str = ""
    story_synopsis: str = ""
    chapters: list[ChapterState] = field(default_factory=list)
    arcs: list[Arc] = field(default_factory=list)
    current_chapter_index: int = 0
    foreshadows: list[Foreshadow] = field(default_factory=list)
    last_foreshadow_outline_report: Optional[ForeshadowOutlineReport] = None
    last_outline_character_report: Optional[OutlineCharacterReport] = None
    pending_writing_conflict: Optional[WritingConflict] = None
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    memory_max_tokens: int = 0

    def to_dict(self) -> dict:
        """序列化（不含正文内容）"""
        return {
            "phase": self.phase,
            "title": self.title,
            "core_prompt": self.core_prompt,
            "story_synopsis": self.story_synopsis,
            "chapters": [
                {
                    "num": c.num, "title": c.title, "outline": c.outline,
                    "summary": c.summary, "status": c.status.value,
                    "word_count": c.word_count
                }
                for c in self.chapters
            ],
            "current_chapter_index": self.current_chapter_index,
        }


# ─── 角色 / 世界观 / 组织 ───

@dataclass
class Character:
    id: str = ""
    name: str = ""
    age: str = ""
    appearance: str = ""
    personality: str = ""
    background: str = ""
    motivation: str = ""
    abilities: str = ""
    notes: str = ""
    relationships: list[dict] = field(default_factory=list)


@dataclass
class WorldviewEntry:
    id: str = ""
    name: str = ""
    category: str = ""
    description: str = ""
    tags: str = ""


@dataclass
class Organization:
    id: str = ""
    name: str = ""
    type: str = ""
    description: str = ""
    members: list[str] = field(default_factory=list)


@dataclass
class ProjectSettings:
    characters: list[Character] = field(default_factory=list)
    worldview: list[WorldviewEntry] = field(default_factory=list)
    organizations: list[Organization] = field(default_factory=list)

    def next_character_id(self) -> str:
        nums = [int(c.id) for c in self.characters if c.id.isdigit()]
        return str(max(nums) + 1 if nums else 1)

    def next_worldview_id(self) -> str:
        nums = [int(w.id) for w in self.worldview if w.id.isdigit()]
        return str(max(nums) + 1 if nums else 1)

    def next_org_id(self) -> str:
        nums = [int(o.id) for o in self.organizations if o.id.isdigit()]
        return str(max(nums) + 1 if nums else 1)


# ─── Skill ───

@dataclass
class Skill:
    id: str = ""
    name: str = ""
    description: str = ""
    content: str = ""
    enabled: bool = False
    skill_type: str = ""  # writing|polish
    builtin: bool = False
    lang: str = ""
    category: str = ""
    source: str = ""


# ─── API 配置 ───

@dataclass
class APIConfig:
    api_key: str = ""
    base_url: str = ""
    url_strict: bool = False
    model: str = ""
    max_tokens: int = 0
    http_timeout_seconds: int = 300
    context_budget_tokens: int = 300000
    verify_ssl: bool = True  # 是否校验 TLS 证书（默认开启，关闭仅用于兼容旧证书环境）


# ─── 全书优化 ───

@dataclass
class BookDiagnosisItem:
    chapter_num: int = 0
    type: str = ""  # logic|transition|style|rhythm|dialogue|polish
    priority: str = ""  # P0|P1|P2
    feedback: str = ""
    selected: bool = True


@dataclass
class BookDiagnosis:
    items: list[BookDiagnosisItem] = field(default_factory=list)

    def clear(self):
        self.items.clear()


@dataclass
class PostProcessExecuteOptions:
    run_smooth_transitions_first: bool = True


@dataclass
class PostProcessState:
    diagnosis_report: str = ""
    consistency_report: str = ""
    roadmap: BookDiagnosis = field(default_factory=BookDiagnosis)
    execute_options: PostProcessExecuteOptions = field(
        default_factory=PostProcessExecuteOptions
    )
