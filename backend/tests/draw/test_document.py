import copy
import hashlib
import json
import unittest

from ocre_core.draw import document as doc_mod
from ocre_core.draw.document import (
    add_stroke,
    confirm_erasure,
    copy_document,
    dumps,
    erase_stroke,
    live_strokes,
    loads,
    new_document,
    stabilize_stroke,
)
from ocre_core.draw.validator_draw import (
    StrictJSONError,
    bundled_schema_sha256,
    validate_draw_document_or_raise,
)

SOURCE = {"repo": "santleme/draw", "commit": "8a17115", "origin": "upstream-vectorize"}


def make_doc(order_kind="unknown"):
    return new_document(SOURCE, order_kind=order_kind)


class NewDocumentTests(unittest.TestCase):
    def test_envelope_defaults(self):
        doc = make_doc()
        self.assertEqual(doc["schema"], "draw.paper.v1")
        self.assertEqual(doc["schema_sha256"], bundled_schema_sha256())
        self.assertEqual(doc["revision"], 1)
        self.assertEqual(doc["units"], "mm")
        self.assertEqual(doc["coordinate_system"], "sheet_top_left_y_down")
        self.assertEqual(doc["paper"]["format"], "A4")
        self.assertEqual(doc["paper"]["width_mm"], 210)
        self.assertEqual(doc["paper"]["height_mm"], 297)
        self.assertEqual(doc["paper"]["effective_width_mm"], 210)
        self.assertEqual(doc["paper"]["effective_height_mm"], 296.47)
        self.assertEqual(doc["paper"]["canvas_width_px"], 850)
        self.assertEqual(doc["paper"]["canvas_height_px"], 1200)
        self.assertAlmostEqual(doc["paper"]["scale_mm_per_px"], 210 / 850)
        self.assertEqual(doc["strokes"], [])
        self.assertEqual(doc["order_kind"], "unknown")

    def test_origin_is_explicit_never_default(self):
        with self.assertRaises(ValueError):
            new_document({"repo": "santleme/draw", "commit": "8a17115"})
        with self.assertRaises(ValueError):
            new_document(
                {"repo": "santleme/draw", "commit": "8a17115", "origin": "somewhere"}
            )

    def test_order_kind_temporal_without_stamps_is_allowed_but_timestamps_are_not(self):
        doc = make_doc(order_kind="temporal-stamps")
        self.assertEqual(doc["order_kind"], "temporal-stamps")

    def test_doc_id_is_uuid_v4(self):
        doc = make_doc()
        self.assertRegex(
            doc["doc_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )


class AddStrokeTests(unittest.TestCase):
    def test_assigns_stable_id_order_and_length(self):
        doc = make_doc()
        first = add_stroke(doc, [[10, 10], [20, 10]])
        second = add_stroke(doc, [[0, 0], [3, 4]], closed=False)
        self.assertEqual(first["stroke_id"], "s-0001")
        self.assertEqual(first["order"], 0)
        self.assertEqual(first["status"], "pending")
        self.assertAlmostEqual(first["length_mm"], 10.0)
        self.assertEqual(second["stroke_id"], "s-0002")
        self.assertEqual(second["order"], 1)
        self.assertAlmostEqual(second["length_mm"], 5.0)
        self.assertEqual(doc["revision"], 3)  # created at 1, +1 per stroke

    def test_closed_length_includes_closing_segment(self):
        doc = make_doc()
        stroke = add_stroke(doc, [[0, 0], [3, 0], [3, 4]], closed=True)
        self.assertAlmostEqual(stroke["length_mm"], 3 + 4 + 5)

    def test_rejects_point_outside_sheet(self):
        doc = make_doc()
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [210.01, 10]])
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [10, 296.48]])
        self.assertEqual(doc["strokes"], [])

    def test_rejects_closed_with_fewer_than_3_points(self):
        doc = make_doc()
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [1, 1]], closed=True)

    def test_rejects_non_finite_points(self):
        doc = make_doc()
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [float("nan"), 1]])
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [float("inf"), 1]])

    def test_circle_params_stored_and_imply_circle_shape(self):
        doc = make_doc()
        stroke = add_stroke(
            doc,
            [[15, 10], [10, 15], [5, 10], [10, 5]],
            closed=True,
            circle={"cx_mm": 10, "cy_mm": 10, "r_mm": 5},
        )
        self.assertEqual(stroke["shape"], "circle")
        self.assertEqual(
            stroke["circle"], {"cx_mm": 10.0, "cy_mm": 10.0, "r_mm": 5.0}
        )

    def test_circle_rejects_bad_params(self):
        doc = make_doc()
        points = [[10, 10], [20, 10], [20, 20]]
        with self.assertRaises(ValueError):  # chave faltando
            add_stroke(doc, points, closed=True,
                       circle={"cx_mm": 10, "cy_mm": 10})
        with self.assertRaises(ValueError):  # raio não positivo
            add_stroke(doc, points, closed=True,
                       circle={"cx_mm": 10, "cy_mm": 10, "r_mm": 0})
        with self.assertRaises(ValueError):  # fora da folha
            add_stroke(doc, points, closed=True,
                       circle={"cx_mm": 205, "cy_mm": 10, "r_mm": 10})
        with self.assertRaises(ValueError):  # shape incompatível
            add_stroke(doc, points, closed=True, shape="polygon",
                       circle={"cx_mm": 10, "cy_mm": 10, "r_mm": 5})
        self.assertEqual(doc["strokes"], [])

    def test_circle_tangent_to_edge_within_rounding_noise(self):
        """Círculo tangente à borda: o fit arredonda a 3 casas e pode
        exceder a folha por ~1µm — dentro do eps, aceita; muito além,
        segue fora da folha."""
        doc = make_doc()
        stroke = add_stroke(
            doc,
            [[10, 10], [20, 10], [20, 20]],
            closed=True,
            circle={"cx_mm": 205, "cy_mm": 10, "r_mm": 5.0005},  # 210.0005 ≤ eps
        )
        self.assertEqual(stroke["circle"]["r_mm"], 5.0005)
        validate_draw_document_or_raise(doc)  # o validator concorda
        with self.assertRaises(ValueError):  # 0.01 mm fora: não é ruído
            add_stroke(
                doc,
                [[10, 10], [20, 10], [20, 20]],
                closed=True,
                circle={"cx_mm": 205, "cy_mm": 10, "r_mm": 5.01},
            )

    def test_timestamps_require_temporal_stamps(self):
        doc = make_doc(order_kind="unknown")
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [1, 1]], t_start_ms=0, t_end_ms=10)
        doc = make_doc(order_kind="temporal-stamps")
        stroke = add_stroke(doc, [[0, 0], [1, 1]], t_start_ms=0, t_end_ms=10)
        self.assertEqual((stroke["t_start_ms"], stroke["t_end_ms"]), (0, 10))
        with self.assertRaises(ValueError):
            add_stroke(doc, [[0, 0], [1, 1]], t_start_ms=10, t_end_ms=0)


class LifecycleTests(unittest.TestCase):
    def test_stabilize_then_erase_keeps_tombstone(self):
        doc = make_doc()
        add_stroke(doc, [[10, 10], [20, 10]])
        stabilize_stroke(doc, "s-0001")
        self.assertEqual(doc["strokes"][0]["status"], "stable")
        with self.assertRaises(ValueError):
            stabilize_stroke(doc, "s-0001")
        erase_stroke(doc, "s-0001", reason="hand-erase", at_ms=1234)
        stroke = doc["strokes"][0]
        self.assertEqual(stroke["status"], "erased")
        self.assertEqual(stroke["erased_at_ms"], 1234)
        self.assertEqual(stroke["erased_reason"], "hand-erase")
        self.assertEqual(len(doc["strokes"]), 1)  # never removed
        self.assertEqual(live_strokes(doc), [])
        with self.assertRaises(ValueError):
            erase_stroke(doc, "s-0001", reason="manual", at_ms=2000)

    def test_inferred_tombstone_is_provisional_until_confirmed(self):
        doc = make_doc()
        add_stroke(doc, [[10, 10], [20, 10]])
        erase_stroke(doc, "s-0001", reason="manual", at_ms=500, inferred=True)
        stroke = doc["strokes"][0]
        self.assertEqual(stroke["status"], "erased")
        self.assertTrue(stroke["erased_inferred"])
        self.assertNotIn("erased_reason", stroke)
        confirm_erasure(doc, "s-0001", reason="page-turn")
        self.assertEqual(stroke["erased_reason"], "page-turn")
        self.assertNotIn("erased_inferred", stroke)

    def test_revision_bumps_once_per_mutation(self):
        doc = make_doc()
        self.assertEqual(doc["revision"], 1)
        add_stroke(doc, [[0, 0], [1, 1]])
        self.assertEqual(doc["revision"], 2)
        stabilize_stroke(doc, "s-0001")
        self.assertEqual(doc["revision"], 3)
        erase_stroke(doc, "s-0001", reason="manual", at_ms=1)
        self.assertEqual(doc["revision"], 4)

    def test_copy_document_is_independent(self):
        doc = make_doc()
        add_stroke(doc, [[0, 0], [1, 1]])
        prev = copy_document(doc)
        add_stroke(doc, [[0, 0], [2, 2]])
        self.assertEqual(len(prev["strokes"]), 1)
        self.assertEqual(len(doc["strokes"]), 2)


class SerialisationTests(unittest.TestCase):
    def test_canonical_bytes_are_deterministic(self):
        doc = make_doc()
        add_stroke(doc, [[10, 10], [20, 10]])
        first = doc_mod.canonical_document_bytes(doc)
        reordered = copy.deepcopy(doc)
        reordered["strokes"][0] = dict(reversed(list(reordered["strokes"][0].items())))
        self.assertEqual(first, doc_mod.canonical_document_bytes(reordered))
        self.assertEqual(
            doc_mod.document_sha256(doc),
            hashlib.sha256(first).hexdigest(),
        )

    def test_round_trip_through_dumps_loads(self):
        doc = make_doc()
        add_stroke(doc, [[10, 10], [20, 10]], shape="line")
        self.assertEqual(loads(dumps(doc)), doc)

    def test_loads_rejects_duplicate_keys_and_non_finite(self):
        for source in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(source=source), self.assertRaises(StrictJSONError):
                loads(source)

    def test_loads_rejects_non_object(self):
        with self.assertRaises(StrictJSONError):
            loads("[1, 2, 3]")

    def test_dumps_is_plain_json_not_jcs(self):
        # Canonical document serialisation is json.dumps(sort_keys=True);
        # the JCS/RFC 8785 fingerprint is a different module's job.
        doc = make_doc()
        text = dumps(doc)
        self.assertEqual(
            text,
            json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
