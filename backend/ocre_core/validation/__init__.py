"""Deterministic, read-only EIR v0.2 validation and pre-flight checks."""

from .validator import ValidationReport, validate

__all__ = ["ValidationReport", "validate"]
