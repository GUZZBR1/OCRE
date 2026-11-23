"""Testes do runner black-box do vectorize (F0).

Ressalva explícita: todos os testes usam a FIXTURE
``fixtures/fake_vectorize.py`` — um script que IMITA a CLI do
``src/draw/exporters/vectorize.py`` real e devolve um payload fixo. Nenhum teste
aqui roda o vectorize real do Draw nem um PNG real; a bateria real
G0.1/G0.2 (20 folhas) NÃO foi rodada.
"""

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocre_core.draw.vectorize_runner import (
    VectorizeError,
    find_vectorize_python,
    normalize_primitives,
    run_vectorize,
    sha256_file,
)

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fixtures" / "fake_vectorize.py"


def fake_png(tmp: Path) -> Path:
    # O fake não lê o PNG; só precisa existir.
    png = tmp / "canvas.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    return png


class Sha256Tests(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_bytes(b"conteudo")
            self.assertEqual(sha256_file(path), hashlib.sha256(b"conteudo").hexdigest())


class NormalizeTests(unittest.TestCase):
    def test_rectangle_uses_corners_and_is_closed(self):
        polylines, without = normalize_primitives(
            [{"type": "rectangle", "corners_mm": [[0, 0], [1, 0], [1, 1], [0, 1]]}]
        )
        self.assertEqual(len(polylines), 1)
        self.assertTrue(polylines[0]["closed"])
        self.assertEqual(polylines[0]["type"], "rectangle")
        self.assertEqual(without, 0)

    def test_circle_is_discretized_closed_with_analytic_params(self):
        polylines, without = normalize_primitives(
            [{"type": "circle", "cx_mm": 150.0, "cy_mm": 150.0,
              "r_mm": 25.0, "radial_rmse_px": 0.5}]
        )
        self.assertEqual(without, 0)
        self.assertEqual(len(polylines), 1)
        circle = polylines[0]
        self.assertEqual(circle["type"], "circle")
        self.assertTrue(circle["closed"])
        self.assertEqual(len(circle["points_mm"]), 64)
        for x, y in circle["points_mm"]:
            self.assertAlmostEqual(
                math.hypot(x - 150.0, y - 150.0), 25.0, places=2)
        self.assertEqual(
            circle["circle_mm"],
            {"cx_mm": 150.0, "cy_mm": 150.0, "r_mm": 25.0,
             "radial_rmse_px": 0.5},
        )

    def test_point_becomes_single_point_polyline(self):
        polylines, without = normalize_primitives(
            [{"type": "point", "x_mm": 200.0, "y_mm": 250.0}]
        )
        self.assertEqual(without, 0)
        self.assertEqual(polylines, [
            {"points_mm": [[200.0, 250.0]], "closed": False, "type": "point"}])

    def test_malformed_circle_and_point_count_as_without(self):
        polylines, without = normalize_primitives([
            {"type": "circle", "cx_mm": 1.0, "cy_mm": 2.0, "r_mm": 0.0},
            {"type": "circle", "cx_mm": 1.0, "cy_mm": 2.0},
            {"type": "point", "x_mm": 1.0},
            {"type": "blob"},
        ])
        self.assertEqual(polylines, [])
        self.assertEqual(without, 4)

    def test_non_dict_primitives_are_counted_not_crashed(self):
        # Elemento não-dict (null, string, número) no JSON do vetorizador
        # era AttributeError cru; agora conta como tipo não reconhecido.
        polylines, without = normalize_primitives(
            [
                None,
                "x",
                5,
                {"type": "polyline", "points_mm": [[0, 0], [1, 1]]},
            ]
        )
        self.assertEqual(len(polylines), 1)
        self.assertEqual(without, 3)

    def test_stamps_are_preserved_and_validated(self):
        polylines, without = normalize_primitives([
            {"type": "line", "x1_mm": 1, "y1_mm": 2, "x2_mm": 3, "y2_mm": 4,
             "t_start_ms": 100, "t_end_ms": 250},
            {"type": "point", "x_mm": 1.0, "y_mm": 2.0, "t_start_ms": 300,
             "t_end_ms": 300},
        ])
        self.assertEqual(without, 0)
        self.assertEqual(polylines[0]["t_start_ms"], 100)
        self.assertEqual(polylines[0]["t_end_ms"], 250)
        self.assertEqual(polylines[1]["t_start_ms"], 300)
        with self.assertRaises(VectorizeError):
            normalize_primitives(
                [{"type": "line", "x1_mm": 1, "y1_mm": 2, "x2_mm": 3,
                  "y2_mm": 4, "t_start_ms": "cem"}])
        with self.assertRaises(VectorizeError):
            normalize_primitives(
                [{"type": "line", "x1_mm": 1, "y1_mm": 2, "x2_mm": 3,
                  "y2_mm": 4, "t_start_ms": -5}])

    def test_line_endpoints_are_normalized_and_legacy_points_still_work(self):
        primitives = [
            {"type": "line", "x1_mm": 1, "y1_mm": 2, "x2_mm": 3, "y2_mm": 4},
            {"type": "line", "points_mm": [[5, 6], [7, 8]]},
        ]
        polylines, without = normalize_primitives(primitives)
        self.assertEqual([p["points_mm"] for p in polylines], [
            [[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]],
        ])
        self.assertEqual(without, 0)

    def test_malformed_points_are_vectorize_error(self):
        # Ponto não numérico/desempacotável era ValueError/TypeError
        # cru; o contrato do runner é VectorizeError. E bool/str/NaN
        # passavam silenciosos por float() — coordenada corrompida.
        for bad_points in (
            [["a", "b"], [1, 2]],
            [[1, 2, 3], [4, 5]],
            [5, [1, 2]],
            [[None, 1], [2, 3]],
            [[True, 0.0], [1, 2]],
            [["1.5", "2.5"], [1, 2]],
            [[float("nan"), 1], [2, 3]],
            [[float("inf"), 1], [2, 3]],
        ):
            with self.subTest(points_mm=bad_points):
                with self.assertRaises(VectorizeError):
                    normalize_primitives([
                        {"type": "polyline", "points_mm": bad_points}])

class FindVectorizePythonTests(unittest.TestCase):
    """Costura de interpretador: explícito > env > sys.executable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_python(self, name: str = "python") -> Path:
        fake = self.tmp / name
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def test_explicit_wins_over_env(self):
        explicit = self._fake_python("explicit-python")
        env = self._fake_python("env-python")
        with mock.patch.dict(os.environ, {"OCRE_VECTORIZE_PYTHON": str(env)}):
            self.assertEqual(
                find_vectorize_python(explicit),
                Path(os.path.abspath(explicit)))

    def test_env_wins_over_sys_executable(self):
        env = self._fake_python()
        with mock.patch.dict(os.environ, {"OCRE_VECTORIZE_PYTHON": str(env)}):
            self.assertEqual(
                find_vectorize_python(), Path(os.path.abspath(env)))

    def test_sys_executable_is_the_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                find_vectorize_python(),
                Path(os.path.abspath(sys.executable)))

    def test_declared_but_missing_refuses_without_fallback(self):
        # Caminho declarado inexistente é erro claro — nunca cai para
        # sys.executable nem para outro fallback não pedido.
        for kwargs, env in (
            ({"explicit": self.tmp / "ausente"}, {}),
            ({}, {"OCRE_VECTORIZE_PYTHON": str(self.tmp / "ausente")}),
        ):
            with self.subTest(kwargs=kwargs, env=env):
                with mock.patch.dict(os.environ, env):
                    with self.assertRaises(VectorizeError):
                        find_vectorize_python(**kwargs)

    def test_non_executable_refuses(self):
        not_exec = self.tmp / "python"
        not_exec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        not_exec.chmod(0o644)
        with self.assertRaises(VectorizeError):
            find_vectorize_python(not_exec)
        with mock.patch.dict(os.environ, {"OCRE_VECTORIZE_PYTHON": str(not_exec)}):
            with self.assertRaises(VectorizeError):
                find_vectorize_python()

    def test_tilde_in_env_path_is_expanded(self):
        home_python = self.tmp / "home" / "bin" / "python"
        home_python.parent.mkdir(parents=True)
        home_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        home_python.chmod(0o755)
        env = {
            "OCRE_VECTORIZE_PYTHON": "~/bin/python",
            "HOME": str(self.tmp / "home"),
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                find_vectorize_python(),
                Path(os.path.abspath(home_python)))

    def test_venv_symlink_is_not_resolved(self):
        # bin/python de venv é symlink: .resolve() rodaria o subprocesso
        # FORA do venv — o caminho devolvido é absoluto, não resolvido.
        link = self.tmp / "venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        link.symlink_to(sys.executable)
        result = find_vectorize_python(link)
        self.assertEqual(result, Path(os.path.abspath(link)))
        self.assertNotEqual(result, Path(os.path.realpath(link)))

    def test_dotdot_after_directory_symlink_is_not_collapsed(self):
        # sub/link_dir -> target_dir: sub/link_dir/../real_bin/python resolve
        # pelo kernel para self.tmp/real_bin/python, NÃO sub/real_bin/python.
        # os.path.abspath() colapsaria textualmente para um caminho inexistente.
        real_py = self.tmp / "real_bin" / "python"
        real_py.parent.mkdir(parents=True)
        real_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real_py.chmod(0o755)
        target_dir = self.tmp / "target_dir"
        target_dir.mkdir()
        link_dir = self.tmp / "sub" / "link_dir"
        link_dir.parent.mkdir(parents=True)
        link_dir.symlink_to(target_dir)

        candidate = link_dir / ".." / "real_bin" / "python"
        result = find_vectorize_python(candidate)
        self.assertEqual(result, candidate)
        self.assertTrue(result.is_file())
        self.assertNotEqual(result, Path(os.path.abspath(candidate)))

    def test_nonexistent_tilde_user_raises_vectorize_error(self):
        bad = "~usuario_inexistente_ocre_99999/bin/python"
        with self.assertRaises(VectorizeError) as ctx_explicit:
            find_vectorize_python(bad)
        self.assertIn("explícito", str(ctx_explicit.exception))

        with mock.patch.dict(os.environ, {"OCRE_VECTORIZE_PYTHON": bad}):
            with self.assertRaises(VectorizeError) as ctx_env:
                find_vectorize_python()
        self.assertIn("OCRE_VECTORIZE_PYTHON", str(ctx_env.exception))


class RunVectorizeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.png = fake_png(self.tmp)
        self.fake_sha = sha256_file(FAKE)

    def tearDown(self):
        self._tmp.cleanup()

    def test_success_returns_primitives_and_normalized_polylines(self):
        result = run_vectorize(self.png, FAKE, self.fake_sha, self.tmp / "out")
        self.assertEqual(result["script_sha256"], self.fake_sha)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["primitives_total"], 5)
        # todas as 5 primitivas viram polilinhas: retângulo, polilinha,
        # círculo (discretizado), linha e ponto (1 vértice).
        self.assertEqual(result["polylines_total"], 5)
        self.assertEqual(result["primitives_without_points"], 0)
        types = {p["type"] for p in result["polylines"]}
        self.assertEqual(
            types, {"rectangle", "polyline", "line", "circle", "point"})
        rect = next(p for p in result["polylines"] if p["type"] == "rectangle")
        self.assertTrue(rect["closed"])
        self.assertEqual(rect["points_mm"][0], [10.0, 30.0])
        line = next(p for p in result["polylines"] if p["type"] == "line")
        self.assertEqual(line["points_mm"], [[10.0, 200.0], [100.0, 200.0]])
        circle = next(p for p in result["polylines"] if p["type"] == "circle")
        self.assertTrue(circle["closed"])
        self.assertEqual(len(circle["points_mm"]), 64)
        point = next(p for p in result["polylines"] if p["type"] == "point")
        self.assertEqual(point["points_mm"], [[200.0, 250.0]])
        self.assertEqual(result["summary"]["polilinhas"], 5)
        self.assertTrue(Path(result["json_path"]).is_file())

    def test_stamps_path_is_forwarded_and_stamps_normalized(self):
        stamps = self.tmp / "canvas_stamps.npz"
        stamps.write_bytes(b"npz fake")
        result = run_vectorize(
            self.png, FAKE, self.fake_sha, self.tmp / "out",
            stamps_path=stamps,
        )
        self.assertEqual(result["returncode"], 0)
        stamped = [p for p in result["polylines"] if "t_start_ms" in p]
        self.assertEqual(len(stamped), 5)
        for index, polyline in enumerate(result["polylines"]):
            self.assertEqual(polyline["t_start_ms"], 1000 * (index + 1))
            self.assertEqual(polyline["t_end_ms"], 1000 * (index + 1) + 500)

    def test_missing_stamps_file_is_fail_closed(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out",
                stamps_path=self.tmp / "ausente.npz",
            )

    def test_paper_mm_flags_are_forwarded(self):
        # O fake registra as dimensões recebidas no summary — prova de
        # que --paper-w-mm/--paper-h-mm chegaram ao subprocesso.
        result = run_vectorize(
            self.png, FAKE, self.fake_sha, self.tmp / "out",
            paper_mm=(297.0, 210.38),
        )
        self.assertEqual(result["summary"]["paper_mm"], [297.0, 210.38])

    def test_hash_mismatch_refuses_and_never_runs_the_script(self):
        marker = self.tmp / "marker.json"
        with self.assertRaises(VectorizeError):
            run_vectorize(
                self.png,
                FAKE,
                "0" * 64,
                self.tmp / "out",
                extra_args=["--json", str(marker)],
            )
        # O script não chegou a rodar: nenhum JSON foi escrito.
        self.assertFalse(marker.exists())

    def test_missing_png_refuses(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(self.tmp / "ausente.png", FAKE, self.fake_sha)

    def test_missing_script_refuses(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(self.png, self.tmp / "ausente.py", "0" * 64)

    def test_nonzero_exit_is_fail_closed(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out",
                extra_args=["--fail"],
            )

    def test_missing_json_output_is_fail_closed(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out",
                extra_args=["--no-json"],
            )

    def test_invalid_json_output_is_fail_closed(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out",
                extra_args=["--bad-json"],
            )

    def test_timeout_is_fail_closed(self):
        with self.assertRaises(VectorizeError):
            run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out",
                timeout_s=0.3,
                extra_args=["--sleep", "5"],
            )

    def test_output_dir_is_created_when_missing(self):
        out_dir = self.tmp / "novo" / "sub"
        result = run_vectorize(self.png, FAKE, self.fake_sha, out_dir)
        self.assertTrue(out_dir.is_dir())
        self.assertEqual(result["polylines_total"], 5)

    def _capturing_subprocess(self, captured: dict):
        """Subprocesso fake: grava o JSON esperado e devolve sucesso."""

        def fake_run(command, **kwargs):
            captured["argv0"] = command[0]
            json_path = Path(command[command.index("--json") + 1])
            json_path.write_text(
                json.dumps({"summary": {}, "primitives": []}),
                encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0, stdout="", stderr="")

        return mock.patch("subprocess.run", side_effect=fake_run)

    def test_argv0_is_the_explicit_vectorize_python(self):
        fake = self.tmp / "vectorize-python"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        captured: dict = {}
        with self._capturing_subprocess(captured):
            result = run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out",
                vectorize_python=fake)
        expected = os.path.abspath(fake)
        self.assertEqual(captured["argv0"], expected)
        self.assertEqual(result["python_path"], expected)

    def test_argv0_is_the_env_vectorize_python(self):
        env_python = self.tmp / "env-python"
        env_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        env_python.chmod(0o755)
        captured: dict = {}
        with mock.patch.dict(
            os.environ, {"OCRE_VECTORIZE_PYTHON": str(env_python)}
        ), self._capturing_subprocess(captured):
            result = run_vectorize(
                self.png, FAKE, self.fake_sha, self.tmp / "out")
        expected = os.path.abspath(env_python)
        self.assertEqual(captured["argv0"], expected)
        self.assertEqual(result["python_path"], expected)

    def test_missing_vectorize_python_refuses_and_never_runs(self):
        captured: dict = {}
        with self._capturing_subprocess(captured):
            with self.assertRaises(VectorizeError):
                run_vectorize(
                    self.png, FAKE, self.fake_sha, self.tmp / "out",
                    vectorize_python=self.tmp / "ausente")
        self.assertNotIn("argv0", captured)

    def test_python_path_defaults_to_sys_executable(self):
        result = run_vectorize(self.png, FAKE, self.fake_sha, self.tmp / "out")
        self.assertEqual(result["python_path"], os.path.abspath(sys.executable))


if __name__ == "__main__":
    unittest.main()
