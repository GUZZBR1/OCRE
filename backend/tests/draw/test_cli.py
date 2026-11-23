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
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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
    def _export(self, out_dir: Path, extra: list[str] | None = None) -> tuple[int, str, dict]:
        png = out_dir / "canvas.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
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
            # Fake vectorize: rectangle + polyline + line -> 3 strokes,
            # all stabilized; the circle has no points and is skipped.
            self.assertEqual(len(doc["strokes"]), 3)
            self.assertTrue(all(s["status"] == "stable" for s in doc["strokes"]))
            self.assertEqual(doc["source"]["origin"], "upstream-canvas-png")
            self.assertEqual(doc["source"]["commit"], COMMIT)

            sidecar = json.loads((out_dir / "sidecar.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_draw_sidecar(sidecar), [])
            self.assertEqual(sidecar["export_parameters"]["vectorize"]["skipped_primitive_types"], ["circle"])
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
                fp["inputs"]["export_parameters"]["vectorize"]["primitives_without_points"], 1)

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
            png = tmp_path / "canvas.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
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
            png = tmp_path / "canvas.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
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
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
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
