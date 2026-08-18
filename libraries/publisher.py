"""
上架 / 发布（Publisher）— 纯规则，无 LLM

上架前检查（5 项 error 级，缺一不可）+ 状态机（writing → finished → published）
+ 按平台格式导出章节文件（供手动投稿番茄 / 起点）。

数据源全部来自磁盘：book.json / outline.json（synopsis）/ storyline.json / chapters/{n}.json。
authoritative 字数一律重算，不依赖 engine 记账字段（断点续写重入可能造成轻微偏差）。
"""
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.json_store import read_json, write_json_atomic
from core.safe_paths import ensure_child_path, is_safe_book_id
from core.text_utils import count_prose_units
import libraries.book_meta as book_meta

# 文件名非法字符（Windows / 常见平台通用）
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')


@dataclass
class PublishCheckItem:
    key: str            # title/synopsis/words/review/finished
    label: str          # 中文标签
    passed: bool
    detail: str
    severity: str = "error"


@dataclass
class PublishReport:
    book_id: str
    platform: str
    passed: bool
    items: list
    total_words: int
    chapters: int
    can_publish: bool
    summary: str
    thresholds: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "platform": self.platform,
            "passed": self.passed,
            "can_publish": self.can_publish,
            "total_words": self.total_words,
            "chapters": self.chapters,
            "summary": self.summary,
            "thresholds": self.thresholds,
            "items": [
                {"key": i.key, "label": i.label, "passed": i.passed,
                 "detail": i.detail, "severity": i.severity}
                for i in self.items
            ],
        }


class Publisher:
    """上架检查 + 状态机 + 平台格式导出。"""

    def __init__(self, book_mgr):
        self.book_mgr = book_mgr
        self.books_dir = Path(getattr(book_mgr, "dir", "books"))

    # ───────────────────────────────────────────
    # 上架前检查（免费规则，无 LLM）
    # ───────────────────────────────────────────
    def build_report(self, book, storyline=None, outline=None,
                     thresholds: dict | None = None) -> PublishReport:
        """对一本书跑 5 项 error 级检查，返回 PublishReport。

        book：BookConfig；storyline/outline 可传入，None 时从磁盘加载。
        thresholds：publish_thresholds() 的返回；默认按 platform 取真实阈值。
        """
        if thresholds is None:
            thresholds = book_meta.publish_thresholds(book.platform or "")
        min_words = int(thresholds.get("min_total_words", 0))
        min_ch = int(thresholds.get("min_chapters", 0))

        if storyline is None:
            try:
                storyline = self.book_mgr.load_storyline(book.book_id)
            except Exception:
                storyline = None
        if outline is None:
            try:
                outline = self.book_mgr.get_outline(book.book_id) or {}
            except Exception:
                outline = {}

        chapters, total_words = self._load_chapters(book)
        min_words = max(min_words, 1)

        items = [
            self._check_title(book),
            self._check_synopsis(outline, storyline),
            self._check_words(total_words, min_words, min_ch),
            self._check_review(chapters),
            self._check_finished(book, total_words, min_words, min_ch),
        ]
        can_publish = all(i.passed for i in items)
        passed = can_publish
        summary = ("✅ 通过上架检查，可以上架" if can_publish
                   else f"❌ {sum(1 for i in items if not i.passed)}/{len(items)} 项未通过，暂不能上架")
        return PublishReport(
            book_id=book.book_id,
            platform=book.platform or "",
            passed=passed,
            items=items,
            total_words=total_words,
            chapters=len(chapters),
            can_publish=can_publish,
            summary=summary,
            thresholds=thresholds,
        )

    def _check_title(self, book) -> PublishCheckItem:
        title = (book.title or "").strip()
        ok = bool(title) and title != "(待定)" and title != "(未命名)"
        detail = f"书名：{title or '（空）'}" if ok else "书名缺失或仍是占位符，请先生成书名/简介"
        return PublishCheckItem("title", "书名齐备", ok, detail)

    def _check_synopsis(self, outline, storyline) -> PublishCheckItem:
        synopsis = ""
        if outline and outline.get("synopsis"):
            synopsis = (outline.get("synopsis") or "").strip()
        if not synopsis and storyline and storyline.basic_info:
            synopsis = ((storyline.basic_info.get("synopsis") or "").strip()
                        if isinstance(storyline.basic_info, dict) else "")
        ok = len(synopsis) >= 20
        detail = f"简介 {len(synopsis)} 字，已就绪" if ok else "简介缺失或过短（<20字），请先生成简介"
        return PublishCheckItem("synopsis", "简介齐备", ok, detail)

    def _check_words(self, total_words: int, min_words: int,
                     min_ch: int) -> PublishCheckItem:
        ok = total_words >= min_words
        detail = (f"已写 {total_words} 字 ≥ {min_words} 字（{min_ch} 章起）"
                  if ok else f"已写 {total_words} 字，未达 {min_words} 字上架线（{min_ch} 章起）")
        return PublishCheckItem("words", "平台字数达标", ok, detail)

    def _check_review(self, chapters: list) -> PublishCheckItem:
        failed = [c.get("num") for c in chapters
                  if c.get("review") and c.get("review").get("passed") is False]
        ok = not failed
        detail = (f"全部 {len(chapters)} 章审查通过" if ok
                  else f"以下章节审查未过：第{'、第'.join(str(n) for n in failed[:5])}章")
        return PublishCheckItem("review", "章节审查通过", ok, detail)

    def _check_finished(self, book, total_words: int, min_words: int,
                        min_ch: int) -> PublishCheckItem:
        all_written = book.current_chapter >= (book.chapter_count or 0) >= 1
        enough_words = total_words >= min_words
        enough_ch = (book.current_chapter or 0) >= min_ch
        ok = all_written or enough_words or enough_ch
        detail = (f"已写 {book.current_chapter}/{book.chapter_count} 章"
                  if ok else f"仅写 {book.current_chapter}/{book.chapter_count} 章，未完本且未达字数线")
        return PublishCheckItem("finished", "完本或达字数线", ok, detail)

    def _load_chapters(self, book):
        """从磁盘加载 1..current_chapter 的章节 dict，并重算总字数。"""
        chapters, total_words = [], 0
        for n in range(1, (book.current_chapter or 0) + 1):
            try:
                ch = self.book_mgr.load_chapter(book.book_id, n)
            except Exception:
                ch = None
            if ch and ch.get("content"):
                ch["num"] = n
                total_words += count_prose_units(ch.get("content") or "")
                chapters.append(ch)
        return chapters, total_words

    # ───────────────────────────────────────────
    # 状态机
    # ───────────────────────────────────────────
    def mark_finished(self, book_id: str) -> dict:
        """标完本：status=finished + finished_at，重算并写回 total_words。"""
        book = self.book_mgr.get(book_id)
        if not book:
            return {"ok": False, "error": "图书不存在"}
        _, total_words = self._load_chapters(book)
        book.status = "finished"
        book.finished_at = datetime.now().isoformat(timespec="seconds")
        book.total_words = total_words
        self.book_mgr.update(book)
        return {"ok": True, "status": book.status,
                "finished_at": book.finished_at, "total_words": total_words}

    def publish(self, book_id: str, force: bool = False) -> dict:
        """标记已上架：status=published + published_at。未通过检查时需 force。"""
        book = self.book_mgr.get(book_id)
        if not book:
            return {"ok": False, "error": "图书不存在"}
        report = self.build_report(book)
        if not report.can_publish and not force:
            return {"ok": False, "error": "上架检查未通过，请先补齐条件",
                    "report": report.to_dict()}
        book.status = "published"
        book.published_at = datetime.now().isoformat(timespec="seconds")
        self.book_mgr.update(book)
        return {"ok": True, "status": book.status,
                "published_at": book.published_at,
                "report": report.to_dict()}

    # ───────────────────────────────────────────
    # 平台格式导出（免费规则，无 LLM）
    # ───────────────────────────────────────────
    def export_book(self, book_id: str, force: bool = True) -> dict:
        """导出全书为投稿包：books/{book_id}/export/{YYYYmmdd_HHMMSS}/。

        生成 meta.json + 逐章 第NNN章_标题.txt + book.txt（合并）+ export.zip。
        force 默认 True：导出允许在未上架时也执行（预览用），但会跳过检查。
        """
        if not is_safe_book_id(book_id):
            return {"ok": False, "error": "非法图书 ID"}
        book = self.book_mgr.get(book_id)
        if not book:
            return {"ok": False, "error": "图书不存在"}
        if (book.current_chapter or 0) < 1:
            return {"ok": False, "error": "尚无已写章节，无法导出"}

        chapters, total_words = self._load_chapters(book)
        outline = self.book_mgr.get_outline(book_id) or {}
        storyline = None
        try:
            storyline = self.book_mgr.load_storyline(book_id)
        except Exception:
            storyline = None
        synopsis = (outline.get("synopsis") or "").strip()
        if not synopsis and storyline and storyline.basic_info:
            synopsis = (storyline.basic_info.get("synopsis") or "").strip()

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_dir = ensure_child_path(
            self.books_dir / book_id, f"books/{book_id}/export/{stamp}")
        export_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "title": book.title or "",
            "pen_name": book.pen_name or "",
            "genre": book.genre or "",
            "sub_genre": book.sub_genre or "",
            "platform": book.platform or "",
            "chapter_count": len(chapters),
            "total_words": total_words,
            "synopsis": synopsis,
            "finished_at": book.finished_at or "",
            "published_at": book.published_at or "",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
        }
        meta_path = export_dir / "meta.json"
        write_json_atomic(str(meta_path), meta)

        files = []
        for ch in chapters:
            title = (ch.get("title") or f"第{ch.get('num')}章").strip()
            safe_title = self._safe_filename(title) or f"第{ch.get('num')}章"
            fname = f"第{ch.get('num'):03d}章_{safe_title}.txt"
            path = export_dir / fname
            path.write_text(ch.get("content") or "", encoding="utf-8")
            files.append(fname)

        # 全书合并
        book_txt = "\n\n".join(
            f"第{ch.get('num')}章 {ch.get('title') or ''}\n\n{ch.get('content') or ''}"
            for ch in chapters)
        (export_dir / "book.txt").write_text(book_txt, encoding="utf-8")
        files.append("book.txt")

        # zip 打包
        zip_name = f"{book_id}_投稿包_{stamp}.zip"
        zip_path = export_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(meta_path, "meta.json")
            for fname in files:
                zf.write(export_dir / fname, fname)
        files.append(zip_name)

        book.exported_at = meta["exported_at"]
        self.book_mgr.update(book)

        return {
            "ok": True,
            "book_id": book_id,
            "export_dir": str(export_dir),
            "stamp": stamp,
            "chapter_count": len(chapters),
            "total_words": total_words,
            "files": sorted(files),
            "zip_path": str(zip_path),
            "zip_name": zip_name,
        }

    @staticmethod
    def _safe_filename(name: str) -> str:
        """清洗文件名非法字符并限长。"""
        cleaned = _ILLEGAL_FILENAME.sub("_", name or "").strip(" .")
        return cleaned[:40] or ""
