"""Shared document helpers for the draw export tests."""

HEIGHT = 296.47


def make_doc(strokes, **paper):
    return {
        "schema": "draw.paper.v1",
        "paper": {"width_mm": 210.0, "height_mm": HEIGHT, **paper},
        "strokes": strokes,
    }


def stroke(stroke_id, points, status="stable", closed=False, **extra):
    item = {
        "stroke_id": stroke_id,
        "order": 0,
        "status": status,
        "points_mm": points,
        "closed": closed,
    }
    item.update(extra)
    return item
