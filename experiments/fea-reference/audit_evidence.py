#!/usr/bin/env python3
"""Read-only integrity audit for OCRE's structural benchmark manifests.

This tool checks recorded file paths and SHA-256 digests. It does not run a
solver, rewrite manifests, or modify benchmark evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from ocre_core.provenance.sidecar import audit_sidecar  # noqa: E402

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AuditError(Exception):
    """A malformed manifest or unsafe audit target."""


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"manifest unreadable or invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError("manifest root must be a JSON object")
    return value


def safe_file(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise AuditError("artifact path must be a non-empty relative POSIX path")
    if "\\" in relative or "\x00" in relative:
        raise AuditError(f"artifact path is not a valid POSIX relative path: {relative!r}")
    raw = PurePosixPath(relative)
    if raw.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise AuditError(f"artifact path must be normalized and relative: {relative!r}")
    candidate = root.joinpath(*raw.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AuditError(f"artifact file is missing or unresolvable: {relative!r}: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AuditError(f"artifact path escapes evidence root: {relative!r}") from exc
    if not resolved.is_file():
        raise AuditError(f"artifact is not a regular file: {relative!r}")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_check(root: Path, relative: Any, expected: Any, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        return None, f"{label}: expected SHA-256 must be 64 hexadecimal characters"
    try:
        path = safe_file(root, relative)
        actual = sha256_file(path)
    except (AuditError, OSError) as exc:
        return None, f"{label}: {exc}"
    if actual.lower() != expected.lower():
        return None, f"{label}: SHA-256 mismatch for {relative!r} (expected {expected.lower()}, got {actual})"
    return {"label": label, "path": relative, "sha256": actual}, None


def required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuditError(f"{label} must be an object")
    return value


def audit_solver_runs(root: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    runs = manifest.get("runs")
    if not isinstance(runs, list) or not runs:
        raise AuditError("solver-runs manifest must contain a non-empty 'runs' array")

    for index, raw_run in enumerate(runs):
        run = required_object(raw_run, f"runs[{index}]")
        run_dir = run.get("run_directory")
        deck = run.get("deck")
        if not isinstance(run_dir, str) or not isinstance(deck, str):
            raise AuditError(f"runs[{index}] requires string 'run_directory' and 'deck'")
        try:
            run_path = safe_file(root, run_dir + "/" + deck)
        except AuditError as exc:
            errors.append(f"runs[{index}].deck: {exc}")
            run_path = None

        # Output artifact map: filename -> SHA-256 under the run directory.
        artifacts = run.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise AuditError(f"runs[{index}].artifacts must be a non-empty object")
        for name, digest in sorted(artifacts.items()):
            try:
                if not isinstance(name, str) or not name or PurePosixPath(name).name != name or name in (".", ".."):
                    raise AuditError(f"artifact filename must be a single path component: {name!r}")
                rel = run_dir + "/" + name
                result, error = hash_check(root, rel, digest, f"runs[{index}].artifacts[{name!r}]")
                if error:
                    errors.append(error)
                elif result:
                    checks.append(result)
            except AuditError as exc:
                errors.append(f"runs[{index}].artifacts[{name!r}]: {exc}")

        source = run.get("source_artifacts")
        if not isinstance(source, dict):
            raise AuditError(f"runs[{index}].source_artifacts must be an object")
        source_pairs = (
            ("analysis_deck_sha256", deck, "analysis deck"),
            ("run_input_sha256", deck, "run input deck"),
            ("mesh_input_sha256", source.get("mesh_input"), "mesh input"),
            ("geometry_sha256", source.get("geometry"), "geometry"),
        )
        for digest_key, rel, label in source_pairs:
            if digest_key not in source:
                errors.append(f"runs[{index}].source_artifacts missing {digest_key}")
                continue
            result, error = hash_check(root, rel, source[digest_key], f"runs[{index}].source_artifacts.{label}")
            if error:
                errors.append(error)
            elif result:
                checks.append(result)

    return checks, errors


def audit_prepared_manifest(root: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    artifacts = manifest.get("mesh_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise AuditError("prepared manifest must contain a non-empty 'mesh_artifacts' array")
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_item in enumerate(artifacts):
        item = required_object(raw_item, f"mesh_artifacts[{index}]")
        for path_key, digest_key in (
            ("geometry", "geometry_sha256"),
            ("mesh_input", "mesh_sha256"),
            ("analysis_input", "analysis_sha256"),
        ):
            if path_key not in item or digest_key not in item:
                errors.append(f"mesh_artifacts[{index}] requires {path_key} and {digest_key}")
                continue
            result, error = hash_check(root, item[path_key], item[digest_key], f"mesh_artifacts[{index}].{path_key}")
            if error:
                errors.append(error)
            elif result:
                checks.append(result)
    return checks, errors


def audit_manifest(root: Path, manifest: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    if "sidecar_version" in manifest:
        report = audit_sidecar(root, manifest)
        checks = report["checks"]
        errors = [
            f"{item['state'].upper()}: {item['code']}: {item['message']}"
            for item in report["issues"]
        ]
        if report["status"] != "complete" and not errors:
            errors.append(f"{report['status'].upper()}: sidecar evidence is not complete")
        return "cad_mesh_provenance_sidecar", checks, errors
    runs = manifest.get("runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], dict) and "run_directory" in runs[0]:
        checks, errors = audit_solver_runs(root, manifest)
        return "solver_runs_manifest", checks, errors
    if isinstance(manifest.get("mesh_artifacts"), list):
        checks, errors = audit_prepared_manifest(root, manifest)
        return "prepared_manifest", checks, errors
    raise AuditError("unsupported manifest shape; expected solver runs or prepared mesh_artifacts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="manifest JSON file to audit")
    parser.add_argument("--evidence-root", required=True, type=Path,
                        help="root directory; all referenced evidence files must resolve inside it")
    args = parser.parse_args(argv)

    try:
        root = args.evidence_root.resolve(strict=True)
        if not root.is_dir():
            raise AuditError("evidence root must be a directory")
        manifest_path = args.manifest.resolve(strict=True)
        try:
            manifest_path.relative_to(root)
        except ValueError as exc:
            raise AuditError("manifest file must be inside the evidence root") from exc
        manifest = load_manifest(manifest_path)
        kind, checks, errors = audit_manifest(root, manifest)
    except (AuditError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for error in sorted(errors):
        print(f"ERROR: {error}")
    if errors:
        print(f"FAIL: {kind}: {len(errors)} error(s), {len(checks)} file hash(es) verified")
        return 1
    print(f"OK: {kind}: {len(checks)} file hash(es) verified under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
