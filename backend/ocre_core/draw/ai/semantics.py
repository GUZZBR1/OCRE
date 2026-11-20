"""Semantic candidate proposals; persisted proposals require human approval."""
from __future__ import annotations

from typing import Any

from ..document import document_sha256, live_strokes
from .catalog import (
    CATALOG_VERSION,
    MATERIALS,
    MATERIAL_FALLBACK,
    MATERIAL_FALLBACK_VERSION,
    match,
    material_match,
)
from .context import AIContext, build_context
from .interfaces import Classification, MaterialAdvice
from .proposals import Proposal, new_proposal


def _valid_ids(item: Any, stable: set[str]) -> tuple[str, ...] | None:
    ids = getattr(item, "stroke_ids", ()) or (getattr(item, "stroke_id", None),)
    if not isinstance(ids, (tuple, list)) or not ids or any(not isinstance(i, str) or i not in stable for i in ids):
        return None
    return tuple(sorted(set(ids)))


def _confidence(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.7 <= value <= 1.0
    )


def _text(value: Any, *, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def _proposal(ctx: AIContext, kind: str, content: dict[str, Any]) -> Proposal:
    return new_proposal(
        kind=kind,
        content=content,
        document_revision=ctx.revision,
        document_sha256=ctx.document_sha256,
    )


def _eligible(document: dict[str, Any]) -> tuple[str, ...]:
    """Stable closed profiles, matching STEP export's eligibility boundary."""
    eligible = []
    for stroke in live_strokes(document):
        points = stroke.get("points_mm")
        if (stroke.get("status") == "stable" and stroke.get("closed") is True
                and isinstance(points, list) and len(points) >= 3):
            eligible.append(stroke["stroke_id"])
    return tuple(sorted(eligible))


def analyze(document: dict[str, Any], adapter: Any, *, explicit_material: str | None = None) -> list[Proposal]:
    """Create pending classification/material candidates; adapter errors propagate."""
    ctx = build_context(document)
    stable = {s["stroke_id"] for s in ctx.stable_strokes() if isinstance(s.get("stroke_id"), str)}
    classes: dict[frozenset[str], set[str]] = {}
    reasons: dict[frozenset[str], str] = {}
    proposals: list[Proposal] = []
    for c in adapter.classify(ctx):
        if not isinstance(c, Classification) or not _confidence(c.confidence):
            continue
        ids = _valid_ids(c, stable)
        label = match(c.label)
        if ids is None or label is None or not _text(c.rationale, limit=500):
            continue
        group = frozenset(ids)
        classes.setdefault(group, set()).add(label)
        reasons[group] = c.rationale.strip()
    resolved = {ids: next(iter(labels)) for ids, labels in classes.items() if len(labels) == 1}
    for group, label in sorted(resolved.items(), key=lambda item: tuple(sorted(item[0]))):
        ids = sorted(group)
        proposals.append(_proposal(ctx, "semantic-classification", {
            "stroke_ids": ids,
            "label": label,
            "reason": reasons[group],
            "catalog_version": CATALOG_VERSION,
        }))
    if explicit_material and explicit_material.strip():
        return proposals
    # Do not infer one global material from incomplete or overlapping groups.
    eligible = _eligible(document)
    if not eligible or set(eligible) != stable or not resolved or set().union(*resolved) != stable:
        return proposals
    owners: dict[str, frozenset[str]] = {}
    for group in resolved:
        for stroke_id in group:
            if stroke_id in owners and owners[stroke_id] != group:
                return proposals
            owners[stroke_id] = group
    advise_material = getattr(adapter, "advise_material", None)
    if advise_material is None:
        advice = []
    elif not callable(advise_material):
        return proposals
    else:
        advice = advise_material(ctx)
    if not isinstance(advice, (list, tuple)):
        return proposals
    by_group: dict[frozenset[str], set[str]] = {}
    advice_reason: dict[frozenset[str], str] = {}
    invalid_advice = False
    for a in advice:
        if not isinstance(a, MaterialAdvice):
            invalid_advice = True
            continue
        ids = _valid_ids(a, stable)
        if ids is None or not _text(a.material, limit=80) or not _text(a.rationale, limit=500):
            invalid_advice = True
            continue
        group = frozenset(ids)
        part = resolved.get(group)
        if part is None:
            invalid_advice = True
            continue
        material = material_match(part, a.material.strip())
        if material is None:
            invalid_advice = True
            continue
        by_group.setdefault(group, set()).add(material)
        advice_reason[group] = a.rationale.strip()
    if invalid_advice:
        return proposals
    if not advice:
        fallback = []
        for group, part in resolved.items():
            material = MATERIAL_FALLBACK.get(part)
            if material is None:
                return proposals
            fallback.append((group, material))
        fallback_materials = {material for _, material in fallback}
        if len(fallback_materials) != 1:
            return proposals
        material = next(iter(fallback_materials))
        proposals.append(_proposal(ctx, "material-advice", {
            "stroke_ids": sorted(stable),
            "material": material,
            "reason": "single catalog fallback candidate for every identified STEP part",
            "catalog_version": CATALOG_VERSION,
            "source": "catalog",
            "fallback_version": MATERIAL_FALLBACK_VERSION,
        }))
        return proposals
    # Require exact per-part advice coverage, then identical material across all STEP parts.
    chosen = []
    for group in resolved:
        options = by_group.get(group, set())
        if len(options) != 1:
            return proposals
        chosen.append(next(iter(options)))
    materials = set(chosen)
    if len(materials) != 1:
        return proposals
    material = next(iter(materials))
    proposals.append(_proposal(ctx, "material-advice", {
        "stroke_ids": sorted(stable),
        "material": material,
        "reason": " | ".join(advice_reason.values())[:1000],
        "catalog_version": CATALOG_VERSION,
        "source": "adapter+catalog",
    }))
    return proposals


def material_for_step(document: dict[str, Any], proposal: Proposal | None, *, explicit_material: str | None = None) -> str | None:
    """Return explicit material or confirmed catalog candidate covering all STEP strokes."""
    if isinstance(explicit_material, str) and explicit_material.strip():
        return explicit_material.strip()
    if proposal is None or proposal.kind != "material-advice" or proposal.state != "confirmed":
        return None
    if not _text(proposal.decided_by, limit=200) or not _text(proposal.decided_at, limit=100):
        return None
    if proposal.document_sha256 != document_sha256(document) or proposal.document_revision != document.get("revision"):
        return None
    if not isinstance(proposal.content, dict):
        return None
    covered = proposal.content.get("stroke_ids")
    eligible = _eligible(document)
    if not eligible or not isinstance(covered, list) or any(not isinstance(item, str) for item in covered):
        return None
    if tuple(sorted(set(covered))) != eligible:
        return None
    material = proposal.content.get("material")
    if proposal.content.get("catalog_version") != CATALOG_VERSION:
        return None
    if not _text(proposal.content.get("reason"), limit=1000):
        return None
    if not isinstance(material, str) or not material.strip():
        return None
    return material.strip() if any(material_match(part, material) for part in MATERIALS) else None
