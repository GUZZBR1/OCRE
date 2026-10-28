"""Experimental local CAD-to-mesh provenance sidecars.

This package is deliberately independent of EIR. Its first supported source
kind is a synthetic ``.geo`` primitive; it does not represent imported CAD.
"""

from .sidecar import audit_sidecar, build_sidecar, sha256_file

__all__ = ["audit_sidecar", "build_sidecar", "sha256_file"]
