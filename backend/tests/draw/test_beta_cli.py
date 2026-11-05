"""Tests for the beta v0.1 end-to-end CLI (``beta-run``).

Two layers, kept apart on purpose:

- Mocked chain (fast, always runs): every step's layer is mocked
  and the test asserts the chain calls the steps IN ORDER, assembles
  a complete report, blocks fail-closed without thickness, and
  reports a missing OCS binary as ``skipped-unavailable`` without
  failing the chain. The real library functions are NOT exercised
  here -- they have their own test files.
- Real end-to-end (slow, ``pytest.mark.slow``): runs the real chain
  over the ``beta-exemplo`` fixture with the real OCS binary and the
  real FreeCAD oracle when they exist on this machine; skipped
  honestly (with the reason) when any of them is missing. A skip is
  not a pass.

Fingerprint domain cited by the report is
``DRAW:PAPER-APPROVAL:FINGERPRINT`` (Senate errata).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

import ocre_core.draw.cli as cli_mod
from ocre_core.draw.cli import main
from ocre_core.draw.vectorize_runner import sha256_file

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BETA_EXEMPLO = FIXTURES / "beta-exemplo"
FAKE_VECTORIZE = FIXTURES / "fake_vectorize.py"
BETA_PNG = BETA_EXEMPLO / "canvas.png"
BETA_DOCUMENT = BETA_EXEMPLO / "document.json"
COMMIT = "a" * 40

OCS_BINARY = Path("/home/hatch/workspace/ocs-spike-tmp/squashfs-root"
                  "/usr/bin/OpenCADStudio")
FREECAD_CMD = Path("/home/hatch/workspace/ocre-beta-wt-step/.external"
                   "/squashfs-root/usr/bin/freecadcmd")


def _run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def _base_argv(out_dir: Path, extra: list[str] | None = None) -> list[str]:
    return [
        "beta-run",
        "--png", str(BETA_PNG),
        "--vectorize-script", str(FAKE_VECTORIZE),
        "--vectorize-sha256", sha256_file(FAKE_VECTORIZE),
        "--out-dir", str(out_dir),
        "--origin", "upstream-canvas-png",
        "--draw-repo", "santleme/draw",
        "--draw-commit", COMMIT,
        "--thickness-mm", "2.0",
        "--material", "steel",
        "--document", str(BETA_DOCUMENT),
    ] + (extra or [])


class _FakeOcsClient:
    """Stand-in for OcsClient: syncs ids, erases the tombstoned one."""

    def __init__(self):
        self.synced: list[str] = []
        self.erased: list[str] = []

    def sync_strokes(self, dxf_path, stroke_ids, *, out_path):
        self.synced = list(stroke_ids)
        return {
            "dxf_path": str(dxf_path), "out_path": str(out_path),
            "handles": ["8C"] * len(stroke_ids),
            "stroke_ids": list(stroke_ids),
            "xdata_round_trip": {sid: sid for sid in stroke_ids},
            "dropped_on_save": 0,
            "gate_g2_1_open_to_queryable_s": 0.05,
            "save_verified": {"verified": True, "sha256": "b" * 64,
                              "dropped_on_save": 0},
        }

    def scan_stroke_ids(self):
        return {sid: "8C" for sid in self.synced
                if sid not in self.erased}

    def sync_document(self, document, *, out_path=None):
        erased = [s["stroke_id"] for s in document["strokes"]
                  if s["status"] == "erased" and s["stroke_id"] in self.synced]
        self.erased = erased
        live = [s["stroke_id"] for s in document["strokes"]
                if s["status"] != "erased"]
        return {
            "erased": erased,
            "already_absent": [s["stroke_id"] for s in document["strokes"]
                               if s["status"] == "erased"
                               and s["stroke_id"] not in self.synced],
            "live": live,
            "remaining": {sid: "8C" for sid in live},
            "verified_absent": True,
            "save_verified": {"verified": True, "sha256": "c" * 64,
                              "dropped_on_save": 0},
        }


class BetaRunMockedTests(unittest.TestCase):
    """Chain with mocked layers: order, complete report, fail-closed."""

    def _patch_chain(self, calls: list[str], ocs_binary: str | None):
        """Patch every layer; each wrapper records its step id."""
        from ocre_core.draw.dxf_export import export_dxf

        patches = []

        def fake_export(namespace):
            calls.append("export")
            out_dir = Path(namespace.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            doc = json.loads(BETA_DOCUMENT.read_text(encoding="utf-8"))
            (out_dir / "document.json").write_text(
                json.dumps(doc, sort_keys=True), encoding="utf-8")
            export_dxf(doc, out_dir / "document.dxf")
            return 0

        patches.append(mock.patch.object(
            cli_mod, "_cmd_export", side_effect=fake_export))

        def fake_find_ocs(binary):
            calls.append("ocs-find")
            return ocs_binary

        patches.append(mock.patch(
            "ocre_core.draw.ocs.find_ocs_binary",
            side_effect=fake_find_ocs))

        fake_client = _FakeOcsClient()

        def fake_launch(binary, **kwargs):
            calls.append("ocs")
            return mock.Mock(), fake_client

        patches.append(mock.patch(
            "ocre_core.draw.ocs.launch_headless",
            side_effect=fake_launch))
        patches.append(mock.patch(
            "ocre_core.draw.ocs.stop_server", return_value=None))

        def fake_step(document, thickness_mm, material, out_path, **kwargs):
            calls.append("step")
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text("fake STEP", encoding="utf-8")
            return {
                "output_path": str(out_path), "output_sha256": "d" * 64,
                "output_bytes": 9, "file_schema": "AP242",
                "thickness_mm": thickness_mm,
                "material_declared": material,
                "stroke_ids": ["s-0001"], "skipped": [],
                "profiles": 1, "expected_volume_mm3": 6400.0,
                "volume_mm3": 6400.0,
                "bbox_mm": [[10.0, 226.47, 0.0], [90.0, 266.47, 2.0]],
                "worker_versions": {"build123d": "0.13.0"},
            }

        patches.append(mock.patch(
            "ocre_core.draw.step_export.document_to_step",
            side_effect=fake_step))
        patches.append(mock.patch(
            "ocre_core.draw.step_export.validate_step",
            side_effect=lambda p, **k: (calls.append("brepcheck"),
                                        {"ok": True, "brepcheck_valid": True})[1]))
        patches.append(mock.patch(
            "ocre_core.draw.step_export.oracle_step",
            side_effect=lambda p, **k: (calls.append("oracle"),
                                        {"status": "pass", "available": True,
                                         "is_valid": True,
                                         "volume_mm3": 6400.0})[1]))

        def fake_fea(step_path, workdir, **kwargs):
            calls.append("fea")
            Path(workdir).mkdir(parents=True, exist_ok=True)
            return {
                "status": "passed", "case": "axial-bar-100x10x10",
                "gate": {"id": "G4.1", "tolerance_pct": 1.0, "passed": True},
                "measured": {"rx_N": -2100.0, "s11_max_MPa": 21.0},
                "errors_pct": {"rx": 0.0, "s11": 0.0},
                "artifacts": {},
                "fingerprint": {"domain": "DRAW:PAPER-APPROVAL:FINGERPRINT",
                                "digest": "e" * 64},
                "failures": [],
            }

        patches.append(mock.patch(
            "ocre_core.draw.fea.build_bar_step",
            side_effect=lambda w, **k: (calls.append("fea-bar"),
                                        {"ok": True,
                                         "step_path": str(w / "bar.step")})[1]))
        patches.append(mock.patch(
            "ocre_core.draw.fea.run_axial_case",
            side_effect=fake_fea))

        def fake_physics(**kwargs):
            calls.append("physics")
            return {
                "case": "axial_bar_contact.v1",
                "model_sha256": "f" * 64,
                "engines": {
                    "pybullet": {"status": "ok",
                                 "envelope": {"f_max_N": 1.5, "rms_N": 0.2,
                                              "p95_N": 1.0}},
                    "mujoco": {"status": "ok",
                               "envelope": {"f_max_N": 1.6, "rms_N": 0.2,
                                            "p95_N": 1.1}},
                },
                "comparison": {"divergence_f_max_N": 0.06},
                "pinocchio_check": {"passed": True},
                "load_profile_pybullet": {"profile": "physics.load_profile.v1",
                                          "f_max_N": 1.5},
                "load_profile_mujoco": {"profile": "physics.load_profile.v1",
                                        "f_max_N": 1.6},
            }

        patches.append(mock.patch(
            "ocre_core.draw.physics.run_contact_case",
            side_effect=fake_physics))

        patches.append(mock.patch(
            "ocre_core.draw.cli._beta_gmsh_surface_stl",
            side_effect=lambda *a, **k: (calls.append("gmsh-stl"),
                                         {"stl_path": str(a[1]),
                                          "stl_bytes": 10})[1]))
        patches.append(mock.patch(
            "ocre_core.draw.viz.render_field",
            side_effect=lambda *a, **k: (calls.append("viz"),
                                         calls.append("render"),
                                         {"out_png": str(a[1]),
                                          "png_bytes": 100,
                                          "field": "SXX",
                                          "visualization_only": True})[2]))
        patches.append(mock.patch(
            "ocre_core.draw.viz.to_glb",
            side_effect=lambda *a, **k: (calls.append("glb"),
                                         {"out_glb": str(a[1]),
                                          "glb_bytes": 200,
                                          "is_watertight": True,
                                          "bounds_after_m": [[0, 0, 0],
                                                             [0.1, 0.01, 0.01]],
                                          "visualization_only": True})[1]))
        patches.append(mock.patch(
            "ocre_core.draw.viz.plot_pdf",
            side_effect=lambda *a, **k: (calls.append("pdf"),
                                         {"out_pdf": str(a[1]),
                                          "pdf_bytes": 300,
                                          "route": "librecad dxf2pdf",
                                          "visualization_only": True})[1]))

        def fake_fmu(fmu_path=None, **kwargs):
            # The real validate_fmu is covered by its own test below;
            # here only the call order matters.
            calls.append("fmu")
            return {
                "gate": "fmu-fmpy",
                "fingerprint_domain": "DRAW:PAPER-APPROVAL:FINGERPRINT",
                "fmu_path": fmu_path,
                "status": "not-triggered",
                "available": False,
                "reason": "beta v0.1 has no FMU",
            }

        patches.append(mock.patch(
            "ocre_core.draw.fmu_gate.validate_fmu", side_effect=fake_fmu))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake_client

    def test_mocked_chain_runs_steps_in_order_with_complete_report(self):
        calls: list[str] = []
        self._patch_chain(calls, ocs_binary="/fake/OpenCADStudio")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_dir = Path(tmp.name) / "out"
        code, out = _run_cli(_base_argv(out_dir))
        report = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["fingerprint_domain"],
                         "DRAW:PAPER-APPROVAL:FINGERPRINT")
        step_ids = [s["id"] for s in report["steps"]]
        self.assertEqual(step_ids,
                         ["export", "ocs", "step", "fea", "physics", "viz",
                          "fmu"])
        self.assertEqual([s["status"] for s in report["steps"]],
                         ["ok", "ok", "ok", "ok", "ok", "ok",
                          "not-triggered"])
        # Order: export first, ocs before step, fea before physics/viz,
        # fmu last.
        order = [c for c in calls if c in
                 ("export", "ocs", "step", "fea", "physics", "viz", "fmu")]
        self.assertEqual(order, ["export", "ocs", "step", "fea", "physics",
                                 "viz", "fmu"])
        # The eraser is proven by the mocked OCS step: the tombstoned
        # s-0002 never reaches the CAD (the F0 DXF excludes erased
        # strokes), so sync_document proves it already absent; s-0001
        # stays live with its id.
        ocs = next(s for s in report["steps"] if s["id"] == "ocs")
        self.assertEqual(ocs["detail"]["sync_document"]["erased"], [])
        self.assertEqual(ocs["detail"]["sync_document"]["already_absent"],
                         ["s-0002"])
        self.assertEqual(ocs["detail"]["scan_after_sync_document"],
                         {"s-0001": "8C"})
        self.assertTrue(ocs["detail"]["sync_document"]["verified_absent"])
        # STEP used the declared thickness/material; FEA gate passed.
        step = next(s for s in report["steps"] if s["id"] == "step")
        self.assertEqual(step["detail"]["step"]["thickness_mm"], 2.0)
        self.assertEqual(step["detail"]["step"]["material_declared"], "steel")
        self.assertEqual(step["detail"]["step"]["volume_mm3"], 6400.0)
        fea = next(s for s in report["steps"] if s["id"] == "fea")
        self.assertEqual(fea["detail"]["gate"]["id"], "G4.1")
        self.assertTrue(fea["detail"]["gate"]["passed"])
        # FMU gate is honestly not-triggered without an FMU.
        fmu = next(s for s in report["steps"] if s["id"] == "fmu")
        self.assertEqual(fmu["status"], "not-triggered")
        # Report files exist and agree with stdout.
        report_path = out_dir / "beta-report.json"
        self.assertTrue(report_path.is_file())
        self.assertEqual(json.loads(report_path.read_text()),
                         report)
        self.assertTrue((out_dir / "beta-report.txt").is_file())
        self.assertIn("status: ok", (out_dir / "beta-report.txt").read_text())
        # Both documents are recorded: the F0 export and the working one.
        self.assertEqual(report["documents"]["working"]["doc_id"],
                         "11111111-2222-4333-8444-555555555555")
        self.assertEqual(report["documents"]["export"]["doc_id"],
                         "11111111-2222-4333-8444-555555555555")

    def test_missing_thickness_blocks_fail_closed_before_any_step(self):
        calls: list[str] = []
        self._patch_chain(calls, ocs_binary="/fake/OpenCADStudio")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            argv = _base_argv(out_dir)
            argv.remove("2.0")  # drop the --thickness-mm value
            argv.remove("--thickness-mm")
            code, out = _run_cli(argv)
            report = json.loads(out)
        self.assertEqual(code, 2, out)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("thickness-mm is required",
                      report["steps"][0]["error"])
        self.assertEqual(calls, [], "no step ran after a blocked preflight")

    def test_missing_material_blocks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            argv = _base_argv(out_dir)
            argv.remove("steel")
            argv.remove("--material")
            code, out = _run_cli(argv)
            report = json.loads(out)
        self.assertEqual(code, 2, out)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("material is required", report["steps"][0]["error"])

    def test_invalid_document_blocks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_doc = Path(tmp) / "bad.json"
            bad_doc.write_text('{"schema": "draw.paper.v1", "strokes": []}',
                               encoding="utf-8")
            out_dir = Path(tmp) / "out"
            argv = _base_argv(out_dir)
            argv.remove("--document")
            argv.remove(str(BETA_DOCUMENT))
            argv += ["--document", str(bad_doc)]
            code, out = _run_cli(argv)
            report = json.loads(out)
        self.assertEqual(code, 2, out)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("--document failed validation",
                      report["steps"][0]["error"])

    def test_missing_ocs_binary_is_skipped_honestly_without_failing(self):
        calls: list[str] = []
        self._patch_chain(calls, ocs_binary=None)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            code, out = _run_cli(_base_argv(out_dir))
            report = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(report["status"], "ok")
        ocs = next(s for s in report["steps"] if s["id"] == "ocs")
        self.assertEqual(ocs["status"], "skipped-unavailable")
        self.assertIn("no OpenCADStudio binary", ocs["detail"]["reason"])
        # The chain went on: every later step still ran.
        order = [c for c in calls if c in
                 ("export", "step", "fea", "physics", "viz", "fmu")]
        self.assertEqual(order, ["export", "step", "fea", "physics", "viz",
                                 "fmu"])

    def test_fmu_gate_reports_not_triggered_without_fmu(self):
        from ocre_core.draw.fmu_gate import validate_fmu

        result = validate_fmu(None)
        self.assertEqual(result["status"], "not-triggered")
        self.assertFalse(result["available"])
        missing = validate_fmu("/tmp/does-not-exist.fmu")
        self.assertEqual(missing["status"], "blocked")


def _beta_venv_ready() -> bool:
    """True when the beta venv libs (physics/viz engines) import here."""
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("pybullet", "mujoco", "pinocchio", "pyvista",
                     "meshio", "trimesh", "build123d")
    )


@unittest.skipUnless(OCS_BINARY.is_file(), "sem binário OpenCADStudio "
                     "em ~/workspace/ocs-spike-tmp — e2e real não rodado")
@unittest.skipUnless(FREECAD_CMD.is_file(), "sem freecadcmd extraído em "
                     "~/workspace/ocre-beta-wt-step/.external — e2e real "
                     "não rodado")
@unittest.skipUnless(_beta_venv_ready(), "venv do beta (.venv-beta) sem "
                     "os engines de física/viz — e2e real não rodado")
@pytest.mark.slow
class BetaRunRealEndToEndTests(unittest.TestCase):
    """Real end-to-end over beta-exemplo (slow, ~40 s)."""

    def test_real_chain_over_beta_exemplo(self):
        import shutil

        self.assertTrue(shutil.which("gmsh"), "gmsh ausente")
        self.assertTrue(shutil.which("ccx"), "ccx ausente")
        self.assertTrue(shutil.which("librecad"), "librecad ausente")
        env = {
            "OCS_API_TOKEN": "ocs-test-token-real",
            "FREECAD_CMD": str(FREECAD_CMD),
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_dir = Path(tmp.name) / "out"
        argv = _base_argv(out_dir, [
            "--ocs-binary", str(OCS_BINARY),
        ])
        with mock.patch.dict(os.environ, env):
            code, out = _run_cli(argv)
        report = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(report["status"], "ok")
        by_id = {s["id"]: s for s in report["steps"]}
        self.assertEqual([s["status"] for s in report["steps"]],
                         ["ok", "ok", "ok", "ok", "ok", "ok",
                          "not-triggered"])
        # OCS: ids preserved, eraser proven, save verified.
        ocs = by_id["ocs"]["detail"]
        self.assertEqual(ocs["sync"]["xdata_round_trip"],
                         {"s-0001": "s-0001"})
        self.assertEqual(set(ocs["scan_after_sync"]), {"s-0001"})
        self.assertEqual(ocs["sync_document"]["already_absent"], ["s-0002"])
        self.assertTrue(ocs["sync_document"]["verified_absent"])
        self.assertTrue(ocs["sync_document"]["save_verified"]["verified"])
        self.assertLessEqual(
            ocs["sync"]["gate_g2_1_open_to_queryable_s"], 2.0)
        # The saved file (read outside the OCS) keeps s-0001, not s-0002.
        import ezdxf

        saved = ezdxf.readfile(out_dir / "ocs" / "ocs-after-erase.dxf")
        found = {e.get_xdata("DRAW")[0].value
                 for e in saved.modelspace() if e.get_xdata("DRAW")}
        self.assertEqual(found, {"s-0001"})
        # STEP: 80x40 mm rectangle x 2 mm = 6400 mm3; oracle passed.
        step = by_id["step"]["detail"]
        self.assertAlmostEqual(step["step"]["volume_mm3"], 6400.0, places=3)
        self.assertEqual(step["step"]["stroke_ids"], ["s-0001"])
        self.assertTrue(step["brepcheck"]["brepcheck_valid"])
        self.assertEqual(step["oracle"]["status"], "pass")
        self.assertTrue(step["oracle"]["is_valid"])
        # FEA gate G4.1 passed within 1%.
        fea = by_id["fea"]["detail"]
        self.assertTrue(fea["gate"]["passed"])
        self.assertLessEqual(fea["errors_pct"]["rx"], 1.0)
        self.assertLessEqual(fea["errors_pct"]["s11"], 1.0)
        # Physics: both engines produced load profiles.
        phys = by_id["physics"]["detail"]
        self.assertEqual(set(phys["load_profiles"]),
                         {"pybullet", "mujoco"})
        self.assertTrue(phys["pinocchio_check_passed"])
        # Viz: png (visualization only), watertight glb in metres, PDF.
        viz = by_id["viz"]["detail"]
        self.assertTrue(viz["render"]["visualization_only"])
        self.assertTrue(viz["glb"]["is_watertight"])
        # Bounds are float32-rounded by the glTF round-trip (0.1 m
        # reads back as 0.10000000149...), so compare approximately.
        bounds = viz["glb"]["bounds_after_m"]
        for got, want in zip(bounds[0], [0.0, 0.0, 0.0]):
            self.assertAlmostEqual(got, want, places=6)
        for got, want in zip(bounds[1], [0.1, 0.01, 0.01]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertGreater(viz["pdf"]["pdf_bytes"], 0)
        for rel in ("export/document.json", "export/document.dxf",
                    "export/sidecar.json", "export/fingerprint.json",
                    "step/drawing.step", "fea/case/job.frd",
                    "physics/physics.json",
                    "physics/load-profile-pybullet.json",
                    "physics/load-profile-mujoco.json",
                    "viz/field.png", "viz/model.glb", "viz/drawing.pdf",
                    "beta-report.json", "beta-report.txt"):
            self.assertTrue((out_dir / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
