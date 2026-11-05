"""Testes do caso axial FEA (gate G4.1) -- Gmsh + CalculiX de verdade.

O teste do gate roda os binarios reais (``/usr/bin/gmsh`` e
``/usr/bin/ccx``): se o ambiente nao rodar, o teste FALHA
honestamente -- nada aqui e marcado como passado sem rodar. Os
caminhos de erro usam STUBs executaveis (mesmo padrao de
``test_librecad_gate.py``). O STEP da barra de referencia e gerado
pelo Gmsh/OCC a partir de um Box primitivo (``build_bar_step``) --
declarado no modulo: nao ha build123d neste venv.
"""

import os
import stat
import unittest
from pathlib import Path

from ocre_core.draw.fea import (
    AXIAL_CASE_ID,
    GATE_ID,
    AxialCaseSpec,
    build_bar_step,
    parse_ccx_dat,
    parse_gmsh_msh,
    run_axial_case,
    write_ccx_inp,
)
from ocre_core.draw.sidecar import validate_draw_sidecar

STUB_CCX_FAIL = """#!/bin/sh
echo "ccx: erro simulado do solver" >&2
exit 1
"""

STUB_CCX_SLEEP = """#!/bin/sh
# Dorme so na invocacao real do solver (`ccx -i job`); a chamada de
# versao (`ccx -v`) retorna na hora.
if [ "$1" = "-i" ]; then sleep 30; fi
exit 0
"""

STUB_GMSH_FAIL = """#!/bin/sh
echo "gmsh: erro simulado de malha" >&2
exit 2
"""

DAT_MINIMAL = """ forces (fx,fy,fz) for set FIXED and time  0.1000000E+01

         1 -1.050000E+03  0.000000E+00  0.000000E+00
         2 -1.050000E+03  0.000000E+00  0.000000E+00

 displacements (ux,uy,uz) for set LOADED and time  0.1000000E+01

         5  1.000000E-02  0.000000E+00  0.000000E+00

 stresses (elem, integ.pnt.,sxx,syy,szz,sxy,sxz,syz) for set BAR and time  0.1000000E+01

         1   1  2.100000E+01  0.000000E+00  0.000000E+00  0.000000E+00  0.000000E+00  0.000000E+00
         2   1  2.090000E+01  0.000000E+00  0.000000E+00  0.000000E+00  0.000000E+00  0.000000E+00
"""


def _make_stub(tmp: Path, name: str, content: str) -> Path:
    path = tmp / name
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _bar_step(tmp: Path) -> Path:
    built = build_bar_step(tmp / "gen")
    assert built["ok"], f"build_bar_step failed: {built['failure']}"
    return Path(built["step_path"])


class TestAxialGateReal(unittest.TestCase):
    """Gate G4.1 de verdade: Gmsh + ccx reais, Rx/S11 dentro de 1%."""

    def test_gate_passes_with_real_binaries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            result = run_axial_case(step, tmp_path / "case")
            self.assertEqual(result["status"], "passed",
                             f"gate failed: {result['failures']} "
                             f"measured={result['measured']}")
            self.assertEqual(result["case"], AXIAL_CASE_ID)
            self.assertEqual(result["gate"]["id"], GATE_ID)
            self.assertTrue(result["gate"]["passed"])
            self.assertLessEqual(result["errors_pct"]["rx"], 1.0)
            self.assertLessEqual(result["errors_pct"]["s11"], 1.0)
            # Referencia: Rx = 2100 N, S11 = 21 MPa.
            self.assertAlmostEqual(abs(result["measured"]["rx_N"]),
                                   2100.0, delta=21.0)
            self.assertAlmostEqual(result["measured"]["s11_max_MPa"],
                                   21.0, delta=0.21)
            self.assertAlmostEqual(result["measured"]["ux_max_mm"],
                                   0.01, delta=1e-6)
            # Artefatos brutos ficam no workdir com sha256.
            for name in ("mesh", "inp", "frd", "dat", "sta"):
                self.assertIn(name, result["artifacts"])
                self.assertEqual(
                    len(result["artifacts"][name]["sha256"]), 64)
                self.assertTrue(
                    (tmp_path / "case"
                     / result["artifacts"][name]["file"]).is_file())
            # Sidecar valido e fingerprint com o dominio certo.
            self.assertEqual(
                validate_draw_sidecar(result["sidecar"]), [])
            self.assertEqual(
                result["fingerprint"]["domain"],
                "DRAW:PAPER-APPROVAL:FINGERPRINT")
            self.assertEqual(len(result["fingerprint"]["digest"]), 64)
            # NU=0.3 registrado com a proveniencia (errata do Senado).
            params = result["sidecar"]["export_parameters"]
            self.assertEqual(params["poisson_ratio"], 0.3)
            self.assertIn("synthetic benchmark",
                          params["poisson_ratio_source"])


class TestIncompleteCaseBlocked(unittest.TestCase):
    def test_missing_material_blocks_before_any_subprocess(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            spec = AxialCaseSpec(step_path=step, young_modulus_mpa=None)
            # Binarios falsos: se tentasse rodar, falharia de outro jeito.
            result = run_axial_case(step, tmp_path / "case", spec=spec,
                                    gmsh_bin="/nonexistent/gmsh",
                                    ccx_bin="/nonexistent/ccx")
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(any("young_modulus_mpa" in f
                                for f in result["failures"]))
            self.assertIsNone(result["measured"])

    def test_missing_load_blocks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            spec = AxialCaseSpec(step_path=step, prescribed_ux_mm=None)
            result = run_axial_case(step, tmp_path / "case", spec=spec)
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(any("prescribed_ux_mm" in f
                                for f in result["failures"]))

    def test_missing_nu_blocks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            spec = AxialCaseSpec(step_path=step, poisson_ratio=None)
            result = run_axial_case(step, tmp_path / "case", spec=spec)
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(any("poisson_ratio" in f
                                for f in result["failures"]))

    def test_missing_step_file_blocks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = run_axial_case(tmp_path / "nao-existe.step",
                                    tmp_path / "case")
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(any("not found" in f
                                for f in result["failures"]))


class TestSolverFailuresRecorded(unittest.TestCase):
    def test_ccx_exit_nonzero_is_recorded_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            stub = _make_stub(tmp_path, "ccx-fail", STUB_CCX_FAIL)
            result = run_axial_case(step, tmp_path / "case",
                                    ccx_bin=str(stub))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("ccx failed" in f
                                for f in result["failures"]))
            self.assertIsNone(result["measured"])

    def test_ccx_timeout_is_recorded_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            stub = _make_stub(tmp_path, "ccx-sleep", STUB_CCX_SLEEP)
            spec = AxialCaseSpec(step_path=step, timeout_ccx_s=1.0)
            result = run_axial_case(step, tmp_path / "case", spec=spec,
                                    ccx_bin=str(stub))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("timeout" in f
                                for f in result["failures"]))

    def test_gmsh_exit_nonzero_is_recorded_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            stub = _make_stub(tmp_path, "gmsh-fail", STUB_GMSH_FAIL)
            result = run_axial_case(step, tmp_path / "case",
                                    gmsh_bin=str(stub))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("gmsh failed" in f
                                for f in result["failures"]))


class TestParsersAndWriter(unittest.TestCase):
    def test_parse_ccx_dat_minimal(self):
        parsed = parse_ccx_dat(DAT_MINIMAL)
        self.assertAlmostEqual(parsed["rx_N"], -2100.0)
        self.assertAlmostEqual(parsed["s11_max_MPa"], 21.0)
        self.assertAlmostEqual(parsed["ux_max_mm"], 0.01)
        self.assertEqual(parsed["n_fixed_nodes"], 2)
        self.assertEqual(parsed["n_elements"], 2)

    def test_parse_ccx_dat_missing_section_raises(self):
        with self.assertRaises(ValueError):
            parse_ccx_dat("nada aqui\n")

    def test_parse_gmsh_msh_real_mesh(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            result = run_axial_case(step, tmp_path / "case")
            self.assertEqual(result["status"], "passed")
            mesh = parse_gmsh_msh(tmp_path / "case" / "bar.msh")
            self.assertGreater(len(mesh["nodes"]), 100)
            self.assertGreater(len(mesh["tets"]), 100)
            self.assertIn("fixed", mesh["face_nodes"])
            self.assertIn("loaded", mesh["face_nodes"])

    def test_write_ccx_inp_wraps_long_node_lists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            step = _bar_step(tmp_path)
            result = run_axial_case(step, tmp_path / "case")
            self.assertEqual(result["status"], "passed")
            mesh = parse_gmsh_msh(tmp_path / "case" / "bar.msh")
            out = tmp_path / "job2.inp"
            write_ccx_inp(mesh, out)
            for line in out.read_text(encoding="utf-8").splitlines():
                entries = [e for e in line.split(",") if e.strip()]
                # CalculiX: no maximo 16 entradas por linha; usamos 8.
                self.assertLessEqual(len(entries), 16, line[:80])

    def test_parse_gmsh_msh_rejects_wrong_format(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.msh"
            bad.write_text("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n",
                           encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_gmsh_msh(bad)


if __name__ == "__main__":
    unittest.main()
