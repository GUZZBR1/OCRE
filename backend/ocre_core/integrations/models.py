"""Small immutable value types shared by adapter implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AdapterMode(str, Enum):
    BATCH = "batch"
    SESSION = "session"
    STREAM = "stream"
    FILE_TRANSLATOR = "file_translator"
    GATEWAY = "gateway"


class Maturity(str, Enum):
    EXPERIMENTAL = "experimental"
    VALIDATED = "validated"


class LicenseStatus(str, Enum):
    FREE = "free"
    COMMERCIAL = "commercial"
    REVIEW_REQUIRED = "review_required"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class SoftwareIdentity:
    name: str
    version: str
    binary_sha256: str | None = None


@dataclass(frozen=True)
class ResourceRequirements:
    operating_systems: tuple[str, ...] = ()
    cpu_architectures: tuple[str, ...] = ()
    minimum_cpu_cores: int | None = None
    minimum_memory_bytes: int | None = None
    gpu_required: bool = False
    network_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityManifest:
    adapter_id: str
    adapter_version: str
    contract_version: str
    software: SoftwareIdentity
    mode: AdapterMode
    supported_analyses: tuple[str, ...]
    input_formats: tuple[str, ...]
    result_formats: tuple[str, ...]
    request_schema_versions: tuple[str, ...]
    result_schema_versions: tuple[str, ...]
    resource_requirements: ResourceRequirements
    license_status: LicenseStatus
    maturity: Maturity
    limitations: tuple[str, ...] = ()
    validation_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("adapter_id", "adapter_version", "contract_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        if not self.software.name.strip() or not self.software.version.strip():
            raise ValueError("software name and version must not be empty")
        digest = self.software.binary_sha256
        if digest is not None and (
            len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("binary_sha256 must be a lowercase SHA-256 hex digest")
        if self.resource_requirements.minimum_cpu_cores is not None and self.resource_requirements.minimum_cpu_cores < 1:
            raise ValueError("minimum_cpu_cores must be positive")
        if self.resource_requirements.minimum_memory_bytes is not None and self.resource_requirements.minimum_memory_bytes < 1:
            raise ValueError("minimum_memory_bytes must be positive")
