"""Testes do runner black-box do vectorize (F0).

Ressalva explícita: todos os testes usam a FIXTURE
``fixtures/fake_vectorize.py`` — um script que IMITA a CLI do
``src/draw/exporters/vectorize.py`` real e devolve um payload fixo. Nenhum teste
aqui roda o vectorize real do Draw nem um PNG real; a bateria real
G0.1/G0.2 (20 folhas) NÃO foi rodada.
"""

import hashlib
import math
import tempfile
import unittest
from pathlib import Path

from ocre_core.draw.vectorize_runner import (
    VectorizeError,
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


if __name__ == "__main__":
    unittest.main()
