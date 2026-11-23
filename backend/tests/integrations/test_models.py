import math

import pytest

from ocre_core.integrations.models import (
    AdapterMode,
    CapabilityManifest,
    LicenseStatus,
    Maturity,
    ResourceRequirements,
    SoftwareIdentity,
)


def manifest(**overrides):
    fields = dict(
        adapter_id="adapter",
        adapter_version="1.0",
        contract_version="1.0",
        software=SoftwareIdentity("solver", "1.0"),
        mode=AdapterMode.BATCH,
        supported_analyses=("axial",),
        input_formats=("eir",),
        result_formats=("vtk",),
        request_schema_versions=("0.2",),
        result_schema_versions=("0.1",),
        resource_requirements=ResourceRequirements(),
        license_status=LicenseStatus.FREE,
        maturity=Maturity.EXPERIMENTAL,
    )
    return CapabilityManifest(**(fields | overrides))


@pytest.mark.parametrize("field", ("minimum_cpu_cores", "minimum_memory_bytes"))
@pytest.mark.parametrize("bad", (0, -3, 1.5, math.nan, math.inf, True))
def test_resource_minimums_require_positive_integers(field, bad):
    with pytest.raises(ValueError, match="positive integer"):
        manifest(
            resource_requirements=ResourceRequirements(**{field: bad}),
        )


@pytest.mark.parametrize("field", ("minimum_cpu_cores", "minimum_memory_bytes"))
def test_resource_minimums_accept_positive_integers(field):
    manifest(resource_requirements=ResourceRequirements(**{field: 2}))
