"""Deterministic identity for the style material used by one Plot Run.

Only inputs actually shown to the Writer are included. The unrelated global
sample pool is deliberately excluded so adding an unused sample does not stale
a prepared Plot.
"""
from __future__ import annotations

import hashlib
import json


SELECTOR_VERSION = "plot-sample-v1"


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def rendered_sample_digest(text: str) -> str:
    """Digest the exact rendered sample text shown to the Writer."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def build_snapshot(profile, style_card: str, sample_receipt: dict | None = None,
                   selector_version: str = SELECTOR_VERSION) -> dict:
    receipt = sample_receipt if isinstance(sample_receipt, dict) else {}
    semantic = {
        "profile_id": getattr(profile, "id", "") or "",
        "style_card": style_card or "",
        "selector_version": selector_version,
        "sample_id": receipt.get("sample_id", "") or "",
        "sample_content_digest": receipt.get("content_digest", "") or "",
    }
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
                     current_sample_digest: str = "") -> bool:
    """Compare a prepared style snapshot without selecting or recording samples."""
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
    )
    return current.get("digest") == snapshot.get("digest")
