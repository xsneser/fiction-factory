"""Deterministic identity for the style material used by one Plot Run.

Only inputs actually shown to the Writer are included. The unrelated global
sample pool is deliberately excluded so adding an unused sample does not stale
a prepared Plot.
"""
from __future__ import annotations

import hashlib
import json


SELECTOR_VERSION = "plot-sample-v1"
# 章级样文锚（2026-09-11）：**独立版本号，不改上面那个**。
# `pick_plot_sample` 仍在 legacy 全量工具面按 Plot 抽样并用 plot-sample-v1 记账；把全局版本
# 改名会让 legacy 的在途 token 与历史快照无法解释。章锚走自己的版本，两条链互不干扰。
CHAPTER_ANCHOR_SELECTOR_VERSION = "chapter-anchor-v1"


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def rendered_sample_digest(text: str) -> str:
    """Digest the exact rendered sample text shown to the Writer."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def build_snapshot(profile, style_card: str, sample_receipt: dict | None = None,
                   selector_version: str = SELECTOR_VERSION,
                   anchor_extra: dict | None = None) -> dict:
    """构造风格快照。`anchor_extra` 仅章锚路径使用（scope/chapter_num/anchor_id/…）。

    章锚要把「同一篇样文，但属于不同章节锚」区分开，否则两章共用同一 sample 时快照会撞。
    """
    receipt = sample_receipt if isinstance(sample_receipt, dict) else {}
    semantic = {
        "profile_id": getattr(profile, "id", "") or "",
        "style_card": style_card or "",
        "selector_version": selector_version,
        "sample_id": receipt.get("sample_id", "") or "",
        "sample_content_digest": receipt.get("content_digest", "") or "",
    }
    for k, v in (anchor_extra or {}).items():
        # 空值一律不进 digest：缺键与「键存在但为空」必须等价，否则同一次 prepare 因为
        # 「有没有传 empty 的 anchor_extra」而算出两个指纹。`0` 也算空（chapter_num=0
        # 表示未设置）。
        if v in ("", None, [], {}, 0, False):
            continue
        semantic[k] = v
    return {**semantic, "digest": _digest(semantic)}


def selected_sample_digest(profile, sample_id: str) -> str | None:
    """Read one selected sample's current rendered content without side effects."""
    if not sample_id:
        return ""
    from . import style_samples
    for sample in style_samples.pool_for(profile):
        if sample.id == sample_id:
            return rendered_sample_digest(style_samples.render_reference([sample]))
    return None


def snapshot_matches(profile, style_card: str, snapshot: dict,
                     current_sample_digest: str = "",
                     anchor_extra: dict | None = None) -> bool:
    """Compare a prepared style snapshot without selecting or recording samples.

    `anchor_extra` 必须与 build_snapshot 时传入的一致（章锚路径由调用方从锚现算），
    否则 digest 必然不等、会误判成「样文已改，请重新 prepare」。
    """
    if not isinstance(snapshot, dict):
        return False
    current = build_snapshot(
        profile,
        style_card,
        {
            "sample_id": snapshot.get("sample_id", ""),
            "content_digest": current_sample_digest,
        },
        selector_version=snapshot.get("selector_version") or SELECTOR_VERSION,
        anchor_extra=anchor_extra,
    )
    return current.get("digest") == snapshot.get("digest")
