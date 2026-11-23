"""Regression tests for experiments/fea-reference/audit_evidence.py."""
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[2] / "experiments" / "fea-reference" / "audit_evidence.py"
spec = importlib.util.spec_from_file_location("audit_evidence", AUDIT)
audit_evidence = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit_evidence
spec.loader.exec_module(audit_evidence)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AuditEvidenceTests(unittest.TestCase):
    def make_solver_run_root(self, tmp_path):
        """Evidence root with one run whose deck copy differs from the source deck."""
        root = tmp_path / "runs"
        run_dir = root / "solver_runs" / "deck_a" / "attempt_001"
        run_dir.mkdir(parents=True)
        (root / "axial_bar_gmsh_deck_h1mm.inp").write_bytes(b"DECK-ORIGINAL")
        (run_dir / "axial_bar_gmsh_deck_h1mm.inp").write_bytes(b"DECK-TAMPERED-COPY")
        (root / "mesh.inp").write_bytes(b"MESH")
        (root / "geo.geo").write_bytes(b"GEO")
        (run_dir / "out.dat").write_bytes(b"OUT")
        manifest = {
            "status": "solver_runs_completed",
            "runs": [{
                "deck": "axial_bar_gmsh_deck_h1mm.inp",
                "run_directory": "solver_runs/deck_a/attempt_001",
                "artifacts": {"out.dat": sha256(run_dir / "out.dat")},
                "source_artifacts": {
                    "analysis_deck_sha256": sha256(root / "axial_bar_gmsh_deck_h1mm.inp"),
                    "run_input_sha256": sha256(root / "axial_bar_gmsh_deck_h1mm.inp"),
                    "mesh_input": "mesh.inp",
                    "mesh_input_sha256": sha256(root / "mesh.inp"),
                    "geometry": "geo.geo",
                    "geometry_sha256": sha256(root / "geo.geo"),
                },
            }],
        }
        return root, manifest, run_dir

    def test_run_input_digest_is_verified_against_the_run_directory_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root, manifest, run_dir = self.make_solver_run_root(Path(td))
            # The recorded digest equals the source deck's, but the copy inside
            # the run directory was tampered with after the fact.
            _, checks, errors = audit_evidence.audit_manifest(root, manifest)
            self.assertTrue(
                any("run input deck" in error for error in errors),
                f"tampered run-directory copy must fail run_input_sha256: {errors}",
            )
            # A byte-identical copy audits clean.
            (run_dir / "axial_bar_gmsh_deck_h1mm.inp").write_bytes(b"DECK-ORIGINAL")
            _, checks, errors = audit_evidence.audit_manifest(root, manifest)
            self.assertEqual(errors, [])
            self.assertTrue(any(item["path"].startswith("solver_runs/") for item in checks))

    def test_prepared_manifest_accepts_baseline_input_sha256_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "axial_bar_h5mm.inp").write_bytes(b"BASELINE-DECK")
            manifest = {"mesh_artifacts": [{
                "element_type": "C3D8",
                "input": "axial_bar_h5mm.inp",
                "sha256": sha256(root / "axial_bar_h5mm.inp"),
            }]}
            kind, checks, errors = audit_evidence.audit_manifest(root, manifest)
            self.assertEqual(kind, "prepared_manifest")
            self.assertEqual(errors, [])
            self.assertEqual(len(checks), 1)
            (root / "axial_bar_h5mm.inp").write_bytes(b"BASELINE-TAMPERED")
            _, _, errors = audit_evidence.audit_manifest(root, manifest)
            self.assertTrue(any("SHA-256 mismatch" in error for error in errors))

    def test_prepared_manifest_still_requires_gmsh_artifact_triple(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "geo.geo").write_bytes(b"GEO")
            manifest = {"mesh_artifacts": [{
                "geometry": "geo.geo",
                "geometry_sha256": sha256(root / "geo.geo"),
            }]}
            _, _, errors = audit_evidence.audit_manifest(root, manifest)
            self.assertTrue(any("mesh_input" in error for error in errors))
            self.assertTrue(any("analysis_input" in error for error in errors))

    def test_prepared_manifest_rejects_mixed_single_deck_and_triple_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "deck.inp").write_bytes(b"DECK")
            (root / "geo.geo").write_bytes(b"GEO")
            manifest = {"mesh_artifacts": [{
                "input": "deck.inp",
                "sha256": sha256(root / "deck.inp"),
                "geometry": "geo.geo",
                "geometry_sha256": sha256(root / "geo.geo"),
                "mesh_input": "mesh.inp",
                "mesh_sha256": "0" * 64,
                "analysis_input": "a.inp",
                "analysis_sha256": "0" * 64,
            }]}
            _, _, errors = audit_evidence.audit_manifest(root, manifest)
            self.assertTrue(any("mixes" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
