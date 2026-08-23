"""
图书管理器
管理每本书的完整生命周期：创建→大纲→章节→状态
"""
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
import logging

from core.json_store import read_json, write_json_atomic
from core.safe_paths import ensure_child_path, is_safe_book_id

logger = logging.getLogger("novel-engine.book_manager")


@dataclass
class BookConfig:
    """单书配置"""
    book_id: str = ""
    title: str = ""                        # 书名
    pen_name: str = ""                     # 笔名
    platform: str = ""                     # 目标平台：fanqie/qidian/...
    chapter_count: int = 500
    current_chapter: int = 0
    words_per_chapter: int = 3000
    total_words: int = 0
    status: str = "planning"              # planning/ready/writing/reviewing/finished/published/paused
    structure_template_id: str = ""        # 使用的大纲模板ID
    assigned_profiles: list[str] = field(default_factory=list)  # 使用的桥段列表
    assigned_gags: list[str] = field(default_factory=list)      # 使用的笑点列表
    assigned_themes: list[str] = field(default_factory=list)    # 使用的内涵主题
    opening_template_id: str = ""          # 开篇模板ID
    # 前三章特殊配置
    first_three_chapters: dict = field(default_factory=dict)
    style_profile_id: str = ""             # 笔名风格档案ID
    budget: float = 50.0                   # API 花费预算
    detector_frequency: int = 1            # 灵机一动探测频率（每 N 短句组探测一次）
    current_cost: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    # 上架生命周期（由 libraries/publisher.py 写入；from_dict 按字段过滤，旧 book.json 自动兼容）
    finished_at: str = ""                  # 完本时间
    published_at: str = ""                 # 上架时间
    exported_at: str = ""                  # 最近导出时间
    publish_note: str = ""                 # 可选投稿备注

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: dict) -> "BookConfig":
        fields = [f.name for f in BookConfig.__dataclass_fields__.values()]
        return BookConfig(**{k: v for k, v in d.items() if k in fields})


class BookManager:
    """图书管理器"""

    def __init__(self, books_dir: str | None = None):
        # 锚定到项目根目录，避免依赖当前工作目录（从任何目录启动都找得到数据）
        base = Path(__file__).resolve().parent.parent
        self.dir = Path(books_dir) if books_dir else base / "books"
        if not self.dir.is_absolute():
            self.dir = base / self.dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, BookConfig] = {}
        self._scan_sig = None  # (book 数量, book.json mtime 之和) → 磁盘是否变化
        self._load_all()

    def _load_all(self):
        for d in self.dir.iterdir():
            if d.is_dir():
                cfg_path = d / "book.json"
                if cfg_path.exists():
                    try:
                        cfg = BookConfig.from_dict(read_json(cfg_path, {}))
                        self._cache[cfg.book_id] = cfg
                    except Exception as e:
                        # 单本书损坏不拖垮整个书库（否则缓存为空，create 会撞号覆盖）
                        logger.warning("跳过无法解析的图书配置 %s: %s", cfg_path, e)

    def list_all(self) -> list[BookConfig]:
        # mtime 缓存：只在 book.json 有变化时重扫磁盘（避免每次导航 3N 次磁盘读）
        count = 0
        sig_sum = 0.0
        for d in self.dir.iterdir():
            if not d.is_dir():
                continue
            cfg_path = d / "book.json"
            try:
                sig_sum += cfg_path.stat().st_mtime
                count += 1
            except OSError:
                pass
        sig = (count, sig_sum)
        if sig == self._scan_sig and self._cache:
            return list(self._cache.values())
        self._cache = {}
        self._load_all()
        self._scan_sig = sig
        return list(self._cache.values())

    def get(self, book_id: str) -> BookConfig | None:
        if book_id not in self._cache:
            # 缓存未命中时重新扫描磁盘
            self._cache = {}
            self._load_all()
        return self._cache.get(book_id)

    def _next_book_id(self) -> str:
        """基于磁盘现有 book_* 目录取下一个可用 id。

        不依赖 self._cache（缓存可能为空/过期），否则 create 会撞上已存在的
        book_001 并覆盖其配置，导致整本书数据丢失。
        """
        existing = set()
        if self.dir.is_dir():
            for d in self.dir.iterdir():
                if d.is_dir() and d.name.startswith("book_"):
                    existing.add(d.name)
        n = 1
        while f"book_{n:03d}" in existing:
            n += 1
        return f"book_{n:03d}"

    def create(self, title: str, pen_name: str, genre: str = "",
               sub_genre: str = "", platform: str = "fanqie",
               chapter_count: int = 500,
               structure_template_id: str = "",
               style_profile_id: str = "") -> BookConfig:
        book_id = self._next_book_id()
        cfg = BookConfig(
            book_id=book_id, title=title, pen_name=pen_name,
            platform=platform,
            chapter_count=chapter_count,
            structure_template_id=structure_template_id,
            style_profile_id=style_profile_id,
            created_at=datetime.now().isoformat(),
        )
        # 创建目录
        book_dir = self.dir / book_id
        book_dir.mkdir(exist_ok=True)
        (book_dir / "chapters").mkdir(exist_ok=True)
        (book_dir / "outline").mkdir(exist_ok=True)
        # 写配置
        write_json_atomic(book_dir / "book.json", cfg.to_dict())
        self._cache[book_id] = cfg
        return cfg

    def update(self, cfg: BookConfig):
        cfg.updated_at = datetime.now().isoformat()
        book_dir = self.dir / cfg.book_id
        write_json_atomic(book_dir / "book.json", cfg.to_dict())
        self._cache[cfg.book_id] = cfg

    def save_chapter(self, book_id: str, chapter_num: int,
                     title: str, content: str, summary: str = "",
                     review: dict | None = None,
                     bridges: list | None = None):
        """保存章节（review：规则审查结果 dict，随章节落盘供详情页展示；
        bridges：本桥段逐段去AI味后的 [{plot_id, plot_name, text}]，供写作台
        点击桥段→高亮对应正文；旧文件无此键，向前兼容。
        content 为派生缓存，落盘时保证 == "\n\n".join(bridges[].text)）"""
        book_dir = self.dir / book_id / "chapters"
        book_dir.mkdir(parents=True, exist_ok=True)
        chapter_file = book_dir / f"{chapter_num:04d}.json"
        write_json_atomic(chapter_file, {
            "num": chapter_num, "title": title,
            "content": content, "summary": summary,
            "review": review,
            "bridges": bridges,
            "created_at": datetime.now().isoformat(),
        })

    def load_chapter(self, book_id: str, chapter_num: int) -> dict | None:
        chapter_file = self.dir / book_id / "chapters" / f"{chapter_num:04d}.json"
        if chapter_file.exists():
            return read_json(chapter_file)
        return None

    def update_chapter_summary(self, book_id: str, chapter_num: int, summary: str):
        """补写/更新章节摘要（原子回写，供语义摘要管线用）。"""
        chapter_file = self.dir / book_id / "chapters" / f"{chapter_num:04d}.json"
        if not chapter_file.exists():
            return
        data = read_json(chapter_file)
        if not isinstance(data, dict):
            return
        data["summary"] = summary
        write_json_atomic(chapter_file, data)

    def load_chapter_summaries(self, book_id: str, before_chapter: int,
                               limit: int = 5) -> list[dict]:
        """返回最近的 [{num, summary}]，仅含 summary 非空、num < before_chapter 的章节，按 num 降序。

        作为跨章长程记忆注入后续写作 prompt。
        """
        chapters_dir = self.dir / book_id / "chapters"
        if not chapters_dir.exists():
            return []
        results = []
        for f in chapters_dir.glob("[0-9]*.json"):
            try:
                num = int(f.stem)
            except ValueError:
                continue
            if num >= before_chapter:
                continue
            data = read_json(f)
            if isinstance(data, dict) and data.get("summary"):
                results.append({"num": num, "summary": data["summary"]})
        results.sort(key=lambda x: x["num"], reverse=True)
        return results[:limit]

    def save_outline(self, book_id: str, outline_data: dict):
        """保存大纲"""
        outline_dir = self.dir / book_id / "outline"
        outline_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(outline_dir / "outline.json", outline_data)

    def get_outline(self, book_id: str) -> dict | None:
        """加载大纲"""
        outline_path = self.dir / book_id / "outline" / "outline.json"
        if outline_path.exists():
            return read_json(outline_path)
        return None

    def save_storyline(self, book_id: str, storyline) -> None:
        """把 BookStoryline 持久化到 books/{book_id}/storyline.json。
        storyline.save_storyline 内部会调用 to_dict()，这里直接透传对象。
        函数内 import，避免与 storyline.py 产生循环依赖。"""
        from libraries.storyline import save_storyline as _save
        _save(storyline, str(self.dir / book_id / "storyline.json"))

    def load_storyline(self, book_id: str):
        """加载 books/{book_id}/storyline.json，返回 BookStoryline 或 None。"""
        from libraries.storyline import load_storyline as _load
        path = self.dir / book_id / "storyline.json"
        if path.exists():
            return _load(str(path))
        return None

    def delete(self, book_id: str) -> bool:
        import shutil
        if not is_safe_book_id(book_id):
            logger.warning("拒绝删除非法 book_id: %s", book_id)
            return False
        book_dir = ensure_child_path(self.dir, self.dir / book_id)
        if not (book_dir / "book.json").exists():
            return False
        shutil.rmtree(book_dir)
        self._cache.pop(book_id, None)
        return True


