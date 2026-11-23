"""Tests for the configurable paper policy (decision D3).

The paper policy lives in ocre_core/draw/paper.py: A4 is the default,
the registry mirrors the draw-core config.py formats x orientations,
document/validator/DXF/CLI all read it from there, and the operator
can switch it with --paper or derive it from Draw's config.py with
--draw-config. There is no end-user configuration: these are operator
flags for the agent, so the measurement standard is easy to change
when Draw changes.

A3 below is a test-only PaperSpec (297 x 420 mm, canvas 1200 x 1696 px,
scale 0.2475 mm/px): it is intentionally NOT the registered A3 spec
(which is 849 x 1200 px) — it exercises a spec object passed directly,
independent of the registry.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.draw.png_helper import write_png
from ocre_core.draw.cli import main
from ocre_core.draw.document import add_stroke, new_document, stabilize_stroke
from ocre_core.draw.dxf_export import build_dxf
from ocre_core.draw.paper import (
    DEFAULT_PAPER,
    DEFAULT_SPEC,
    PaperSpec,
    load_draw_config,
    paper_size_mm,
    resolve_paper,
)
from ocre_core.draw.validator_draw import validate_draw_document
from ocre_core.draw.vectorize_runner import sha256_file

SOURCE = {"repo": "santleme/draw", "commit": "8a17115", "origin": "upstream-vectorize"}
COMMIT = "a" * 40
HERE = Path(__file__).resolve().parent
FAKE_VECTORIZE = HERE / "fixtures" / "fake_vectorize.py"

# Test-only A3 spec: scale 297/1200 = 0.2475 mm/px, so the effective
# height (419.76 mm) is exactly scale * 1696 px.
A3 = PaperSpec(
    format="A3",
    width_mm=297.0,
    height_mm=420.0,
    effective_width_mm=297.0,
    effective_height_mm=419.76,
    canvas_width_px=1200,
    canvas_height_px=1696,
)


class DefaultPaperTests(unittest.TestCase):
    def test_default_is_a4_with_the_draw_values(self):
        self.assertEqual(DEFAULT_PAPER, "A4")
        self.assertEqual(DEFAULT_SPEC.format, "A4")
        self.assertEqual(DEFAULT_SPEC.width_mm, 210.0)
        self.assertEqual(DEFAULT_SPEC.height_mm, 297.0)
        self.assertEqual(DEFAULT_SPEC.effective_width_mm, 210.0)
        self.assertEqual(DEFAULT_SPEC.effective_height_mm, 296.47)
        self.assertEqual(DEFAULT_SPEC.canvas_width_px, 850)
        self.assertEqual(DEFAULT_SPEC.canvas_height_px, 1200)
        self.assertAlmostEqual(DEFAULT_SPEC.scale_mm_per_px, 210.0 / 850.0)

    def test_resolve_defaults_to_a4(self):
        self.assertIs(resolve_paper(None), DEFAULT_SPEC)
        self.assertIs(resolve_paper("A4"), DEFAULT_SPEC)
        self.assertIs(resolve_paper(DEFAULT_SPEC), DEFAULT_SPEC)

    def test_registry_mirrors_draw_core_formats_and_orientations(self):
        from ocre_core.draw.paper import _SPECS
        # draw-core config.py: a4 a5 a3 carta caderno x retrato paisagem.
        self.assertEqual(
            sorted(_SPECS),
            [
                "A3", "A3-PAISAGEM", "A4", "A4-PAISAGEM", "A5",
                "A5-PAISAGEM", "CADERNO", "CADERNO-PAISAGEM", "CARTA",
                "CARTA-PAISAGEM",
            ],
        )
        paisagem = resolve_paper("A4-PAISAGEM")
        self.assertEqual(paisagem.canvas_width_px, 1200)
        self.assertEqual(paisagem.canvas_height_px, 850)
        self.assertAlmostEqual(paisagem.scale_mm_per_px, 297.0 / 1200.0)

    def test_lookup_is_case_insensitive_for_draw_core_names(self):
        self.assertIs(resolve_paper("a4"), DEFAULT_SPEC)
        a5 = resolve_paper("a5")
        self.assertEqual(a5.format, "A5")
        self.assertEqual(a5.width_mm, 148.0)
        self.assertEqual(a5.canvas_width_px, 846)  # round(1200*148/210)
        self.assertAlmostEqual(a5.effective_height_mm, 1200 * 148.0 / 846.0, places=2)
        self.assertIs(resolve_paper("carta"), resolve_paper("CARTA"))
        self.assertIs(resolve_paper("caderno"), resolve_paper("CADERNO"))

    def test_registered_specs_keep_effective_height_consistent(self):
        from ocre_core.draw.paper import _SPECS
        # validator exige |effective_height - scale*canvas_h| < 0.01.
        for spec in _SPECS.values():
            self.assertAlmostEqual(
                spec.effective_height_mm,
                spec.scale_mm_per_px * spec.canvas_height_px,
                delta=0.01,
                msg=spec.format,
            )

    def test_new_document_defaults_to_a4(self):
        doc = new_document(SOURCE)
        self.assertEqual(doc["paper"], DEFAULT_SPEC.as_dict())
        self.assertEqual(doc["paper"]["format"], "A4")


class NonA4SpecTests(unittest.TestCase):
    def test_document_validator_and_dxf_follow_a3(self):
        doc = new_document(SOURCE, paper=A3)
        self.assertEqual(doc["paper"]["format"], "A3")
        self.assertEqual(doc["paper"]["width_mm"], 297.0)
        self.assertEqual(doc["paper"]["effective_height_mm"], 419.76)
        # Points inside A3 but outside A4 (x > 210, y > 296.47) are fine.
        stroke = add_stroke(doc, [[250.0, 350.0], [260.0, 350.0], [260.0, 360.0]])
        stabilize_stroke(doc, stroke["stroke_id"])  # DXF includes stable only
        report = validate_draw_document(doc)
        self.assertTrue(report.valid, report.as_dict())
        # The DXF flips y against the A3 effective height, not A4's.
        dxf, dxf_report = build_dxf(doc)
        self.assertEqual(dxf_report["entities"], 1)
        polyline = next(iter(dxf.modelspace().query("LWPOLYLINE")))
        points = list(polyline.get_points(format="xy"))
        self.assertAlmostEqual(points[0][0], 250.0)
        self.assertAlmostEqual(points[0][1], 419.76 - 350.0)
        self.assertAlmostEqual(points[1][1], 419.76 - 350.0)
        self.assertAlmostEqual(points[2][1], 419.76 - 360.0)

    def test_points_outside_the_configured_sheet_are_refused(self):
        doc = new_document(SOURCE, paper=A3)
        with self.assertRaises(ValueError) as ctx:
            add_stroke(doc, [[300.0, 10.0], [310.0, 10.0]])
        self.assertIn("A3", str(ctx.exception))
        self.assertNotIn("A4", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            add_stroke(doc, [[10.0, 419.9], [20.0, 419.9]])
        self.assertIn("A3", str(ctx.exception))

    def test_validator_flags_points_outside_a3_by_name(self):
        doc = new_document(SOURCE, paper=A3)
        add_stroke(doc, [[10.0, 10.0], [20.0, 10.0]])
        doc["strokes"][0]["points_mm"] = [[300.0, 10.0], [310.0, 10.0]]
        report = validate_draw_document(doc)
        self.assertFalse(report.valid)
        codes = [d.code for d in report.diagnostics]
        self.assertIn("point_outside_sheet", codes)
        messages = [d.message for d in report.diagnostics]
        self.assertTrue(any("A3" in m for m in messages))
        self.assertTrue(all("A4" not in m for m in messages))

    def test_a4_still_refuses_points_outside_a4(self):
        doc = new_document(SOURCE)  # default A4
        with self.assertRaises(ValueError) as ctx:
            add_stroke(doc, [[250.0, 10.0], [260.0, 10.0]])
        self.assertIn("A4", str(ctx.exception))


class LoadDrawConfigTests(unittest.TestCase):
    def _write_config(self, tmp: Path, text: str) -> Path:
        path = tmp / "config.py"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_paper_w_h_and_derives_mm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                Path(tmp),
                "# Draw config (sample)\nPAPER_W = 1200\nPAPER_H = 1696\n",
            )
            spec = load_draw_config(path)
            self.assertEqual(spec.format, "DRAW-1200x1696")
            self.assertEqual(spec.canvas_width_px, 1200)
            self.assertEqual(spec.canvas_height_px, 1696)
            self.assertAlmostEqual(spec.width_mm, 1200 * 210.0 / 850.0)
            self.assertAlmostEqual(spec.effective_height_mm, 1696 * 210.0 / 850.0)
            self.assertAlmostEqual(spec.scale_mm_per_px, 210.0 / 850.0)

    def test_a4_config_returns_the_registered_a4_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                Path(tmp), "PAPER_W = 850\nPAPER_H = 1200\n")
            self.assertIs(load_draw_config(path), DEFAULT_SPEC)

    def test_ignores_other_assignments_and_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                Path(tmp),
                'NOTE = "PAPER_W = 999"\nPAPER_W = 850\nOTHER = 123\nPAPER_H = 1200\n',
            )
            self.assertIs(load_draw_config(path), DEFAULT_SPEC)

    def test_missing_paper_w_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(Path(tmp), "PAPER_H = 1200\n")
            with self.assertRaises(ValueError):
                load_draw_config(path)

    def test_non_integer_or_non_positive_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in (
                'PAPER_W = "850"\nPAPER_H = 1200\n',
                "PAPER_W = 850.5\nPAPER_H = 1200\n",
                "PAPER_W = 0\nPAPER_H = 1200\n",
                "PAPER_W = -850\nPAPER_H = 1200\n",
            ):
                path = self._write_config(Path(tmp), text)
                with self.assertRaises(ValueError, msg=text):
                    load_draw_config(path)

    def test_unparsable_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(Path(tmp), "PAPER_W = \n")
            with self.assertRaises(ValueError):
                load_draw_config(path)

    def test_missing_file_is_refused(self):
        with self.assertRaises((ValueError, FileNotFoundError, OSError)):
            load_draw_config(Path("/nonexistent/config.py"))


class PaperSizeMmTests(unittest.TestCase):
    def test_absent_or_non_object_paper_falls_back_to_default(self):
        # Bloco ausente ou malformado (str/None/list) -> papel default;
        # antes "A4" como string era AttributeError cru.
        for doc in ({}, {"paper": None}, {"paper": "A4"}, {"paper": [1]}):
            with self.subTest(doc=doc):
                self.assertEqual(
                    paper_size_mm(doc),
                    (DEFAULT_SPEC.width_mm, DEFAULT_SPEC.effective_height_mm),
                )

    def test_bool_and_non_numeric_fields_refuse_fail_closed(self):
        # bool não é número; None/str/list recusam como ValueError
        # (antes: TypeError cru, 1.0 mm silencioso ou str aceita).
        for bad_paper in (
            {"width_mm": None},
            {"width_mm": True},
            {"width_mm": "210"},
            {"width_mm": [210]},
            {"effective_height_mm": None},
            {"effective_height_mm": False},
            {"effective_height_mm": "296.47"},
            {"height_mm": "297"},
            {"height_mm": None},
        ):
            with self.subTest(paper=bad_paper):
                with self.assertRaises(ValueError):
                    paper_size_mm({"paper": bad_paper})

    def test_height_mm_used_when_effective_is_absent(self):
        width, height = paper_size_mm(
            {"paper": {"width_mm": 210.0, "height_mm": 297.0}})
        self.assertEqual((width, height), (210.0, 297.0))


class UnknownFormatTests(unittest.TestCase):
    def test_unknown_format_is_refused_never_invented(self):
        for name in ("A0", "Letter", "TABLOIDE", "a0", "B4"):
            with self.assertRaises(ValueError, msg=name):
                resolve_paper(name)
            with self.assertRaises(ValueError, msg=name):
                new_document(SOURCE, paper=name)


class CliPaperFlagTests(unittest.TestCase):
    def _export(self, out_dir: Path, extra: list[str],
                canvas: tuple[int, int] = (850, 1200)) -> tuple[int, dict]:
        png = write_png(out_dir / "canvas.png", *canvas)
        argv = [
            "export",
            "--png", str(png),
            "--vectorize-script", str(FAKE_VECTORIZE),
            "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
            "--out-dir", str(out_dir / "out"),
            "--origin", "upstream-canvas-png",
            "--draw-repo", "santleme/draw",
            "--draw-commit", COMMIT,
        ] + extra
        code = main(argv)
        out = out_dir / "out"
        doc = {}
        if (out / "document.json").is_file():
            doc = json.loads((out / "document.json").read_text(encoding="utf-8"))
        return code, doc

    def test_export_defaults_to_a4(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, doc = self._export(Path(tmp), [])
            self.assertEqual(code, 0)
            self.assertEqual(doc["paper"]["format"], "A4")
            self.assertEqual(doc["paper"]["width_mm"], 210)
            self.assertEqual(doc["paper"]["height_mm"], 297)

    def test_export_with_paper_a4_matches_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, doc = self._export(Path(tmp), ["--paper", "A4"])
            self.assertEqual(code, 0)
            self.assertEqual(doc["paper"], DEFAULT_SPEC.as_dict())

    def test_export_with_unknown_paper_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, doc = self._export(tmp_path, ["--paper", "A0"])
            self.assertNotEqual(code, 0)
            self.assertEqual(doc, {})
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_with_registered_format_validates_png_canvas(self):
        # A4-PAISAGEM pede canvas 1200x850; um PNG 850x1200 (retrato)
        # é recusado antes do subprocesso — a escala mm estaria errada.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, doc = self._export(tmp_path, ["--paper", "A4-PAISAGEM"])
            self.assertEqual(code, 2)
            self.assertEqual(doc, {})
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_with_paisagem_fails_closed_on_portrait_geometry(self):
        # Com o canvas certo (1200x850), o export repassa --paper-w/h-mm
        # ao vectorize; o fake devolve geometria A4-retrato (y até 250 mm
        # > 210.38 mm efetivos) e o documento é recusado, não cortado.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, doc = self._export(
                tmp_path, ["--paper", "A4-PAISAGEM"], canvas=(1200, 850))
            self.assertNotEqual(code, 0)
            self.assertEqual(doc, {})
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_with_draw_config_derives_the_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.py"
            config.write_text("PAPER_W = 1200\nPAPER_H = 1696\n", encoding="utf-8")
            code, doc = self._export(
                tmp_path, ["--draw-config", str(config)], canvas=(1200, 1696))
            self.assertEqual(code, 0)
            self.assertEqual(doc["paper"]["format"], "DRAW-1200x1696")
            self.assertAlmostEqual(doc["paper"]["width_mm"], 1200 * 210.0 / 850.0)
            self.assertAlmostEqual(
                doc["paper"]["effective_height_mm"], 1696 * 210.0 / 850.0)
            report = validate_draw_document(doc)
            self.assertTrue(report.valid, report.as_dict())

    def test_draw_config_overrides_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.py"
            config.write_text("PAPER_W = 1200\nPAPER_H = 1696\n", encoding="utf-8")
            code, doc = self._export(
                tmp_path, ["--paper", "A4", "--draw-config", str(config)],
                canvas=(1200, 1696))
            self.assertEqual(code, 0)
            self.assertEqual(doc["paper"]["format"], "DRAW-1200x1696")

    def test_draw_config_canvas_must_match_the_png(self):
        # config diz 1200x1696 mas o PNG é 850x1200: recusa antes de
        # vetorizar com a escala errada.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.py"
            config.write_text("PAPER_W = 1200\nPAPER_H = 1696\n", encoding="utf-8")
            code, doc = self._export(tmp_path, ["--draw-config", str(config)])
            self.assertEqual(code, 2)
            self.assertEqual(doc, {})
            self.assertFalse((tmp_path / "out" / "document.json").exists())

    def test_export_with_missing_draw_config_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, doc = self._export(
                tmp_path, ["--draw-config", str(tmp_path / "nope.py")])
            self.assertNotEqual(code, 0)
            self.assertEqual(doc, {})
            self.assertFalse((tmp_path / "out" / "document.json").exists())
