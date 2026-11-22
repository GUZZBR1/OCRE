"""Testes do escritor DXF R2000 (F0).

Ressalva explícita: estes testes usam documentos ``draw.paper.v1``
SINTÉTICOS, construídos à mão aqui. A bateria real G0.1/G0.2 (20 folhas
reais medidas à mão, 10/10 DXFs abrindo no LibreCAD) NÃO foi rodada —
ela é um passo posterior, fora deste módulo.
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

import ezdxf

from ocre_core.draw.dxf_export import (
    APPID,
    LAYER,
    DxfExportError,
    build_dxf,
    export_dxf,
)

HEIGHT = 296.47


def make_doc(strokes, **paper):
    doc = {
        "schema": "draw.paper.v1",
        "paper": {"width_mm": 210.0, "height_mm": HEIGHT, **paper},
        "strokes": strokes,
    }
    return doc


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


class DxfExportTests(unittest.TestCase):
    def test_one_lwpolyline_per_stable_stroke_on_draw_strokes_layer(self):
        doc = make_doc(
            [
                stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]]),
                stroke("s-0002", [[10.0, 30.0], [50.0, 30.0], [50.0, 60.0]]),
            ]
        )
        dxf, report = build_dxf(doc)
        # "R2000" é o rótulo; o código ACADVER correspondente é AC1015.
        self.assertEqual(dxf.dxfversion, "AC1015")
        self.assertEqual(report["dxf_version"], "R2000")
        self.assertEqual(dxf.header["$INSUNITS"], 4)
        msp = dxf.modelspace()
        polylines = list(msp.query("LWPOLYLINE"))
        self.assertEqual(len(polylines), 2)
        self.assertEqual(report["entities"], 2)
        self.assertEqual(report["included_stroke_ids"], ["s-0001", "s-0002"])
        for polyline in polylines:
            self.assertEqual(polyline.dxf.layer, LAYER)

    def test_y_is_inverted_paper_down_to_cad_up(self):
        doc = make_doc([stroke("s-0001", [[10.0, 20.0], [50.0, 100.0]])])
        dxf, _ = build_dxf(doc)
        polyline = next(iter(dxf.modelspace().query("LWPOLYLINE")))
        points = list(polyline.get_points(format="xy"))
        self.assertAlmostEqual(points[0][0], 10.0)
        self.assertAlmostEqual(points[0][1], HEIGHT - 20.0)
        self.assertAlmostEqual(points[1][1], HEIGHT - 100.0)

    def test_closed_stroke_sets_flag_70(self):
        doc = make_doc(
            [stroke("s-0001", [[10.0, 10.0], [50.0, 10.0], [50.0, 50.0]], closed=True)]
        )
        dxf, _ = build_dxf(doc)
        polyline = next(iter(dxf.modelspace().query("LWPOLYLINE")))
        self.assertTrue(polyline.closed)
        self.assertEqual(polyline.dxf.flags & 1, 1)

    def test_circle_shape_stays_a_closed_polyline_never_circle(self):
        # Um círculo vetorizado chega como polilinha fechada; o escritor
        # NUNCA emite CIRCLE, mesmo com shape="circle" no documento.
        doc = make_doc(
            [
                stroke(
                    "s-0001",
                    [[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0]],
                    closed=True,
                    shape="circle",
                )
            ]
        )
        dxf, _ = build_dxf(doc)
        msp = dxf.modelspace()
        self.assertEqual(len(list(msp.query("CIRCLE"))), 0)
        self.assertEqual(len(list(msp.query("LWPOLYLINE"))), 1)

    def test_stroke_id_in_xdata_under_own_appid_draw(self):
        doc = make_doc([stroke("s-0007", [[10.0, 20.0], [50.0, 20.0]])])
        dxf, _ = build_dxf(doc)
        self.assertIn(APPID, dxf.appids)
        polyline = next(iter(dxf.modelspace().query("LWPOLYLINE")))
        xdata = polyline.get_xdata(APPID)
        self.assertIsNotNone(xdata)
        values = [value for _, value in xdata]
        self.assertIn("s-0007", values)
        # APPID próprio do Draw existe; nenhum APPID de outro alvo
        # (ex.: IngeCAD) é criado pelo escritor. (setup=True do ezdxf
        # cria APPIDs padrão — por isso só se verifica a presença do
        # DRAW e a ausência de APPIDs de alvos conhecidos.)
        self.assertIn(APPID, {a.dxf.name for a in dxf.appids})
        self.assertNotIn("INGECAD", {a.dxf.name for a in dxf.appids})

    def test_erased_never_exported(self):
        doc = make_doc(
            [
                stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]], status="erased"),
                stroke("s-0002", [[10.0, 30.0], [50.0, 30.0]]),
            ]
        )
        _, report = build_dxf(doc)
        self.assertEqual(report["included_stroke_ids"], ["s-0002"])
        self.assertIn({"stroke_id": "s-0001", "reason": "erased"}, report["skipped"])

    def test_pending_excluded_by_default_included_when_explicit(self):
        doc = make_doc(
            [stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]], status="pending")]
        )
        _, default_report = build_dxf(doc)
        self.assertEqual(default_report["included_stroke_ids"], [])
        _, explicit_report = build_dxf(doc, include_pending=True)
        self.assertEqual(explicit_report["included_stroke_ids"], ["s-0001"])
        # ... mas erased continua fora mesmo com include_pending=True.
        doc2 = make_doc(
            [stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]], status="erased")]
        )
        _, report2 = build_dxf(doc2, include_pending=True)
        self.assertEqual(report2["included_stroke_ids"], [])

    def test_single_point_stroke_is_skipped_and_recorded(self):
        doc = make_doc([stroke("s-0001", [[10.0, 20.0]])])
        _, report = build_dxf(doc)
        self.assertEqual(report["included_stroke_ids"], [])
        self.assertIn(
            {"stroke_id": "s-0001", "reason": "single_point"}, report["skipped"]
        )

    def test_fail_closed_duplicate_stroke_id(self):
        doc = make_doc(
            [
                stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]]),
                stroke("s-0001", [[10.0, 30.0], [50.0, 30.0]]),
            ]
        )
        with self.assertRaises(DxfExportError):
            build_dxf(doc)

    def test_fail_closed_unknown_status(self):
        doc = make_doc(
            [stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]], status="confirmed")]
        )
        with self.assertRaises(DxfExportError):
            build_dxf(doc)

    def test_fail_closed_non_finite_and_out_of_sheet_points(self):
        for bad_points in (
            [[10.0, float("nan")], [50.0, 20.0]],
            [[10.0, float("inf")], [50.0, 20.0]],
            [[-1.0, 20.0], [50.0, 20.0]],
            [[10.0, 20.0], [50.0, HEIGHT + 0.01]],
        ):
            with self.subTest(points=bad_points):
                doc = make_doc([stroke("s-0001", bad_points)])
                with self.assertRaises(DxfExportError):
                    build_dxf(doc)

    def test_fail_closed_closed_with_fewer_than_3_points(self):
        doc = make_doc(
            [stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]], closed=True)]
        )
        with self.assertRaises(DxfExportError):
            build_dxf(doc)

    def test_fail_closed_missing_strokes_and_wrong_schema(self):
        with self.assertRaises(DxfExportError):
            build_dxf({"schema": "draw.paper.v1"})
        with self.assertRaises(DxfExportError):
            build_dxf({"schema": "eir.v0.2", "strokes": []})

    def test_export_writes_file_and_report_sha_matches(self):
        doc = make_doc([stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]])])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "saida.dxf"
            report = export_dxf(doc, out)
            self.assertTrue(out.is_file())
            self.assertEqual(
                report["output_sha256"],
                hashlib.sha256(out.read_bytes()).hexdigest(),
            )
            # Round-trip: o DXF lido de volta tem a mesma geometria.
            read_back = ezdxf.readfile(out)
            polyline = next(iter(read_back.modelspace().query("LWPOLYLINE")))
            points = list(polyline.get_points(format="xy"))
            self.assertAlmostEqual(points[0][1], HEIGHT - 20.0)
            self.assertEqual(
                [v for _, v in polyline.get_xdata(APPID)], ["s-0001"]
            )

    def test_paper_height_from_document_drives_y_flip(self):
        doc = make_doc(
            [stroke("s-0001", [[0.0, 0.0], [10.0, 10.0]])],
            height_mm=200.0,
            width_mm=150.0,
        )
        dxf, _ = build_dxf(doc)
        polyline = next(iter(dxf.modelspace().query("LWPOLYLINE")))
        points = list(polyline.get_points(format="xy"))
        self.assertAlmostEqual(points[0][1], 200.0)
        self.assertAlmostEqual(points[1][1], 190.0)


if __name__ == "__main__":
    unittest.main()
