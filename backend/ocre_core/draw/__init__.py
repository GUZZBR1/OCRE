"""OCRE-side Draw document (draw.paper.v1): builder + strict validator."""

from .document import (
    add_stroke,
    confirm_erasure,
    copy_document,
    dumps,
    erase_stroke,
    live_strokes,
    loads,
    new_document,
    next_stroke_id,
    stabilize_stroke,
)
from .validator_draw import (
    DrawValidationError,
    DrawValidationReport,
    validate_draw_document,
    validate_draw_document_or_raise,
)

__all__ = [
    "DrawValidationError",
    "DrawValidationReport",
    "add_stroke",
    "confirm_erasure",
    "copy_document",
    "dumps",
    "erase_stroke",
    "live_strokes",
    "loads",
    "new_document",
    "next_stroke_id",
    "stabilize_stroke",
    "validate_draw_document",
    "validate_draw_document_or_raise",
]
