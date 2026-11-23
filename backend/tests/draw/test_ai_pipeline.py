"""End-to-end contract for the human-gated AI -> CAD beta path.

Uses the real vectorize subprocess protocol and real build123d STEP worker;
only the ModelAdapter is fake. It intentionally exercises both invocations
against one immutable document and a persistent proposal ledger.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from draw.png_helper import write_png
from ocre_core.draw.step_export import find_step_worker_python
from ocre_core.draw.document import document_sha256

ROOT = Path(__file__).resolve().parents[3]


class AIPipelineIntegrationTests(unittest.TestCase):
    def test_two_pass_fake_provider_and_real_step_worker(self):
        if find_step_worker_python() is None:
            self.skipTest("build123d STEP worker is not installed")
        temp_root = ROOT / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp:
            base = Path(temp)
            png = write_png(base / "canvas.png")
            vectorizer = base / "fake_vectorizer.py"
            vectorizer.write_text(
                """import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("png")
parser.add_argument("--json")
parser.add_argument("--out")
args, _ = parser.parse_known_args()
with open(args.json, "w", encoding="utf-8") as output:
    json.dump({"primitives": [{"type": "rectangle", "corners_mm": [
        [10, 10], [30, 10], [30, 20], [10, 20]
    ]}]}, output)
"""
            )
            provider = base / "fake_provider.py"
            provider.write_text(
                """from ocre_core.draw.ai.interfaces import Classification, MaterialAdvice

class Fake:
    name = "fixture"

    def classify(self, context):
        stroke = context.stable_strokes()[0]
        return [Classification(stroke["stroke_id"], "plate", 0.99, "fixture")]

    def advise_material(self, context):
        stroke = context.stable_strokes()[0]
        return [MaterialAdvice(stroke["stroke_id"], "steel", "fixture")]

    def check_coherence(self, context):
        return []

    def check_conversion(self, context):
        return [{"kind": "fixture-advisory", "detail": "human review"}]

def create():
    return Fake()
"""
            )
            out = base / "run"
            ledger = base / "proposals.json"
            common = [sys.executable, "-m", "ocre_core.draw.cli", "beta-run",
                      "--png", str(png), "--vectorize-script", str(vectorizer),
                      "--vectorize-sha256", hashlib.sha256(vectorizer.read_bytes()).hexdigest(),
                      "--out-dir", str(out), "--origin", "upstream-canvas-png",
                      "--draw-repo", "fixture", "--draw-commit", "a" * 40,
                      "--thickness-mm", "2", "--ai-adapter-factory", "fake_provider:create",
                      "--proposal-file", str(ledger), "--cad-only"]
            env = dict(os.environ, PYTHONPATH=f"{ROOT / 'backend'}:{base}")
            first = subprocess.run(common, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
            first_report = json.loads(first.stdout)
            doc_path = out / "export" / "document.json"
            self.assertTrue(doc_path.is_file(), first.stdout)
            self.assertTrue(ledger.is_file(), first.stdout)
            document_bytes_before = doc_path.read_bytes()
            document_hash_before = hashlib.sha256(document_bytes_before).hexdigest()
            document = json.loads(doc_path.read_text())
            ledger_data = json.loads(ledger.read_text())
            self.assertEqual(ledger_data["document_sha256"], document_sha256(document))
            proposal = next(p for p in ledger_data["proposals"] if p["kind"] == "material-advice")
            second = [sys.executable, "-m", "ocre_core.draw.cli", "beta-run",
                      *[x for x in common[4:] if x not in {"--ai-adapter-factory", "fake_provider:create"}],
                      "--document", str(doc_path), "--confirm-proposal", proposal["proposal_id"],
                      "--confirmed-by", "pessoa"]
            # The rerun deliberately omits the provider: confirmed ledger data must be sufficient.
            second_run = subprocess.run(second, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(second_run.returncode, 0, second_run.stdout + second_run.stderr)
            report = json.loads(second_run.stdout)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(hashlib.sha256(doc_path.read_bytes()).hexdigest(), document_hash_before)
            self.assertEqual(doc_path.read_bytes(), document_bytes_before)
            self.assertTrue((out / "step" / "drawing.step").is_file())
            self.assertEqual(report["steps"][0]["id"], "export")
            self.assertTrue(all(x["id"] not in {"fea", "physics", "viz", "fmu"} or x["status"] == "not-run" for x in report["steps"]))
            step_manifest = json.loads((out / "step" / "drawing.step.manifest.json").read_text())
            self.assertEqual(step_manifest["material"]["value"], "steel")
            self.assertEqual(step_manifest["step"]["sha256"], report["steps"][2]["detail"]["step"]["output_sha256"])
            observations = [step.get("detail", {}).get("observation") for step in report["steps"]]
            self.assertEqual(observations[0]["status"], "unavailable")
            self.assertEqual(observations[2]["status"], "unavailable")
            published = {Path(item["path"]).name for item in report["artifacts"]}
            self.assertIn("drawing.step", published)
            self.assertIn("drawing.step.manifest.json", published)

            first_observations = [step.get("detail", {}).get("observation") for step in first_report["steps"]]
            self.assertEqual(first_observations[0]["status"], "observed")
