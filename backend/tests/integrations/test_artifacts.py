import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ocre_core.integrations.artifacts import UnsafeArtifactPath, verify_artifact


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "inputs").mkdir()
        (self.root / "inputs" / "model.dat").write_bytes(b"immutable fixture")
        self.digest = hashlib.sha256(b"immutable fixture").hexdigest()

    def test_verifies_confined_regular_file_and_digest(self):
        result = verify_artifact(self.root, "inputs/model.dat", self.digest)
        self.assertEqual(result, self.root / "inputs" / "model.dat")

    def test_rejects_absolute_parent_and_hash_mismatch(self):
        for path in ("/etc/passwd", "../outside", "inputs/../../outside"):
            with self.subTest(path=path), self.assertRaises(UnsafeArtifactPath):
                verify_artifact(self.root, path, self.digest)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            verify_artifact(self.root, "inputs/model.dat", "0" * 64)

    def test_rejects_symlink_components_even_when_target_is_inside_root(self):
        (self.root / "linked").symlink_to(self.root / "inputs", target_is_directory=True)
        with self.assertRaises(UnsafeArtifactPath):
            verify_artifact(self.root, "linked/model.dat", self.digest)


if __name__ == "__main__":
    unittest.main()
