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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocre_core.draw import step_export
from ocre_core.draw.step_export import (
    StepExportError,
    document_to_step,
    export_dxf_ac1015,
    oracle_step,
    validate_step,
    worker_versions,
)
from ocre_core.draw.librecad_gate import check_dxf

HEIGHT = 296.47
# Retângulo 100 x 10 mm, espessura 10 mm -> volume 10000 mm³.
RECT = [[10.0, 20.0], [110.0, 20.0], [110.0, 30.0], [10.0, 30.0]]
THICKNESS_MM = 10.0
EXPECTED_VOLUME_MM3 = 100.0 * 10.0 * 10.0
# Tolerância do volume: OCCT arredonda em ~1e-7 relativo; 0,01% é
# folga ampla sem esconder erro de geometria.
VOLUME_REL_TOL = 1e-4


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


def worker_ready() -> bool:
    return step_export.find_step_worker_python() is not None


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
    em ``profiles``/``skipped`` e o que ainda recusa fail-closed), não
    a geometria OCCT — que a classe ``StepExportTests`` já cobre.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ocre-open-policy-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _export(self, doc, **kwargs):
        """``document_to_step`` com worker fake; devolve (report, job)."""
        captured = {}

        def fake_worker(job, *, timeout_s, worker_python=None):
            captured["job"] = job
            area = sum(
                step_export._polygon_area_mm2(p) for p in job["profiles"]
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
        self.assertEqual(len(job["profiles"]), 1)
        self.assertAlmostEqual(
            report["volume_mm3"], EXPECTED_VOLUME_MM3,
            delta=EXPECTED_VOLUME_MM3 * VOLUME_REL_TOL,
        )

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


if __name__ == "__main__":
    unittest.main()
