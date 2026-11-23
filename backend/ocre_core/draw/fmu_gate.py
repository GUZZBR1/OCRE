"""Gate FMPy do beta v0.1 OCRE<->Draw (FMU, passo de relatório).

O beta v0.1 NÃO tem FMU: não há modelo semântico declarado nem
arquivo ``.fmu`` no beta (ficha ``formatos.md`` + decisão do Senado:
"FMU só com modelo semântico declarado"). Este gate existe para que a
cadeia ``beta-run`` registre o passo honestamente, em vez de fingir
que validou um FMU ou de omitir o passo:

- sem ``--fmu`` (o caso do beta): ``status == "not-triggered"`` com o
  motivo — nunca dispara ponta a ponta sem FMU;
- com um caminho que não existe: ``status == "blocked"`` (fail-closed,
  nunca estimado);
- com um FMU mas sem FMPy instalado neste venv: ``status ==
  "unavailable"`` (skip honesto);
- com FMU + FMPy: lê a descrição do modelo
  (``read_model_description``) e reporta ``pass``/``fail`` sem rodar
  simulação — a simulação do FMU é trabalho futuro, não deste gate.

O domínio do fingerprint onde citado é
``DRAW:PAPER-APPROVAL:FINGERPRINT`` (errata do Senado).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["GATE_ID", "validate_fmu"]

GATE_ID = "fmu-fmpy"
FINGERPRINT_DOMAIN = "DRAW:PAPER-APPROVAL:FINGERPRINT"


def validate_fmu(
    fmu_path: str | Path | None = None,
) -> dict[str, Any]:
    """Valida um FMU com FMPy, ou reporta honestamente por que não.

    Sem FMU no beta, devolve ``not-triggered`` — nunca dispara ponta
    a ponta sem FMU. Nunca levanta exceção por falta de FMU/FMPy: a
    ausência é um status do relatório, não um crash. Nada aqui roda
    sob timeout (a leitura da descrição é em processo); quando a
    simulação do FMU existir, ela ganha o seu próprio timeout.
    """
    base: dict[str, Any] = {
        "gate": GATE_ID,
        "fingerprint_domain": FINGERPRINT_DOMAIN,
        "fmu_path": str(fmu_path) if fmu_path is not None else None,
    }
    if fmu_path is None:
        return {
            **base,
            "status": "not-triggered",
            "available": False,
            "reason": "beta v0.1 has no FMU: no declared semantic model "
            "and no .fmu file in the beta (FMU only with a declared "
            "semantic model); the gate does not run end-to-end "
            "without an FMU",
        }
    path = Path(fmu_path)
    if not path.is_file():
        return {
            **base,
            "status": "blocked",
            "available": False,
            "reason": f"FMU file not found: {path} (fail-closed, "
            "never estimated)",
        }
    try:
        import fmpy  # noqa: F401
    except ImportError:
        return {
            **base,
            "status": "unavailable",
            "available": False,
            "reason": "FMPy is not installed in this venv; the FMU "
            "was not validated (honest skip, not an approval)",
        }
    try:
        from fmpy import read_model_description

        description = read_model_description(str(path))
    except Exception as exc:  # noqa: BLE001 - FMPy errors are a fail verdict
        return {
            **base,
            "status": "fail",
            "available": True,
            "reason": f"FMPy could not read the FMU model description: "
            f"{type(exc).__name__}: {exc}",
        }
    return {
        **base,
        "status": "pass",
        "available": True,
        "model_name": getattr(description, "modelName", None),
        "fmi_version": getattr(description, "fmiVersion", None),
        "problems": [],
        "reason": "FMPy read the model description (no FMU simulation "
        "was run; simulation is future work)",
    }
