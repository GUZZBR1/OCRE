"""Tests for the F0 Draw CLI (ocre_core/draw/cli.py).

Uses the fake vectorize fixture (never the real Draw vectorize) and a
minimal PNG placeholder (the fixture never reads it). The LibreCAD
gate is exercised for real when the ``librecad`` binary exists on the
machine (it does since 2026-11-05; the gate converts the exported DXF
with ``dxf2pdf``) and as "unavailable" when it does not — the CLI
test below is environment-aware and requires exit 0 either way.
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.draw.png_helper import write_png
from ocre_core.draw.cli import main
from ocre_core.draw.fingerprint import parse_encoded
from ocre_core.draw.sidecar import validate_draw_sidecar
from ocre_core.draw.validator_draw import validate_draw_document
from ocre_core.draw.vectorize_runner import sha256_file

HERE = Path(__file__).resolve().parent
FAKE_VECTORIZE = HERE / "fixtures" / "fake_vectorize.py"
COMMIT = "a" * 40


def _run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class CliExportTests(unittest.TestCase):
    def _export(self, out_dir: Path, extra: list[str] | None = None,
                canvas: tuple[int, int] | None = (850, 1200)) -> tuple[int, str, dict]:
        png = out_dir / "canvas.png"
        if canvas is not None:
            write_png(png, *canvas)
        argv = [
            "export",
            "--png", str(png),
            "--vectorize-script", str(FAKE_VECTORIZE),
            "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
            "--out-dir", str(out_dir / "out"),
            "--origin", "upstream-canvas-png",
            "--draw-repo", "santleme/draw",
            "--draw-commit", COMMIT,
        ] + (extra or [])
        code, out = _run_cli(argv)
        summary = json.loads(out) if code == 0 else {}
        return code, out, summary

    def test_clean_flag_reaches_vectorize_and_defaults_off(self):
        from ocre_core.draw.vectorize_runner import VectorizeError

        for clean in (False, True):
            with self.subTest(clean=clean), tempfile.TemporaryDirectory() as tmp:
                argv = ["--clean"] if clean else []
                with mock.patch(
                    "ocre_core.draw.vectorize_runner.run_vectorize",
                    side_effect=VectorizeError("stop after observing call"),
                ) as run:
                    code, _out, _summary = self._export(Path(tmp), argv)
                self.assertEqual(code, 1)
                self.assertEqual(run.call_args.kwargs["clean"], clean)

    def test_export_writes_document_dxf_sidecar_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, _out, summary = self._export(tmp_path)
            self.assertEqual(code, 0, _out)
            out_dir = tmp_path / "out"
            for name in ("document.json", "document.dxf", "sidecar.json", "fingerprint.json"):
                self.assertTrue((out_dir / name).is_file(), name)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["librecad_gate"], "not-run")

            doc = json.loads((out_dir / "document.json").read_text(encoding="utf-8"))
            report = validate_draw_document(doc)
            self.assertTrue(report.valid, report.as_dict())
            # Fake vectorize: rectangle + polyline + circle + line +
            # point -> 5 strokes, all stabilized; the circle arrives
            # closed with shape="circle"; the point is a 1-pt stroke.
            self.assertEqual(len(doc["strokes"]), 5)
            self.assertTrue(all(s["status"] == "stable" for s in doc["strokes"]))
            circle = next(s for s in doc["strokes"] if s.get("shape") == "circle")
            self.assertTrue(circle["closed"])
            self.assertEqual(len(circle["points_mm"]), 64)
            # O fit analítico sobe ao documento mestre (Fase 2 o usa no
            # STEP analítico); radial_rmse_px fica fora — o schema exige
            # exatamente cx_mm/cy_mm/r_mm.
            self.assertEqual(
                circle["circle"],
                {"cx_mm": 150.0, "cy_mm": 150.0, "r_mm": 25.0})
            point_stroke = next(
                s for s in doc["strokes"] if len(s["points_mm"]) == 1)
            self.assertFalse(point_stroke["closed"])
            self.assertNotIn("shape", point_stroke)
            self.assertEqual(doc["source"]["origin"], "upstream-canvas-png")
            self.assertEqual(doc["source"]["commit"], COMMIT)

            sidecar = json.loads((out_dir / "sidecar.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_draw_sidecar(sidecar), [])
            self.assertEqual(sidecar["export_parameters"]["vectorize"]["skipped_primitive_types"], [])
            fidelity = sidecar["export_parameters"]["vectorize"]["summary"]
            self.assertEqual(fidelity["polilinhas"], 5)
            self.assertIn("distancias", fidelity)
            self.assertNotIn("svg", fidelity)  # chaves voláteis ficam fora
            self.assertEqual(sidecar["document_sha256"], summary["document_sha256"])
            self.assertEqual(sidecar["fingerprint"]["digest"], summary["fingerprint_digest"])
            self.assertEqual(sidecar["draw_source"], {"repo": "santleme/draw", "commit": COMMIT})
            self.assertEqual(sidecar["derived_artifacts"][0]["sha256"], summary["dxf_sha256"])

            fp = json.loads((out_dir / "fingerprint.json").read_text(encoding="utf-8"))
            self.assertEqual(fp["algorithm"], "DRAW-AF/1")
            self.assertEqual(fp["digest"], summary["fingerprint_digest"])
            records = parse_encoded(bytes.fromhex(fp["encoded_hex"]))
            self.assertEqual([r[0] for r in records], list(range(1, 11)))
            # The fingerprinted export_parameters are the sidecar's.
            self.assertEqual(
                fp["inputs"]["export_parameters"], sidecar["export_parameters"])
            self.assertEqual(
                fp["inputs"]["export_parameters"]["vectorize"]["primitives_without_points"], 0)

            dxf_bytes = (out_dir / "document.dxf").read_bytes()
            self.assertEqual(
                hashlib.sha256(dxf_bytes).hexdigest(), summary["dxf_sha256"])
            self.assertIn(b"DRAW_STROKES", dxf_bytes)

    def test_export_with_librecad_check_reports_unavailable_without_failing(self):
        # Ambiente-consciente: com o binário presente o gate roda de
        # verdade (dxf2pdf) e deve passar; sem ele, "unavailable".
        # Nos dois casos a exportação não pode falhar (exit 0).
        from ocre_core.draw.librecad_gate import librecad_available

        expected = "pass" if librecad_available() else "unavailable"
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, summary = self._export(Path(tmp), ["--librecad-check"])
            self.assertEqual(code, 0, _out)
            self.assertEqual(summary["librecad_gate"], expected)

    def test_export_rejects_wrong_vectorize_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            png = write_png(tmp_path / "canvas.png")
            code, out = _run_cli([
                "export", "--png", str(png),
                "--vectorize-script", str(FAKE_VECTORIZE),
                "--vectorize-sha256", "0" * 64,
                "--out-dir", str(tmp_path / "out"),
                "--origin", "upstream-canvas-png",
                "--draw-repo", "santleme/draw",
                "--draw-commit", COMMIT,
            ])
            self.assertNotEqual(code, 0)
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_requires_full_40_hex_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            png = write_png(tmp_path / "canvas.png")
            code, _out = _run_cli([
                "export", "--png", str(png),
                "--vectorize-script", str(FAKE_VECTORIZE),
                "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
                "--out-dir", str(tmp_path / "out"),
                "--origin", "upstream-canvas-png",
                "--draw-repo", "santleme/draw",
                "--draw-commit", "8a17115",
            ])
            self.assertNotEqual(code, 0)

    def test_export_with_stamps_records_temporal_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stamps = tmp_path / "canvas_stamps.npz"
            stamps.write_bytes(b"npz fake")
            code, out, summary = self._export(
                tmp_path, ["--stamps", str(stamps)])
            self.assertEqual(code, 0, out)
            doc = json.loads(
                (tmp_path / "out" / "document.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["order_kind"], "temporal-stamps")
            for stroke in doc["strokes"]:
                self.assertIn("t_start_ms", stroke)
                self.assertIn("t_end_ms", stroke)
            sidecar = json.loads(
                (tmp_path / "out" / "sidecar.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_draw_sidecar(sidecar), [])
            vec = sidecar["export_parameters"]["vectorize"]
            self.assertEqual(vec["stamps_sha256"], sha256_file(stamps))
            sources = {a["artifact_id"]: a for a in sidecar["source_artifacts"]}
            self.assertEqual(
                sources["canvas-stamps-1"]["artifact_kind"], "stamps_npz")
            self.assertEqual(
                sources["canvas-stamps-1"]["sha256"], sha256_file(stamps))
            fp = json.loads(
                (tmp_path / "out" / "fingerprint.json").read_text(encoding="utf-8"))
            consumed = {
                a["artifact_id"] for a in fp["inputs"]["consumed_artifacts"]}
            self.assertIn("canvas-stamps-1", consumed)

    def test_export_with_stamps_and_conflicting_order_kind_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stamps = tmp_path / "canvas_stamps.npz"
            stamps.write_bytes(b"npz fake")
            err = io.StringIO()
            with redirect_stderr(err):
                code, _out, _summary = self._export(
                    tmp_path,
                    ["--stamps", str(stamps),
                     "--order-kind", "reading-centroid"])
            self.assertEqual(code, 2)
            self.assertIn("temporal-stamps", err.getvalue())
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_with_missing_stamps_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, _out, _summary = self._export(
                tmp_path, ["--stamps", str(tmp_path / "ausente.npz")])
            self.assertEqual(code, 2)
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_refuses_png_that_is_not_a_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            png = tmp_path / "canvas.png"
            png.write_bytes(b"not a png at all")
            code, _out = _run_cli([
                "export", "--png", str(png),
                "--vectorize-script", str(FAKE_VECTORIZE),
                "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
                "--out-dir", str(tmp_path / "out"),
                "--origin", "upstream-canvas-png",
                "--draw-repo", "santleme/draw",
                "--draw-commit", COMMIT,
            ])
            self.assertEqual(code, 2)
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_rejects_non_finite_or_non_positive_timeout(self):
        # nan/inf chegavam a subprocess.run(timeout=...) e crashavam.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            png = tmp_path / "canvas.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            for bad in ("nan", "inf", "-inf", "0", "-5"):
                with self.subTest(timeout_s=bad):
                    code, _out = _run_cli([
                        "export", "--png", str(png),
                        "--vectorize-script", str(FAKE_VECTORIZE),
                        "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
                        "--out-dir", str(tmp_path / "out"),
                        "--origin", "upstream-canvas-png",
                        "--draw-repo", "santleme/draw",
                        "--draw-commit", COMMIT,
                        f"--timeout-s={bad}",
                    ])
                    self.assertEqual(code, 2)
                    self.assertFalse(
                        (tmp_path / "out" / "document.json").exists())

    def test_export_requires_explicit_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            png = tmp_path / "canvas.png"
            png.write_bytes(b"not a png at all")
            code, _out, _summary = self._export(tmp_path, canvas=None)
            self.assertEqual(code, 2)
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_refuses_png_with_wrong_canvas_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            err = io.StringIO()
            with redirect_stderr(err):
                code, _out, _summary = self._export(
                    tmp_path, canvas=(1200, 850))  # paisagem!
            self.assertEqual(code, 2)
            self.assertIn("1200x850", err.getvalue())

    def test_max_p95_px_gate_blocks_and_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # fake reporta p95 1.1 e 1.3 px -> limite 1.0 bloqueia.
            err = io.StringIO()
            with redirect_stderr(err):
                code, _out, _summary = self._export(
                    tmp_path, ["--max-p95-px", "1.0"])
            self.assertEqual(code, 1)
            self.assertIn("p95", err.getvalue())
            self.assertFalse((tmp_path / "out" / "document.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, out, _summary = self._export(tmp_path, ["--max-p95-px", "2.0"])
            self.assertEqual(code, 0, out)
            sidecar = json.loads(
                (tmp_path / "out" / "sidecar.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sidecar["export_parameters"]["vectorize"]["max_p95_px"], 2.0)

    def test_max_p95_px_rejects_non_finite_and_preserves_package(self):
        # float() do argparse aceita nan/inf: um limite nan nunca
        # bloquearia o gate e ainda iria para export_parameters — o
        # fingerprint recusa não-finito DEPOIS do DXF promovido. O
        # argumento é recusado antes de qualquer escrita, então uma
        # reexportação inválida preserva o pacote anterior byte a byte.
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(limit=bad), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                code, out, _summary = self._export(tmp_path)
                self.assertEqual(code, 0, out)
                out_dir = tmp_path / "out"
                dxf_before = (out_dir / "document.dxf").read_bytes()
                sidecar_before = (out_dir / "sidecar.json").read_bytes()

                err = io.StringIO()
                with redirect_stderr(err):
                    code, _out, _ = self._export(
                        tmp_path, [f"--max-p95-px={bad}"])
                self.assertEqual(code, 2, err.getvalue())
                self.assertIn("finite", err.getvalue())
                self.assertEqual(
                    (out_dir / "document.dxf").read_bytes(), dxf_before)
                self.assertEqual(
                    (out_dir / "sidecar.json").read_bytes(), sidecar_before)
                self.assertFalse((out_dir / ".document.dxf.tmp").exists())

    def test_export_refuses_non_finite_vectorizer_summary(self):
        # json.loads aceita NaN literal: um summary corrompido chegaria
        # ao fingerprint (que recusa) após o DXF promovido — a checagem
        # é feita antes de qualquer artefato.
        def fake_run(*_a, **_k):
            return {
                "polylines": [{
                    "points_mm": [[10.0, 10.0], [50.0, 50.0]],
                    "closed": False, "type": "polyline"}],
                "summary": {
                    "polilinhas": 1,
                    "distancias": {"ink2vec": {"p95_px": float("nan")}},
                },
                "script_sha256": "a" * 64,
                "primitives_total": 1,
                "polylines_total": 1,
                "primitives_without_points": 0,
                "skipped_primitive_types": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            err = io.StringIO()
            with redirect_stderr(err), mock.patch(
                "ocre_core.draw.vectorize_runner.run_vectorize",
                side_effect=fake_run,
            ):
                code, _out, _summary = self._export(tmp_path)
            self.assertEqual(code, 1, err.getvalue())
            self.assertIn("non-finite", err.getvalue())
            out_dir = tmp_path / "out"
            self.assertFalse((out_dir / "document.dxf").exists())
            self.assertFalse((out_dir / "document.json").exists())
            self.assertFalse((out_dir / ".document.dxf.tmp").exists())

    def test_export_requires_explicit_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            png = write_png(tmp_path / "canvas.png")
            code, _out = _run_cli([
                "export", "--png", str(png),
                "--vectorize-script", str(FAKE_VECTORIZE),
                "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
                "--out-dir", str(tmp_path / "out"),
                "--draw-repo", "santleme/draw",
                "--draw-commit", COMMIT,
            ])
            self.assertNotEqual(code, 0)  # argparse: --origin is required


class CliValidateTests(unittest.TestCase):
    def test_validate_valid_document_exits_zero(self):
        from ocre_core.draw.document import add_stroke, dumps, new_document

        doc = new_document(
            {"repo": "santleme/draw", "commit": COMMIT, "origin": "upstream-vectorize"})
        add_stroke(doc, [[10.0, 10.0], [20.0, 10.0]])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.json"
            path.write_text(dumps(doc), encoding="utf-8")
            code, out = _run_cli(["validate", "--document", str(path)])
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["status"], "valid")

    def test_validate_invalid_document_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.json"
            path.write_text(json.dumps({"schema": "draw.paper.v1"}), encoding="utf-8")
            code, out = _run_cli(["validate", "--document", str(path)])
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out)["status"], "invalid")

    def test_validate_missing_file_exits_nonzero(self):
        code, _out = _run_cli(["validate", "--document", "/nonexistent/doc.json"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
