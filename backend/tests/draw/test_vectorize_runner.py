"""Testes do runner black-box do vectorize (F0).

Ressalva explícita: todos os testes usam a FIXTURE
``fixtures/fake_vectorize.py`` — um script que IMITA a CLI do
``tools/vectorize.py`` real e devolve um payload fixo. Nenhum teste
aqui roda o vectorize real do Draw nem um PNG real; a bateria real
G0.1/G0.2 (20 folhas) NÃO foi rodada.
"""

import hashlib
import sys
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

    def test_circle_has_no_points_and_counts_as_without(self):
        polylines, without = normalize_primitives(
            [{"type": "circle", "cx_mm": 1.0, "cy_mm": 2.0, "r_mm": 3.0}]
        )
        self.assertEqual(polylines, [])
        self.assertEqual(without, 1)


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
        self.assertEqual(result["primitives_total"], 4)
        # retângulo + polilinha + linha têm pontos; o círculo não tem.
        self.assertEqual(result["polylines_total"], 3)
        self.assertEqual(result["primitives_without_points"], 1)
        types = {p["type"] for p in result["polylines"]}
        self.assertEqual(types, {"rectangle", "polyline", "line"})
        rect = next(p for p in result["polylines"] if p["type"] == "rectangle")
        self.assertTrue(rect["closed"])
        self.assertEqual(rect["points_mm"][0], [10.0, 30.0])
        self.assertEqual(result["summary"]["polilinhas"], 4)
        self.assertTrue(Path(result["json_path"]).is_file())

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
        self.assertEqual(result["polylines_total"], 3)


if __name__ == "__main__":
    unittest.main()
