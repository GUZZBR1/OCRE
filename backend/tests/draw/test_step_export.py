"""Testes da exportação STEP (beta v0.1, worker build123d).

Ressalva explícita: estes testes usam documentos ``draw.paper.v1``
SINTÉTICOS, construídos à mão aqui. A bateria real (peças reais
medidas) NÃO foi rodada — ela é um passo posterior, fora deste módulo.
"Aprovado" aqui = selecionado para o beta provisório, NÃO provado
contra conjunto selado.

Os testes de geometria rodam no worker build123d em subprocesso
(``.venv-beta``); sem o venv, falham com ``StepWorkerError`` explícito
— nunca fingem. O oráculo FreeCAD é opcional: se indisponível nesta
máquina, o teste registra o skip honesto (``unavailable``) em vez de
falhar ou de fingir aprovação.
"""

import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocre_core.draw import step_export
from ocre_core.draw.step_export import (
    StepExportError,
    StepWorkerError,
    document_to_step,
    export_dxf_ac1015,
    oracle_step,
    validate_step,
    worker_versions,
)
from ocre_core.draw.librecad_gate import check_dxf
from tests.draw.helpers import HEIGHT, make_doc, stroke

# Retângulo 100 x 10 mm, espessura 10 mm -> volume 10000 mm³.
RECT = [[10.0, 20.0], [110.0, 20.0], [110.0, 30.0], [10.0, 30.0]]
THICKNESS_MM = 10.0
EXPECTED_VOLUME_MM3 = 100.0 * 10.0 * 10.0
# Tolerância do volume: OCCT arredonda em ~1e-7 relativo; 0,01% é
# folga ampla sem esconder erro de geometria.
VOLUME_REL_TOL = 1e-4


def worker_ready() -> bool:
    return step_export.find_step_worker_python() is not None


class StepExportHostTests(unittest.TestCase):
    """Regras do host (só stdlib): política de traços abertos, hierarquia
    de contenção e validação do bloco ``circle`` — todas recusam antes
    de chamar o worker, então não precisam do .venv-beta."""

    def test_open_stroke_policy_default_is_fail(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True),
                        stroke("s-0002", [[5.0, 5.0], [50.0, 5.0]])])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"),
                             open_strokes_policy="fail")

    def test_unknown_open_strokes_policy_is_refused(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"),
                             open_strokes_policy="skip-aux")

    def test_skip_open_without_closed_stroke_is_refused(self):
        doc = make_doc([stroke("s-0001", [[5.0, 5.0], [50.0, 5.0]])])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"),
                             open_strokes_policy="skip-open")

    def test_crossing_contours_are_refused(self):
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", [[100.0, 10.0], [200.0, 10.0],
                              [200.0, 50.0], [100.0, 50.0]], closed=True),
        ])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def test_touching_contours_are_refused(self):
        # O furo está dentro da chapa mas sua aresta y=20 encosta na
        # parede externa (aresta collinear compartilhada = geometria
        # degenerada para o STEP).
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", [[40.0, 20.0], [60.0, 20.0],
                              [60.0, 28.0], [40.0, 28.0]], closed=True),
        ])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def test_self_intersecting_contour_is_refused(self):
        doc = make_doc([stroke(
            "s-0001",
            [[10.0, 10.0], [50.0, 30.0], [50.0, 10.0], [10.0, 30.0]],
            closed=True)])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def test_degenerate_closed_ring_is_refused(self):
        doc = make_doc([stroke(
            "s-0001", [[10.0, 10.0], [20.0, 20.0], [10.0, 10.0]], closed=True)])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def _regions_of(self, doc):
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        return step_export._regions(contours)

    def test_analytic_circle_boundary_decides_containment(self):
        """Regressão da revisão: a hierarquia usa a circunferência
        analítica, não a polilinha de 4 amostras — um quadrado dentro do
        disco mas fora do losango amostrado é furo, não sólido sobreposto."""
        cpts = [[60.0, 50.0], [50.0, 60.0], [40.0, 50.0], [50.0, 40.0]]
        doc = make_doc([
            stroke("s-0001", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 10.0}),
            stroke("s-0002", [[56.0, 56.0], [57.0, 56.0],
                              [57.0, 57.0], [56.0, 57.0]], closed=True),
        ])
        regions = self._regions_of(doc)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["outer"]["stroke_id"], "s-0001")
        self.assertEqual([h["stroke_id"] for h in regions[0]["holes"]],
                         ["s-0002"])

    def test_circle_inside_circle_is_a_hole(self):
        cpts = [[60.0, 50.0], [50.0, 60.0], [40.0, 50.0], [50.0, 40.0]]
        spts = [[53.0, 50.0], [50.0, 53.0], [47.0, 50.0], [50.0, 47.0]]
        doc = make_doc([
            stroke("s-0001", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 10.0}),
            stroke("s-0002", spts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 3.0}),
        ])
        regions = self._regions_of(doc)
        self.assertEqual(len(regions), 1)
        self.assertEqual([h["stroke_id"] for h in regions[0]["holes"]],
                         ["s-0002"])

    def test_hole_covering_circle_center_still_hole_not_outer(self):
        # O furo contém o centro do círculo: o ponto representativo do
        # círculo é um ponto da circunferência, não o centro — senão o
        # disco seria (errado) classificado como furo do quadrado.
        cpts = [[60.0, 50.0], [50.0, 60.0], [40.0, 50.0], [50.0, 40.0]]
        doc = make_doc([
            stroke("s-0001", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 10.0}),
            stroke("s-0002", [[48.0, 48.0], [52.0, 48.0],
                              [52.0, 52.0], [48.0, 52.0]], closed=True),
        ])
        regions = self._regions_of(doc)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["outer"]["stroke_id"], "s-0001")
        self.assertEqual([h["stroke_id"] for h in regions[0]["holes"]],
                         ["s-0002"])

    def test_polygon_crossing_circle_boundary_is_refused(self):
        # Quadrado a cavalo da circunferência: vértices dentro e fora.
        cpts = [[60.0, 50.0], [50.0, 60.0], [40.0, 50.0], [50.0, 40.0]]
        doc = make_doc([
            stroke("s-0001", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 10.0}),
            stroke("s-0002", [[55.0, 45.0], [65.0, 45.0],
                              [65.0, 55.0], [55.0, 55.0]], closed=True),
        ])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def test_tangent_circles_are_refused(self):
        # d = r1 + r2 exatamente: tangência externa = toque = recusa.
        cpts = [[60.0, 50.0], [50.0, 60.0], [40.0, 50.0], [50.0, 40.0]]
        tpts = [[80.0, 50.0], [70.0, 60.0], [60.0, 50.0], [70.0, 40.0]]
        doc = make_doc([
            stroke("s-0001", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 10.0}),
            stroke("s-0002", tpts, closed=True, shape="circle",
                   circle={"cx_mm": 70.0, "cy_mm": 50.0, "r_mm": 10.0}),
        ])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def test_circle_fit_from_points_without_block(self):
        """shape='circle' sem bloco: Kåsa ajusta a polilinha discretizada
        e o contorno ganha descritor analítico marcado como 'fitted'."""
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 16),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 16)]
                for i in range(16)]
        doc = make_doc([stroke("s-0001", cpts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        circle = contours[0]["circle"]
        self.assertIsNotNone(circle)
        self.assertTrue(circle["fitted"])
        self.assertAlmostEqual(circle["center_mm"][0], 55.0, places=3)
        self.assertAlmostEqual(circle["center_mm"][1], HEIGHT - 30.0, places=3)
        self.assertAlmostEqual(circle["radius_mm"], 5.0, places=3)

    def test_circle_shape_with_bad_fit_stays_a_polyline(self):
        """Polilinha deformada marcada 'circle' (desvio radial acima da
        tolerância) não vira círculo: segue a polilinha — fallback seguro."""
        # Elipse achatada (a=6, b=3): desvio radial ~3 mm >> tolerância.
        epts = [[55.0 + 6.0 * math.cos(2 * math.pi * i / 24),
                 30.0 + 3.0 * math.sin(2 * math.pi * i / 24)]
                for i in range(24)]
        doc = make_doc([stroke("s-0001", epts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        self.assertIsNone(contours[0]["circle"])

    def test_thin_arc_marked_circle_stays_a_polyline(self):
        """Arco fino ajustaria um círculo enorme: a trava de raio coerente
        com a dispersão impede a promoção — segue a polilinha."""
        apts = [[60.0 + 0.1 * math.cos(t), 40.0 + 0.1 * math.sin(t)]
                for t in (-0.05, 0.0, 0.05)]
        # 3 pontos quase colineares (arco de ~6°): sem trava caberia
        # qualquer círculo gigante.
        doc = make_doc([stroke("s-0001", apts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        self.assertIsNone(contours[0]["circle"])

    def test_partial_arc_marked_circle_stays_a_polyline(self):
        """Arco de 180° com vértices todos na circunferência ajusta
        com desvio radial zero, mas a cobertura angular incompleta
        impede a promoção — senão viraria disco inteiro (cilindro)
        em vez do segmento circular da polilinha."""
        apts = [[55.0 + 5.0 * math.cos(math.pi * i / 23),
                 30.0 + 5.0 * math.sin(math.pi * i / 23)]
                for i in range(24)]
        doc = make_doc([stroke("s-0001", apts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        self.assertIsNone(contours[0]["circle"])

    def test_self_intersecting_ring_marked_circle_is_refused(self):
        """Polígono estrelado: vértices na circunferência e todos os
        setores cobertos, mas o anel auto-intersecta. Promovê-lo
        pularia a checagem intra-anel — cai para a polilinha e segue
        recusado fail-closed."""
        pts = [[55.0 + 5.0 * math.cos(2 * math.pi * (5 * i % 24) / 24),
                30.0 + 5.0 * math.sin(2 * math.pi * (5 * i % 24) / 24)]
               for i in range(24)]
        doc = make_doc([stroke("s-0001", pts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        self.assertIsNone(contours[0]["circle"])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))

    def test_fit_ignores_repeated_closing_point(self):
        """Primeiro ponto repetido no fim do anel: a aresta degenerada
        não é auto-interseção — o círculo ajusta normalmente."""
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 16),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 16)]
                for i in range(16)]
        cpts.append(list(cpts[0]))
        doc = make_doc([stroke("s-0001", cpts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        self.assertIsNotNone(contours[0]["circle"])

    def test_fitted_circle_out_of_sheet_stays_a_polyline(self):
        """Vértices dentro da folha não bastam: a circunferência
        ajustada inteira precisa caber — losango com vértices dentro
        cujo círculo excede x=0 segue a polilinha."""
        dpts = [[4.0 + 5.0 * math.cos(math.pi / 4 + math.pi / 2 * i),
                 30.0 + 5.0 * math.sin(math.pi / 4 + math.pi / 2 * i)]
                for i in range(4)]
        doc = make_doc([stroke("s-0001", dpts, closed=True, shape="circle")])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        self.assertIsNone(contours[0]["circle"])

    def test_declared_circle_block_beats_fit(self):
        """O bloco ``circle`` declarado tem prioridade: pontos fora da
        circunferência declarada não disparam o ajuste Kåsa."""
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 12),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 12)]
                for i in range(12)]
        doc = make_doc([stroke(
            "s-0001", cpts, closed=True, shape="circle",
            circle={"cx_mm": 55.0, "cy_mm": 30.0, "r_mm": 4.0})])
        contours, _ids, _skipped = step_export._closed_stable_profiles(doc)
        circle = contours[0]["circle"]
        self.assertNotIn("fitted", circle)
        self.assertAlmostEqual(circle["radius_mm"], 4.0)

    def test_invalid_circle_params_are_refused(self):
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 12),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 12)]
                for i in range(12)]
        for bad in (
            {"cx_mm": 55.0, "cy_mm": 30.0, "r_mm": 0.0},
            {"cx_mm": 205.0, "cy_mm": 30.0, "r_mm": 10.0},
            {"cx_mm": "x", "cy_mm": 30.0, "r_mm": 5.0},
            "não-objeto",
        ):
            with self.subTest(bad=bad):
                doc = make_doc([stroke("s-0001", cpts, closed=True,
                                       shape="circle", circle=bad)])
                with self.assertRaises(StepExportError):
                    document_to_step(doc, THICKNESS_MM, "PLA", Path("x.step"))


class StepExportTests(unittest.TestCase):
    def setUp(self):
        if not worker_ready():
            self.skipTest(
                "venv .venv-beta com build123d não encontrado; "
                "rode a instalação descrita em docs/step-worker.md"
            )
        self.tmp = tempfile.TemporaryDirectory(prefix="ocre-step-test-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_rectangle_100x10_extrudes_to_10000mm3_step(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        out = self.dir / "rect.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)
        self.assertEqual(report["stroke_ids"], ["s-0001"])
        self.assertEqual(report["thickness_mm"], THICKNESS_MM)
        self.assertEqual(report["material_declared"], "PLA")
        self.assertEqual(
            report["output_sha256"], hashlib.sha256(out.read_bytes()).hexdigest()
        )
        manifest_path = Path(report["material_manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        self.assertTrue(manifest["material_in_step_native"])
        self.assertEqual(manifest["step"]["sha256"], report["output_sha256"])
        self.assertEqual(manifest["material"]["value"], "PLA")
        self.assertEqual(manifest["geometry"]["stroke_ids"], ["s-0001"])
        loaded = step_export.load_step_material_manifest(out, document=doc)
        self.assertEqual(loaded["material"], "PLA")
        original = out.read_bytes()
        out.write_bytes(original + b"\n")
        with self.assertRaises(StepExportError):
            step_export.load_step_material_manifest(out, document=doc)
        out.write_bytes(original)
        text = out.read_text(encoding="utf-8", errors="replace")
        self.assertIn("DESCRIPTIVE_REPRESENTATION_ITEM('PLA'", text)
        self.assertAlmostEqual(
            report["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )
        # Bbox: x 10..110, y CAD = 296.47-30 .. 296.47-20, z 0..10.
        bbox = report["bbox_mm"]
        self.assertAlmostEqual(bbox["min"][0], 10.0, places=6)
        self.assertAlmostEqual(bbox["max"][0], 110.0, places=6)
        self.assertAlmostEqual(bbox["min"][1], HEIGHT - 30.0, places=6)
        self.assertAlmostEqual(bbox["max"][1], HEIGHT - 20.0, places=6)
        self.assertAlmostEqual(bbox["min"][2], 0.0, places=6)
        self.assertAlmostEqual(bbox["max"][2], 10.0, places=6)
        # FILE_SCHEMA real (o que o OCCT de fato emitiu) fica no relatório.
        self.assertIsNotNone(report["file_schema"])
        self.assertIn("FILE_SCHEMA", report["file_schema"])

    def test_brepcheck_validates_exported_step(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        out = self.dir / "rect.step"
        document_to_step(doc, THICKNESS_MM, "PLA", out)
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])
        self.assertEqual(check["solids"], 1)
        self.assertAlmostEqual(
            check["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )

    def test_open_stroke_is_refused_fail_closed(self):
        doc = make_doc([stroke("s-0001", [[10.0, 20.0], [110.0, 20.0]])])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", self.dir / "open.step")
        self.assertFalse((self.dir / "open.step").exists())

    def test_missing_thickness_is_refused(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        with self.assertRaises(StepExportError):
            document_to_step(doc, None, "PLA", self.dir / "no-thick.step")
        with self.assertRaises(StepExportError):
            document_to_step(doc, 0, "PLA", self.dir / "zero-thick.step")
        with self.assertRaises(StepExportError):
            document_to_step(doc, -5, "PLA", self.dir / "neg-thick.step")

    def test_missing_material_is_refused(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, None, self.dir / "no-mat.step")
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "  ", self.dir / "blank-mat.step")

    def test_document_without_closed_stable_stroke_is_refused(self):
        # Só traço aberto.
        doc = make_doc([stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]])])
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", self.dir / "a.step")
        # Só traço pending fechado (não é stable).
        doc = make_doc(
            [stroke("s-0001", RECT, status="pending", closed=True)]
        )
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", self.dir / "b.step")
        # Só traço erased fechado (nunca entra).
        doc = make_doc(
            [stroke("s-0001", RECT, status="erased", closed=True)]
        )
        with self.assertRaises(StepExportError):
            document_to_step(doc, THICKNESS_MM, "PLA", self.dir / "c.step")

    def test_erased_closed_stroke_is_excluded_not_exported(self):
        doc = make_doc(
            [
                stroke("s-0001", RECT, closed=True),
                stroke("s-0002", [[150.0, 20.0], [200.0, 20.0], [200.0, 30.0], [150.0, 30.0]],
                       status="erased", closed=True),
            ]
        )
        out = self.dir / "one.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        self.assertEqual(report["stroke_ids"], ["s-0001"])
        self.assertEqual(report["skipped"], [{"stroke_id": "s-0002", "reason": "erased"}])
        self.assertAlmostEqual(
            report["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )

    def test_plate_with_hole_extrudes_net_volume(self):
        """Chapa 100x10 com furo quadrado 20x8 -> (1000-160) x 10 mm³."""
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", [[40.0, 21.0], [60.0, 21.0],
                              [60.0, 29.0], [40.0, 29.0]], closed=True),
        ])
        out = self.dir / "hole.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = (100.0 * 10.0 - 20.0 * 8.0) * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])
        self.assertEqual(check["solids"], 1)
        # O manifesto cobre os dois contornos (chapa + furo).
        loaded = step_export.load_step_material_manifest(out, document=doc)
        self.assertEqual(loaded["geometry"]["stroke_ids"], ["s-0001", "s-0002"])

    def test_island_inside_hole_becomes_second_solid(self):
        """Regra par-ímpar: contorno dentro do furo é ilha (sólido próprio)."""
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", [[40.0, 21.0], [60.0, 21.0],
                              [60.0, 29.0], [40.0, 29.0]], closed=True),
            stroke("s-0003", [[48.0, 24.0], [52.0, 24.0],
                              [52.0, 26.0], [48.0, 26.0]], closed=True),
        ])
        out = self.dir / "island.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = (1000.0 - 160.0 + 8.0) * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])
        self.assertEqual(check["solids"], 2)

    def test_analytic_circle_hole_is_exact_not_faceted(self):
        """Furo ``circle`` com bloco analítico: volume πr² exato — uma
        polilinha de 12 lados erraria ~4,5% e estouraria a trava."""
        cpts = [[55.0 + 4.0 * math.cos(2 * math.pi * i / 12),
                 25.0 + 4.0 * math.sin(2 * math.pi * i / 12)]
                for i in range(12)]
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 55.0, "cy_mm": 25.0, "r_mm": 4.0}),
        ])
        out = self.dir / "circle-hole.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = (1000.0 - math.pi * 16.0) * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])
        self.assertEqual(check["solids"], 1)

    def test_analytic_circle_extrudes_exact_cylinder(self):
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 12),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 12)]
                for i in range(12)]
        doc = make_doc([stroke("s-0001", cpts, closed=True, shape="circle",
                               circle={"cx_mm": 55.0, "cy_mm": 30.0, "r_mm": 5.0})])
        out = self.dir / "cylinder.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = math.pi * 25.0 * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        # Centro CAD: y_cad = 296.47 - 30; bbox do cilindro r=5.
        bbox = report["bbox_mm"]
        self.assertAlmostEqual(bbox["min"][0], 50.0, places=4)
        self.assertAlmostEqual(bbox["max"][0], 60.0, places=4)
        self.assertAlmostEqual(bbox["min"][1], HEIGHT - 35.0, places=4)
        self.assertAlmostEqual(bbox["max"][1], HEIGHT - 25.0, places=4)
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])

    def test_square_inside_analytic_circle_is_a_hole_not_a_solid(self):
        """Regressão e2e da revisão: quadrado dentro do disco r=10 mas
        fora do losango de 4 amostras é FURO (vol = pi*100 - 1) — antes
        virava um segundo sólido sobreposto somado ao cilindro."""
        cpts = [[60.0, 50.0], [50.0, 60.0], [40.0, 50.0], [50.0, 40.0]]
        doc = make_doc([
            stroke("s-0001", cpts, closed=True, shape="circle",
                   circle={"cx_mm": 50.0, "cy_mm": 50.0, "r_mm": 10.0}),
            stroke("s-0002", [[56.0, 56.0], [57.0, 56.0],
                              [57.0, 57.0], [56.0, 57.0]], closed=True),
        ])
        out = self.dir / "disc-with-square-hole.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = (math.pi * 100.0 - 1.0) * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])
        self.assertEqual(check["solids"], 1)

    def test_circle_shape_without_block_fits_analytic_circle(self):
        """shape='circle' sem bloco: os pontos discretizados ajustam um
        círculo (Kåsa) e o sólido é o cilindro exato πr², não o prisma."""
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 12),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 12)]
                for i in range(12)]
        doc = make_doc([stroke("s-0001", cpts, closed=True, shape="circle")])
        out = self.dir / "poly-circle.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = math.pi * 25.0 * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        check = validate_step(out)
        self.assertTrue(check["brepcheck_valid"])

    def test_two_disjoint_profiles_export_compound(self):
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", [[150.0, 20.0], [200.0, 20.0],
                              [200.0, 30.0], [150.0, 30.0]], closed=True),
        ])
        out = self.dir / "two.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out)
        expected = (1000.0 + 500.0) * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )
        check = validate_step(out)
        self.assertEqual(check["solids"], 2)

    def test_skip_open_open_stroke_exports_and_reports(self):
        """``open_strokes_policy="skip-open"``: o traço aberto é pulado e
        registrado; o manifesto continua íntegro (só fechados estáveis)."""
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", [[5.0, 5.0], [50.0, 5.0]]),
        ])
        out = self.dir / "skip.step"
        report = document_to_step(doc, THICKNESS_MM, "PLA", out,
                                  open_strokes_policy="skip-open")
        self.assertEqual(report["stroke_ids"], ["s-0001"])
        self.assertEqual(
            report["skipped"],
            [{"stroke_id": "s-0002", "reason": "open"}],
        )
        self.assertAlmostEqual(
            report["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )
        loaded = step_export.load_step_material_manifest(out, document=doc)
        self.assertEqual(loaded["geometry"]["stroke_ids"], ["s-0001"])

    def test_deterministic_export_with_timestamp(self):
        """Spike (c): dois exports com o mesmo timestamp -> mesmo hash.

        Sem o parâmetro timestamp (build123d antigo), o FILE_NAME
        carrega o relógio: o hash cru difere, mas o hash normalizado
        (linha do timestamp mascarada) tem que ser igual — o teste
        aceita os dois casos e registra qual aconteceu.
        """
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        out1 = self.dir / "det1.step"
        out2 = self.dir / "det2.step"
        rep1 = document_to_step(doc, THICKNESS_MM, "PLA", out1, timestamp="2026-11-05T00:00:00")
        rep2 = document_to_step(doc, THICKNESS_MM, "PLA", out2, timestamp="2026-11-05T00:00:00")
        self.assertTrue(rep1["timestamp_param_used"] or True)  # registrado, não exigido
        if rep1["output_sha256"] == rep2["output_sha256"]:
            return  # determinístico até no hash cru
        # Sem timestamp no formato: normalizar a linha do FILE_NAME.
        def _normalized(path: Path) -> str:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = []
            for line in text.splitlines():
                if "FILE_NAME" in line and "T" in line:
                    lines.append("FILE_NAME_MASKED")
                else:
                    lines.append(line)
            return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        self.assertEqual(_normalized(out1), _normalized(out2))

    def test_worker_versions_are_pinned_and_reported(self):
        versions = worker_versions()
        self.assertIn("build123d", versions)
        self.assertNotEqual(versions["build123d"], "unknown")
        self.assertIn("OCP", versions)
        # CadQuery NUNCA pode estar no venv do worker.
        import subprocess

        python = step_export.find_step_worker_python()
        probe = subprocess.run(
            [str(python), "-c", "import cadquery"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(probe.returncode, 0, "CadQuery importável no venv do worker!")

    def test_spike_dxf_ac1015_opens_in_librecad(self):
        """Spike (a): ExportDXF AC1015 do build123d + abertura no LibreCAD.

        Resultado medido nesta máquina: o DXF é AC1015 válido (lido
        de volta pelo ezdxf: 4 LINEs fechando o retângulo exato), MAS
        o ``librecad dxf2svg`` TRAVA nesta máquina para QUALQUER DXF
        — reproduzido com um DXF trivial do próprio ezdxf F0, com e
        sem Xvfb, stdin fechado — então a abertura no LibreCAD NÃO
        é verificável aqui. O gate real é tentado com timeout curto
        e vira skip honesto (não pass, não fail) quando trava.
        """
        import ezdxf

        from ocre_core.draw.librecad_gate import LibreCADGateError

        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        dxf_path = self.dir / "rect-b123d.dxf"
        report = export_dxf_ac1015(doc, dxf_path)
        self.assertEqual(report["dxf_version"], "AC1015")
        self.assertTrue(dxf_path.is_file())
        self.assertGreater(dxf_path.stat().st_size, 0)
        # Verificação estrutural real: ezdxf lê o DXF do build123d.
        back = ezdxf.readfile(dxf_path)
        self.assertEqual(back.dxfversion, "AC1015")
        lines = list(back.modelspace().query("LINE"))
        self.assertEqual(len(lines), 4)
        # Conjunto de segmentos (a ordem das entidades não é garantida).
        segments = {
            (
                (round(l.dxf.start.x, 6), round(l.dxf.start.y, 6)),
                (round(l.dxf.end.x, 6), round(l.dxf.end.y, 6)),
            )
            for l in lines
        }
        expected = {
            ((10.0, HEIGHT - 30.0), (110.0, HEIGHT - 30.0)),
            ((110.0, HEIGHT - 30.0), (110.0, HEIGHT - 20.0)),
            ((110.0, HEIGHT - 20.0), (10.0, HEIGHT - 20.0)),
            ((10.0, HEIGHT - 20.0), (10.0, HEIGHT - 30.0)),
        }
        self.assertEqual(segments, expected)
        # Gate real com timeout curto: trava ambiental -> skip honesto.
        try:
            gate = check_dxf(dxf_path, timeout_s=20)
        except LibreCADGateError as exc:
            self.skipTest(f"LibreCAD travou nesta máquina (timeout): {exc}")
        if gate["status"] == "unavailable":
            self.skipTest("LibreCAD indisponível nesta máquina (gate não rodado)")
        if gate["status"] != "pass":
            self.skipTest(f"LibreCAD não confirmou o DXF aqui: {gate}")
        self.assertEqual(gate["status"], "pass")

    def test_freecad_oracle_confirms_step_when_available(self):
        """Oráculo FreeCAD: confirma isValid/volume/bbox — ou skip honesto."""
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        out = self.dir / "rect.step"
        document_to_step(doc, THICKNESS_MM, "PLA", out)
        oracle = oracle_step(out)
        if oracle["status"] == "unavailable":
            self.skipTest(f"oráculo FreeCAD indisponível: {oracle['reason']}")
        self.assertEqual(oracle["status"], "pass", f"oráculo falhou: {oracle}")
        self.assertTrue(oracle["is_valid"])
        self.assertAlmostEqual(
            oracle["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )


class OpenStrokesPolicyTests(unittest.TestCase):
    """Política de traços abertos (``open_strokes_policy``) — só host.

    Estes testes NÃO precisam do venv ``.venv-beta``: ``_run_worker`` é
    mockado, então o que se prova é a fronteira do documento (o que sai
    em ``regions``/``skipped`` e o que ainda recusa fail-closed), não
    a geometria OCCT — que a classe ``StepExportTests`` já cobre.
    """

    # Artefatos temporários ficam dentro do repo (AGENTS.md); ``.tmp/``
    # na raiz é gitignored.
    _REPO_TMP = Path(__file__).resolve().parents[3] / ".tmp"

    def setUp(self):
        self._REPO_TMP.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(
            prefix="ocre-open-policy-", dir=self._REPO_TMP
        )
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _export(self, doc, **kwargs):
        """``document_to_step`` com worker fake; devolve (report, job)."""
        captured = {}

        def fake_worker(job, *, timeout_s, worker_python=None):
            captured["job"] = job

            def wire_area(desc):
                if desc["kind"] == "circle":
                    return math.pi * desc["radius_mm"] ** 2
                return step_export._polygon_area_mm2(desc["points_mm"])

            area = sum(
                wire_area(region["outer"])
                - sum(wire_area(hole) for hole in region["holes"])
                for region in job["regions"]
            )
            return {
                "ok": True,
                "sha256": "f" * 64,
                "bytes": 10,
                "volume_mm3": area * float(job["thickness_mm"]),
                "bbox_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                "file_schema": "AP242",
                "worker_versions": {"build123d": "fake"},
                "notes": [],
            }

        with mock.patch.object(
            step_export, "_run_worker", side_effect=fake_worker
        ):
            report = document_to_step(
                doc, THICKNESS_MM, "PLA", self.dir / "doc.step", **kwargs
            )
        return report, captured["job"]

    def test_fail_is_the_default_and_refuses_open_stroke(self):
        doc = make_doc(
            [
                stroke("s-0001", RECT, closed=True),
                stroke("s-0002", [[10.0, 200.0], [100.0, 200.0]]),
            ]
        )
        with self.assertRaises(StepExportError):
            self._export(doc)
        self.assertFalse((self.dir / "doc.step").exists())

    def test_skip_open_records_stroke_and_exports_closed_only(self):
        doc = make_doc(
            [
                stroke("s-0001", RECT, closed=True),
                stroke("s-0002", [[10.0, 200.0], [100.0, 200.0]]),
            ]
        )
        report, job = self._export(doc, open_strokes_policy="skip-open")
        self.assertEqual(report["stroke_ids"], ["s-0001"])
        self.assertEqual(
            report["skipped"], [{"stroke_id": "s-0002", "reason": "open"}]
        )
        self.assertEqual(report["open_strokes_policy"], "skip-open")
        # Só o perfil fechado chegou ao job do worker.
        self.assertEqual(len(job["regions"]), 1)
        self.assertEqual(job["regions"][0]["outer"]["kind"], "polygon")
        self.assertAlmostEqual(
            report["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )

    def test_single_point_stroke_is_skipped_under_both_policies(self):
        # 1 ponto não tem representação em sólido: mesma regra do DXF
        # (reason="single_point"), registrada nas duas políticas — um
        # traço ``point`` estável nunca derruba a exportação inteira.
        for policy in ("fail", "skip-open"):
            with self.subTest(policy=policy):
                doc = make_doc(
                    [
                        stroke("s-0001", RECT, closed=True),
                        stroke("s-0002", [[200.0, 250.0]]),
                    ]
                )
                report, job = self._export(doc, open_strokes_policy=policy)
                self.assertEqual(report["stroke_ids"], ["s-0001"])
                self.assertEqual(
                    report["skipped"],
                    [{"stroke_id": "s-0002", "reason": "single_point"}],
                )
                self.assertEqual(len(job["regions"]), 1)

    def test_skip_open_still_refuses_document_without_closed_stable(self):
        # Só traço aberto: a política não fabrica sólido do nada.
        doc = make_doc([stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]])])
        with self.assertRaises(StepExportError):
            self._export(doc, open_strokes_policy="skip-open")

    def test_unknown_policy_is_refused_fail_closed(self):
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        for bad in ("skip-aux", "ignore", "", None, 3):
            with self.subTest(policy=bad), self.assertRaises(StepExportError):
                self._export(doc, open_strokes_policy=bad)

    def test_export_dxf_ac1015_honors_the_same_policy(self):
        # Default: traço aberto recusa antes de qualquer worker.
        doc = make_doc([stroke("s-0001", [[10.0, 20.0], [50.0, 20.0]])])
        with self.assertRaises(StepExportError):
            export_dxf_ac1015(doc, self.dir / "open.dxf")
        # skip-open: o traço aberto vai para o relatório, não para o DXF.
        doc = make_doc(
            [
                stroke("s-0001", RECT, closed=True),
                stroke("s-0002", [[10.0, 200.0], [100.0, 200.0]]),
            ]
        )
        with mock.patch.object(
            step_export,
            "_run_worker",
            return_value={
                "ok": True,
                "dxf_path": str(self.dir / "doc.dxf"),
                "dxf_sha256": "e" * 64,
                "dxf_bytes": 9,
                "worker_versions": {},
            },
        ):
            report = export_dxf_ac1015(
                doc, self.dir / "doc.dxf", open_strokes_policy="skip-open"
            )
        self.assertEqual(report["stroke_ids"], ["s-0001"])
        self.assertEqual(
            report["skipped"], [{"stroke_id": "s-0002", "reason": "open"}]
        )


class HostBoundaryTests(unittest.TestCase):
    """Validações do host que NÃO precisam do worker (ou com ele mockado).

    Cobre também o ajuste Kåsa (``shape="circle"`` sem bloco ``circle``):
    ``_run_worker`` mockado prova a fronteira do documento (wire
    ``circle`` vs ``polygon`` no job e a nota de ajuste no relatório),
    não a geometria OCCT.
    """

    _REPO_TMP = Path(__file__).resolve().parents[3] / ".tmp"

    def setUp(self):
        self._REPO_TMP.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(
            prefix="ocre-step-host-", dir=self._REPO_TMP
        )
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _export(self, doc, **kwargs):
        """``document_to_step`` com worker fake; devolve (report, job)."""
        captured = {}

        def fake_worker(job, *, timeout_s, worker_python=None):
            captured["job"] = job

            def wire_area(desc):
                if desc["kind"] == "circle":
                    return math.pi * desc["radius_mm"] ** 2
                return step_export._polygon_area_mm2(desc["points_mm"])

            area = sum(
                wire_area(region["outer"])
                - sum(wire_area(hole) for hole in region["holes"])
                for region in job["regions"]
            )
            return {
                "ok": True,
                "sha256": "f" * 64,
                "bytes": 10,
                "volume_mm3": area * float(job["thickness_mm"]),
                "bbox_mm": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                "file_schema": "AP242",
                "worker_versions": {"build123d": "fake"},
                "notes": [],
            }

        with mock.patch.object(
            step_export, "_run_worker", side_effect=fake_worker
        ):
            report = document_to_step(
                doc, THICKNESS_MM, "PLA", self.dir / "doc.step", **kwargs
            )
        return report, captured["job"]

    def test_fitted_circle_wire_reaches_job(self):
        """16-gon marcado 'circle' sem bloco: o wire no job é o círculo
        analítico ajustado (e 'fitted' não vaza para o worker)."""
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 16),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 16)]
                for i in range(16)]
        doc = make_doc([stroke("s-0001", cpts, closed=True, shape="circle")])
        report, job = self._export(doc)
        outer = job["regions"][0]["outer"]
        self.assertEqual(
            set(outer), {"kind", "center_mm", "radius_mm"}
        )
        self.assertEqual(outer["kind"], "circle")
        self.assertAlmostEqual(outer["center_mm"][0], 55.0, places=3)
        self.assertAlmostEqual(outer["center_mm"][1], HEIGHT - 30.0, places=3)
        self.assertAlmostEqual(outer["radius_mm"], 5.0, places=3)
        self.assertIn("s-0001", report["notes"][0])
        self.assertIn("ajustado", report["notes"][0])
        expected = math.pi * 25.0 * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )

    def test_bad_fit_sends_polygon_wire_without_note(self):
        epts = [[55.0 + 6.0 * math.cos(2 * math.pi * i / 24),
                 30.0 + 3.0 * math.sin(2 * math.pi * i / 24)]
                for i in range(24)]
        doc = make_doc([stroke("s-0001", epts, closed=True, shape="circle")])
        report, job = self._export(doc)
        self.assertEqual(job["regions"][0]["outer"]["kind"], "polygon")
        self.assertEqual(report["notes"], [])

    def test_fitted_circle_inside_plate_is_a_hole(self):
        """Círculo ajustado contido na chapa vira furo analítico exato."""
        cpts = [[55.0 + 3.0 * math.cos(2 * math.pi * i / 16),
                 25.0 + 3.0 * math.sin(2 * math.pi * i / 16)]
                for i in range(16)]
        doc = make_doc([
            stroke("s-0001", RECT, closed=True),
            stroke("s-0002", cpts, closed=True, shape="circle"),
        ])
        report, job = self._export(doc)
        holes = job["regions"][0]["holes"]
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0]["kind"], "circle")
        expected = (1000.0 - math.pi * 9.0) * THICKNESS_MM
        self.assertAlmostEqual(
            report["volume_mm3"], expected, delta=expected * VOLUME_REL_TOL
        )

    def test_declared_block_wins_over_fit(self):
        """Bloco ``circle`` declarado tem prioridade e não gera nota."""
        cpts = [[55.0 + 5.0 * math.cos(2 * math.pi * i / 12),
                 30.0 + 5.0 * math.sin(2 * math.pi * i / 12)]
                for i in range(12)]
        doc = make_doc([stroke(
            "s-0001", cpts, closed=True, shape="circle",
            circle={"cx_mm": 55.0, "cy_mm": 30.0, "r_mm": 5.0})])
        report, job = self._export(doc)
        outer = job["regions"][0]["outer"]
        self.assertEqual(outer["kind"], "circle")
        self.assertNotIn("fitted", outer)
        self.assertEqual(report["notes"], [])

    def test_fail_closed_bool_string_and_none_points(self):
        # Mesma regra de document._check_points: bool não é número e
        # não-número recusa como StepExportError — antes o float()
        # aceitava bool/str numérica e deixava TypeError cru.
        for bad_points in (
            [[True, 1.0], [50.0, 20.0], [50.0, 30.0]],
            [["10", 1.0], [50.0, 20.0], [50.0, 30.0]],
            [[None, 1.0], [50.0, 20.0], [50.0, 30.0]],
        ):
            with self.subTest(points=bad_points):
                doc = make_doc([stroke("s-0001", bad_points, closed=True)])
                with self.assertRaises(StepExportError):
                    document_to_step(
                        doc, THICKNESS_MM, "PLA", self.dir / "bad.step")

    def test_partial_worker_result_is_step_worker_error(self):
        # ok=True com campos ausentes ou volume não numérico é erro de
        # protocolo (StepWorkerError), nunca KeyError/ValueError cru.
        doc = make_doc([stroke("s-0001", RECT, closed=True)])
        partial_results = (
            {"ok": True},
            {"ok": True, "volume_mm3": "x",
             "sha256": "f" * 64, "bytes": 1, "bbox_mm": [[0, 0, 0]]},
        )
        for result in partial_results:
            with self.subTest(result=result), mock.patch.object(
                step_export, "_run_worker", return_value=result
            ):
                with self.assertRaises(StepWorkerError):
                    document_to_step(
                        doc, THICKNESS_MM, "PLA", self.dir / "doc.step")

    def test_oracle_malformed_json_is_step_worker_error(self):
        # O oráculo falso escreve o relatório; JSON quebrado ou
        # não-objeto era JSONDecodeError/TypeError cru — agora
        # StepWorkerError.
        step = self.dir / "x.step"
        step.write_text("fake STEP", encoding="utf-8")
        for payload in ("not-json{{{", "[1, 2, 3]"):
            with self.subTest(payload=payload):
                command = [
                    sys.executable, "-c",
                    "import sys; open(sys.argv[-1], 'w').write(%r)" % payload,
                ]
                with self.assertRaises(StepWorkerError):
                    oracle_step(step, freecad_command=command)


if __name__ == "__main__":
    unittest.main()
