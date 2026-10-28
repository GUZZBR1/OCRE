"""Confinement and integrity checks for files referenced by input manifests."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


class UnsafeArtifactPath(ValueError):
    pass


def verify_artifact(root: Path, relative_path: str, expected_sha256: str) -> Path:
    """Resolve a manifest path under root, reject symlinks, and verify its hash.

    Callers should run this against an immutable staged snapshot immediately
    before process launch. It does not make the file immutable after checking.
    """
    relative = PurePosixPath(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise UnsafeArtifactPath("artifact path must be a non-empty relative path without '..'")
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        raise ValueError("expected_sha256 must be lowercase SHA-256 hex")

    trusted_root = root.resolve(strict=True)
    candidate = trusted_root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise UnsafeArtifactPath("artifact path must not traverse symlinks")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(trusted_root)
    except ValueError as exc:
        raise UnsafeArtifactPath("artifact path escapes bundle root") from exc
    if not resolved.is_file():
        raise UnsafeArtifactPath("artifact path must reference a regular file")

    digest = hashlib.sha256()
    with resolved.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("artifact SHA-256 mismatch")
    return resolved
