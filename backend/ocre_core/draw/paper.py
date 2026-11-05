"""Single source of truth for the paper policy (decision D3).

The paper policy follows exactly what the current Draw has — today only
A4 — and must track the most recent Draw version when it changes. This
module is where that policy lives, so changing the sheet means editing
ONE place (here), never hunting literals through the code.

This is configurable for the *operator* (me), not for the end user:
there is no user-facing paper setting. The CLI flags ``--paper`` and
``--draw-config`` are operator flags.

Rules:

- The registry starts with A4 only (the Draw's current sheet). A format
  that is not in the registry is refused, never invented.
- ``PaperSpec`` carries everything the document, validator and DXF
  writer need: nominal and effective size in mm, canvas size in px,
  and the derived scale (mm per canvas pixel).
- ``load_draw_config`` reads a Draw ``config.py`` *as text* (``ast``,
  never imported or executed — the Draw is read-only reference and its
  code must not run inside OCRE) and derives a spec from ``PAPER_W`` /
  ``PAPER_H`` using the A4 pixel-to-mm mapping (850 px <-> 210 mm).
  A canvas that matches a registered spec returns that spec; any other
  canvas derives its mm from the same mapping. A config that cannot be
  parsed, or lacks positive integer ``PAPER_W`` / ``PAPER_H``, is
  refused fail-closed — the caller keeps the A4 default only when no
  config was given at all.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PaperSpec:
    """One paper format: nominal/effective size and canvas geometry."""

    format: str
    width_mm: float
    height_mm: float
    effective_width_mm: float
    effective_height_mm: float
    canvas_width_px: int
    canvas_height_px: int

    @property
    def scale_mm_per_px(self) -> float:
        """mm per canvas pixel (width axis; the effective height agrees)."""
        return self.width_mm / self.canvas_width_px

    def as_dict(self) -> dict[str, Any]:
        """The ``paper`` block of a ``draw.paper.v1`` document."""
        return {
            "format": self.format,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "effective_width_mm": self.effective_width_mm,
            "effective_height_mm": self.effective_height_mm,
            "canvas_width_px": self.canvas_width_px,
            "canvas_height_px": self.canvas_height_px,
            "scale_mm_per_px": self.scale_mm_per_px,
        }


# The Draw's current sheet (upstream santleme/draw HEAD 8a17115,
# draw/config.py: A4, PAPER_W=850, PAPER_H=1200, 210 x 297 mm).
A4 = PaperSpec(
    format="A4",
    width_mm=210.0,
    height_mm=297.0,
    effective_width_mm=210.0,
    effective_height_mm=296.47,
    canvas_width_px=850,
    canvas_height_px=1200,
)

_SPECS: dict[str, PaperSpec] = {A4.format: A4}
DEFAULT_PAPER = "A4"
DEFAULT_SPEC = A4

# The Draw's canonical pixel-to-mm mapping: its A4 canvas maps
# 850 px to 210 mm. Deriving another canvas uses the same mapping.
_A4_SCALE_MM_PER_PX = 210.0 / 850.0


def get_paper_spec(format: str) -> PaperSpec:
    """Return the registered spec for ``format``; unknown formats refuse."""
    try:
        return _SPECS[format]
    except KeyError as exc:
        raise ValueError(f"unknown paper format: {format!r}") from exc


def resolve_paper(paper: str | PaperSpec | None) -> PaperSpec:
    """Resolve ``None`` -> default, a name -> registry, a spec -> itself."""
    if paper is None:
        return DEFAULT_SPEC
    if isinstance(paper, PaperSpec):
        return paper
    if isinstance(paper, str):
        return get_paper_spec(paper)
    raise TypeError(f"paper must be a format name, a PaperSpec or None, got {type(paper).__name__}")


def _read_draw_canvas(path: str | Path) -> tuple[int, int]:
    """Read PAPER_W / PAPER_H from a Draw config.py, as text, via AST.

    The file is never imported or executed. Only top-level assignments
    of plain integer literals count; anything else is refused.
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read Draw config: {path}: {exc}") from exc
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"Draw config is not valid Python: {path}: {exc}") from exc
    found: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in ("PAPER_W", "PAPER_H"):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
            found[target.id] = value.value
    for name in ("PAPER_W", "PAPER_H"):
        if name not in found:
            raise ValueError(f"Draw config {path} has no integer {name} assignment")
        if found[name] <= 0:
            raise ValueError(f"Draw config {path}: {name} must be positive (got {found[name]})")
    return found["PAPER_W"], found["PAPER_H"]


def load_draw_config(path: str | Path) -> PaperSpec:
    """Build a PaperSpec from a Draw ``config.py`` (read-only, AST only).

    A canvas matching a registered spec returns that spec (with its
    nominal values). Any other canvas derives its mm from the A4
    pixel-to-mm mapping; its nominal height then equals the effective
    height, because a config.py carries no nominal mm — the derivation
    is approximate by construction and the spec records the canvas it
    came from in its format name (``DRAW-<W>x<H>``).
    """
    width_px, height_px = _read_draw_canvas(path)
    for spec in _SPECS.values():
        if (spec.canvas_width_px, spec.canvas_height_px) == (width_px, height_px):
            return spec
    width_mm = width_px * _A4_SCALE_MM_PER_PX
    effective_height_mm = height_px * _A4_SCALE_MM_PER_PX
    return PaperSpec(
        format=f"DRAW-{width_px}x{height_px}",
        width_mm=width_mm,
        height_mm=effective_height_mm,
        effective_width_mm=width_mm,
        effective_height_mm=effective_height_mm,
        canvas_width_px=width_px,
        canvas_height_px=height_px,
    )
