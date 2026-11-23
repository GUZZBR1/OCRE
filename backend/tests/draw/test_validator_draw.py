import unittest

from ocre_core.draw.document import (
    add_stroke,
    confirm_erasure,
    copy_document,
    dumps,
    erase_stroke,
    loads,
    new_document,
    stabilize_stroke,
)
from ocre_core.draw.validator_draw import (
    DrawValidationError,
    bundled_schema_sha256,
    validate_draw_document,
    validate_draw_document_or_raise,
)

SOURCE = {"repo": "santleme/draw", "commit": "8a17115", "origin": "upstream-vectorize"}


def make_valid_doc(order_kind="unknown"):
    doc = new_document(SOURCE, order_kind=order_kind)
    add_stroke(doc, [[10, 10], [20, 10], [20, 20]], shape="polyline")
    add_stroke(doc, [[50, 50], [60, 50], [60, 60], [50, 60]], closed=True, shape="rectangle")
    return doc


def codes(report):
    return [d.code for d in report.diagnostics]


def assert_invalid_with(testcase, doc, code, previous=None):
    report = validate_draw_document(doc, previous=previous)
    testcase.assertFalse(report.valid, report.as_dict())
    testcase.assertIn(code, codes(report), report.as_dict())
    return report


class ValidDocumentTests(unittest.TestCase):
    def test_builder_document_is_valid(self):
        report = validate_draw_document(make_valid_doc())
        self.assertTrue(report.valid, report.as_dict())
        self.assertEqual(report.status, "valid")
        self.assertEqual(report.schema_sha256, bundled_schema_sha256())

    def test_json_round_trip_is_valid(self):
        report = validate_draw_document(dumps(make_valid_doc()))
        self.assertTrue(report.valid, report.as_dict())

    def test_validate_or_raise_returns_report(self):
        report = validate_draw_document_or_raise(make_valid_doc())
        self.assertTrue(report.valid)

    def test_erased_tombstone_is_valid(self):
        doc = make_valid_doc()
        erase_stroke(doc, "s-0001", reason="hand-erase", at_ms=100)
        self.assertTrue(validate_draw_document(doc).valid)

    def test_inferred_tombstone_is_valid(self):
        doc = make_valid_doc()
        erase_stroke(doc, "s-0001", reason="manual", at_ms=100, inferred=True)
        self.assertTrue(validate_draw_document(doc).valid)

    def test_temporal_stamps_document_is_valid(self):
        doc = make_valid_doc(order_kind="temporal-stamps")
        add_stroke(doc, [[0, 0], [1, 1]], t_start_ms=0, t_end_ms=5)
        self.assertTrue(validate_draw_document(doc).valid)

    def test_circle_block_document_is_valid(self):
        doc = make_valid_doc()
        add_stroke(
            doc,
            [[15, 10], [10, 15], [5, 10], [10, 5]],
            closed=True,
            circle={"cx_mm": 10, "cy_mm": 10, "r_mm": 5},
        )
        self.assertTrue(validate_draw_document(doc).valid)


class FailClosedTests(unittest.TestCase):
    def assert_invalid_with(self, doc, code, previous=None):
        return assert_invalid_with(self, doc, code, previous)

    def test_missing_required_field(self):
        doc = make_valid_doc()
        del doc["doc_id"]
        self.assert_invalid_with(doc, "schema_validation")

    def test_units_not_mm(self):
        doc = make_valid_doc()
        doc["units"] = "cm"
        self.assert_invalid_with(doc, "unsupported_unit")

    def test_scale_incompatible_with_paper(self):
        doc = make_valid_doc()
        doc["paper"]["scale_mm_per_px"] = 0.5
        self.assert_invalid_with(doc, "scale_incompatible_with_paper")

    def test_point_outside_sheet(self):
        doc = make_valid_doc()
        doc["strokes"][0]["points_mm"] = [[10, 10], [211, 10]]
        doc["strokes"][0]["length_mm"] = 201.0
        self.assert_invalid_with(doc, "point_outside_sheet")

    def test_naive_datetime_does_not_abort_semantic_checks(self):
        # Regressão: created_at naive + updated_at aware explodia a
        # comparação em TypeError, abortava o _semantic inteiro e
        # escondia o erro do traço atrás de invalid_document_shape.
        doc = make_valid_doc()
        doc["created_at"] = "2026-01-01T00:00:00"  # naive (schema flagra)
        doc["strokes"][0]["points_mm"] = [[10, 10], [211, 10]]
        doc["strokes"][0]["length_mm"] = 1.0  # diverge dos pontos
        report = validate_draw_document(doc)
        self.assertFalse(report.valid)
        self.assertIn("point_outside_sheet", codes(report))
        self.assertIn("length_mismatch", codes(report))
        self.assertNotIn("invalid_document_shape", codes(report))

    def test_duplicate_order(self):
        doc = make_valid_doc()
        doc["strokes"][1]["order"] = 0
        self.assert_invalid_with(doc, "duplicate_order")

    def test_order_gap(self):
        doc = make_valid_doc()
        doc["strokes"][1]["order"] = 5
        self.assert_invalid_with(doc, "order_gap")

    def test_duplicate_stroke_id(self):
        doc = make_valid_doc()
        doc["strokes"][1]["stroke_id"] = "s-0001"
        self.assert_invalid_with(doc, "duplicate_id")

    def test_closed_stroke_with_fewer_than_3_points(self):
        doc = make_valid_doc()
        doc["strokes"][0]["points_mm"] = [[10, 10], [20, 10]]
        doc["strokes"][0]["closed"] = True
        doc["strokes"][0]["length_mm"] = 20.0
        self.assert_invalid_with(doc, "closed_stroke_too_short")

    def test_negative_length(self):
        doc = make_valid_doc()
        doc["strokes"][0]["length_mm"] = -1.0
        self.assert_invalid_with(doc, "schema_validation")

    def test_length_mismatch(self):
        doc = make_valid_doc()
        doc["strokes"][0]["length_mm"] = 999.0
        self.assert_invalid_with(doc, "length_mismatch")

    def test_nan_and_inf_rejected(self):
        doc = make_valid_doc()
        doc["strokes"][0]["points_mm"] = [[10, 10], [float("nan"), 10]]
        self.assert_invalid_with(doc, "invalid_json")
        report = validate_draw_document('{"schema": "draw.paper.v1", "x": Infinity}')
        self.assertFalse(report.valid)
        self.assertIn("invalid_json", codes(report))

    def test_unknown_schema_digest(self):
        doc = make_valid_doc()
        doc["schema_sha256"] = "0" * 64
        self.assert_invalid_with(doc, "unknown_schema_digest")

    def test_legacy_schema_digest_still_validates(self):
        # Documento gravado antes do bloco "circle" entrar no schema:
        # o digest antigo segue aceito via legacy_sha256 do lock.
        doc = make_valid_doc()
        doc["schema_sha256"] = (
            "1ea8e4fdb8a8058aa3fdea4c60b81a699e58a21154d72308333d71dc1df5bd80"
        )
        self.assertTrue(validate_draw_document(doc).valid)

    def test_timestamps_without_stamps(self):
        doc = make_valid_doc()
        doc["strokes"][0]["t_start_ms"] = 0
        doc["strokes"][0]["t_end_ms"] = 5
        self.assert_invalid_with(doc, "timestamps_without_stamps")

    def test_t_end_before_t_start(self):
        doc = make_valid_doc(order_kind="temporal-stamps")
        doc["strokes"][0]["t_start_ms"] = 10
        doc["strokes"][0]["t_end_ms"] = 5
        self.assert_invalid_with(doc, "invalid_time_range")

    def test_temporal_stamps_claim_without_any_stamps(self):
        # order_kind temporal-stamps sem nenhum stroke carimbado é
        # alegação falsa: o schema exige, faltava enforcement.
        doc = make_valid_doc(order_kind="temporal-stamps")
        self.assert_invalid_with(doc, "temporal_stamps_without_stamps")

    def test_temporal_stamps_with_empty_strokes_is_refused(self):
        doc = new_document(SOURCE, order_kind="temporal-stamps")
        self.assert_invalid_with(doc, "temporal_stamps_without_stamps")

    def test_erased_without_erasure_event(self):
        doc = make_valid_doc()
        doc["strokes"][0]["status"] = "erased"
        self.assert_invalid_with(doc, "missing_erasure_event")
        self.assert_invalid_with(doc, "missing_erasure_reason")

    def test_erasure_fields_on_live_stroke(self):
        doc = make_valid_doc()
        doc["strokes"][0]["erased_at_ms"] = 10
        self.assert_invalid_with(doc, "unexpected_erasure_field")

    def test_updated_before_created(self):
        doc = make_valid_doc()
        doc["updated_at"] = "2020-01-01T00:00:00Z"
        self.assert_invalid_with(doc, "updated_before_created")

    def test_non_object_document(self):
        report = validate_draw_document([1, 2, 3])
        self.assertFalse(report.valid)
        self.assertIn("invalid_document_type", codes(report))

    def test_validate_or_raise_raises(self):
        doc = make_valid_doc()
        doc["units"] = "cm"
        with self.assertRaises(DrawValidationError) as ctx:
            validate_draw_document_or_raise(doc)
        self.assertFalse(ctx.exception.report.valid)

    def test_circle_without_circle_shape_is_invalid(self):
        doc = make_valid_doc()
        doc["strokes"][0]["circle"] = {"cx_mm": 15, "cy_mm": 15, "r_mm": 3}
        self.assert_invalid_with(doc, "circle_without_circle_shape")

    def test_circle_outside_sheet_is_invalid(self):
        doc = make_valid_doc()
        doc["strokes"][1]["shape"] = "circle"
        doc["strokes"][1]["circle"] = {"cx_mm": 208, "cy_mm": 50, "r_mm": 10}
        self.assert_invalid_with(doc, "circle_outside_sheet")

    def test_circle_bad_params_are_invalid(self):
        doc = make_valid_doc()
        doc["strokes"][1]["shape"] = "circle"
        doc["strokes"][1]["circle"] = {"cx_mm": 55, "cy_mm": 55, "r_mm": -1}
        self.assert_invalid_with(doc, "schema_validation")
        doc["strokes"][1]["circle"] = {"cx_mm": 55, "cy_mm": 55, "r_mm": 5, "x": 1}
        self.assert_invalid_with(doc, "schema_validation")


class PreviousRevisionTests(unittest.TestCase):
    def assert_invalid_with(self, doc, code, previous=None):
        return assert_invalid_with(self, doc, code, previous)
    def test_valid_transition_pending_to_stable(self):
        doc = make_valid_doc()
        prev = copy_document(doc)
        stabilize_stroke(doc, "s-0001")
        report = validate_draw_document(doc, previous=prev)
        self.assertTrue(report.valid, report.as_dict())

    def test_revision_must_increase_by_exactly_one(self):
        doc = make_valid_doc()
        prev = copy_document(doc)
        # Same revision: mutate without bumping.
        doc["strokes"][0]["status"] = "stable"
        self.assert_invalid_with(doc, "revision_not_monotonic", previous=prev)
        doc = make_valid_doc()
        prev = copy_document(doc)
        stabilize_stroke(doc, "s-0001")
        doc["revision"] = prev["revision"] + 2
        self.assert_invalid_with(doc, "revision_not_monotonic", previous=prev)

    def test_stable_to_pending_is_forbidden(self):
        doc = make_valid_doc()
        stabilize_stroke(doc, "s-0001")
        prev = copy_document(doc)
        doc["strokes"][0]["status"] = "pending"
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "invalid_status_transition", previous=prev)

    def test_erased_is_terminal(self):
        doc = make_valid_doc()
        erase_stroke(doc, "s-0001", reason="manual", at_ms=10)
        prev = copy_document(doc)
        doc["strokes"][0]["status"] = "stable"
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "invalid_status_transition", previous=prev)

    def test_stable_points_are_immutable(self):
        doc = make_valid_doc()
        stabilize_stroke(doc, "s-0001")
        prev = copy_document(doc)
        doc["strokes"][0]["points_mm"] = [[10, 10], [30, 10], [30, 30]]
        doc["strokes"][0]["length_mm"] = 40.0
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "stable_points_mutated", previous=prev)

    def test_stable_circle_params_are_immutable(self):
        doc = make_valid_doc()
        add_stroke(
            doc,
            [[15, 10], [10, 15], [5, 10], [10, 5]],
            closed=True,
            circle={"cx_mm": 10, "cy_mm": 10, "r_mm": 5},
        )
        stabilize_stroke(doc, "s-0003")
        prev = copy_document(doc)
        doc["strokes"][2]["circle"]["r_mm"] = 9.0
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "stable_points_mutated", previous=prev)

    def test_pending_points_may_still_change(self):
        doc = make_valid_doc()
        prev = copy_document(doc)
        doc["strokes"][0]["points_mm"] = [[10, 10], [30, 10], [30, 30]]
        doc["strokes"][0]["length_mm"] = 40.0
        doc["revision"] = prev["revision"] + 1
        report = validate_draw_document(doc, previous=prev)
        self.assertTrue(report.valid, report.as_dict())

    def test_stroke_removal_is_forbidden(self):
        doc = make_valid_doc()
        prev = copy_document(doc)
        doc["strokes"] = doc["strokes"][1:]
        doc["strokes"][0]["order"] = 0
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "stroke_removed", previous=prev)

    def test_tombstone_fields_are_immutable(self):
        doc = make_valid_doc()
        erase_stroke(doc, "s-0001", reason="manual", at_ms=100)
        prev = copy_document(doc)
        doc["strokes"][0]["erased_at_ms"] = 999
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "stable_points_mutated", previous=prev)

    def test_confirmed_tombstone_reason_is_final(self):
        doc = make_valid_doc()
        erase_stroke(doc, "s-0001", reason="manual", at_ms=100)
        prev = copy_document(doc)
        doc["strokes"][0]["erased_reason"] = "page-turn"
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "stable_points_mutated", previous=prev)

    def test_provisional_tombstone_can_still_be_confirmed(self):
        # confirm_erasure é a mutação legítima documentada: grava o
        # erased_reason e remove erased_inferred — tem de continuar
        # válida contra previous.
        doc = make_valid_doc()
        erase_stroke(doc, "s-0001", reason="manual", at_ms=100, inferred=True)
        prev = copy_document(doc)
        confirm_erasure(doc, "s-0001", reason="hand-erase")
        report = validate_draw_document(doc, previous=prev)
        self.assertTrue(report.valid, report.as_dict())

    def test_stable_order_and_timestamps_are_immutable(self):
        doc = make_valid_doc(order_kind="temporal-stamps")
        add_stroke(doc, [[0, 0], [1, 1]], t_start_ms=0, t_end_ms=5)
        stabilize_stroke(doc, "s-0003")
        for field, value in (("order", 9), ("t_start_ms", 2), ("t_end_ms", 9)):
            with self.subTest(field=field):
                doc2 = copy_document(doc)
                doc2["strokes"][2][field] = value
                doc2["revision"] = doc["revision"] + 1
                self.assert_invalid_with(
                    doc2, "stable_points_mutated", previous=doc)

    def test_doc_id_is_immutable(self):
        doc = make_valid_doc()
        prev = copy_document(doc)
        doc["doc_id"] = "00000000-0000-4000-8000-000000000000"
        doc["revision"] = prev["revision"] + 1
        self.assert_invalid_with(doc, "doc_id_changed", previous=prev)

    def test_previous_must_be_a_document(self):
        report = validate_draw_document(make_valid_doc(), previous=[1, 2])
        self.assertFalse(report.valid)
        self.assertIn("invalid_previous_document", codes(report))


class CustomSchemaTests(unittest.TestCase):
    def test_supplied_schema_uses_canonical_digest(self):
        from ocre_core.draw.validator_draw import _canonical_bytes, load_bundled_schema

        schema, _, _, _ = load_bundled_schema()
        doc = make_valid_doc()
        # Canonical digest of the same schema dict differs from the file-bytes
        # digest, so the declared file digest is unknown under a supplied schema.
        report = validate_draw_document(doc, schema=schema)
        self.assertFalse(report.valid)
        self.assertIn("unknown_schema_digest", codes(report))
        # Declaring the canonical digest makes it valid again.
        doc["schema_sha256"] = __import__("hashlib").sha256(
            _canonical_bytes(schema)
        ).hexdigest()
        self.assertTrue(validate_draw_document(doc, schema=schema).valid)

    def test_loads_round_trip_keeps_document_valid(self):
        doc = loads(dumps(make_valid_doc()))
        self.assertTrue(validate_draw_document(doc).valid)


class StrictJSONTests(unittest.TestCase):
    def test_lone_surrogate_in_string_is_refused(self):
        # json.loads aceita \ud800 solto, mas não é JSON estrito: antes
        # o walker não olhava str e o valor seguia adiante.
        from ocre_core.draw.validator_draw import (
            StrictJSONError, parse_json_strict,
        )
        with self.assertRaises(StrictJSONError):
            parse_json_strict('"\\ud800"')
        with self.assertRaises(StrictJSONError):
            parse_json_strict('{"\\ud800": 1}')

    def test_oversized_int_literal_is_refused_not_raw_valueerror(self):
        # CPython rejeita literais int > 4300 dígitos com ValueError —
        # não JSONDecodeError — e ele escapava cru do parse.
        from ocre_core.draw.validator_draw import (
            StrictJSONError, parse_json_strict,
        )
        with self.assertRaises(StrictJSONError):
            parse_json_strict("9" * 5000)


if __name__ == "__main__":
    unittest.main()
