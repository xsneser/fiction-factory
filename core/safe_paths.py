"""路径与输入安全辅助函数。"""

from __future__ import annotations

import re
from pathlib import Path


_BOOK_ID_RE = re.compile(r"^book_\d{3,}$")
_TIMELINE_ID_RE = re.compile(r"^(?:tl|gen)_[A-Za-z0-9_\-\u4e00-\u9fff]+$")


def is_safe_book_id(value: str) -> bool:
    return bool(_BOOK_ID_RE.fullmatch(value or ""))


def is_safe_timeline_id(value: str) -> bool:
    return bool(_TIMELINE_ID_RE.fullmatch(value or ""))


def ensure_child_path(base: str | Path, target: str | Path) -> Path:
    """返回 target 的绝对路径，并确认它位于 base 之内。"""
    base_path = Path(base).resolve()
    target_path = Path(target).resolve()
    try:
        target_path.relative_to(base_path)
    except ValueError as exc:
        raise ValueError(f"路径越界: {target_path}") from exc
    return target_path


def parse_int(value, default: int, *, min_value: int | None = None,
              max_value: int | None = None) -> int:
    """安全解析整数，非法值回退 default，并按范围裁剪。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed