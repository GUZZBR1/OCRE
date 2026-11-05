"""Gate G0.2 — verificação de DXF com o LibreCAD via subprocesso (Fase 0).

O LibreCAD é GPL-2.0: ele é usado AQUI SOMENTE como binário externo,
via subprocesso, nunca linkado ou importado (ressalva V3 da aprovação
do Senado). Nenhum código do LibreCAD entra no OCRE e nenhum módulo
Python dele é importado.

O gate roda ``librecad dxf2svg <dxf> <svg>`` com
``QT_QPA_PLATFORM=offscreen`` (sem display), timeout obrigatório e
stdout/stderr capturados. Passa se o processo sai com código 0 e o SVG
de saída existe e não está vazio.

Se o binário NÃO existir na máquina, o gate devolve
``status == "unavailable"`` — um skip honesto, nunca um passo fingido.
O gate G0.2 da bateria real (10/10 DXFs abrindo no LibreCAD) só é
"pass" quando o binário existe e todos os arquivos passam; sem o
binário, a bateria fica "não rodada", não "aprovada".

Forma do CLI: ``librecad dxf2svg|dxf2pdf|dxf2png|dxf2dwg``. A forma
com arquivo de saída explícito é tentada primeiro; se o LibreCAD
recusar os argumentos (código != 0 e stderr de uso), tenta-se uma vez
a forma sem saída explícita (o LibreCAD escreve ao lado da entrada).
Qual tentativa funcionou fica registrada em ``result["attempt"]``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "LibreCADGateError",
    "librecad_available",
    "check_dxf",
    "gate_batch",
]

DEFAULT_TIMEOUT_S = 120
_BINARY = "librecad"


class LibreCADGateError(RuntimeError):
    """Erro do gate que não é um simples fail do arquivo (ex.: timeout)."""


def librecad_available(binary: str = _BINARY) -> str | None:
    """Devolve o caminho do binário ``librecad`` ou ``None`` se ausente."""
    return shutil.which(binary)


def _run_conversion(
    binary: str, dxf_path: Path, out_path: Path | None, timeout_s: float
) -> dict:
    """Uma tentativa de conversão; devolve dict com returncode/stdout/stderr.

    Com ``out_path`` dado: ``librecad dxf2svg <dxf> <svg>``. Sem ele:
    ``librecad dxf2svg <dxf>`` e o LibreCAD escreve ``<dxf>.svg`` ao
    lado da entrada.
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    command = [binary, "dxf2svg", str(dxf_path)]
    if out_path is not None:
        command.append(str(out_path))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LibreCADGateError(
            f"librecad excedeu o timeout de {timeout_s}s em {dxf_path}"
        ) from exc
    return {
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }


def check_dxf(
    dxf_path: str | Path,
    *,
    binary: str = _BINARY,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Verifica um DXF com o LibreCAD. Nunca finge um passo.

    Retorno (sempre um dict, nunca exceção, exceto timeout/IO grave):
    - ``{"status": "pass", ...}`` — abriu e converteu sem erro.
    - ``{"status": "fail", ...}`` — binário existe mas a conversão falhou.
    - ``{"status": "unavailable", ...}`` — binário ausente (skip honesto).
    """
    dxf_path = Path(dxf_path)
    if not dxf_path.is_file():
        raise LibreCADGateError(f"DXF não encontrado: {dxf_path}")
    binary_path = librecad_available(binary)
    if binary_path is None:
        return {
            "status": "unavailable",
            "available": False,
            "dxf_path": str(dxf_path),
            "binary": binary,
            "reason": f"binário '{binary}' não encontrado no PATH; "
            "gate não rodado (skip honesto, não é aprovação)",
        }

    with tempfile.TemporaryDirectory(prefix="ocre-librecad-") as tmp:
        out_path = Path(tmp) / (dxf_path.stem + ".svg")
        attempt = _run_conversion(binary_path, dxf_path, out_path, timeout_s)
        used_attempt = 1
        if attempt["returncode"] != 0 or not out_path.is_file():
            # Segunda forma: sem arquivo de saída explícito; o LibreCAD
            # escreve <entrada>.svg ao lado da entrada (numa cópia, para
            # não sujar o diretório do DXF original).
            copy_path = Path(tmp) / dxf_path.name
            copy_path.write_bytes(dxf_path.read_bytes())
            fallback_out = Path(tmp) / (dxf_path.stem + ".svg")
            attempt = _run_conversion(binary_path, copy_path, None, timeout_s)
            out_path = fallback_out
            used_attempt = 2
        if attempt["returncode"] != 0:
            return {
                "status": "fail",
                "available": True,
                "dxf_path": str(dxf_path),
                "binary": binary_path,
                "attempt": used_attempt,
                "returncode": attempt["returncode"],
                "stdout": attempt["stdout"],
                "stderr": attempt["stderr"],
                "reason": "librecad saiu com código != 0",
            }
        if not out_path.is_file() or out_path.stat().st_size == 0:
            return {
                "status": "fail",
                "available": True,
                "dxf_path": str(dxf_path),
                "binary": binary_path,
                "attempt": used_attempt,
                "returncode": attempt["returncode"],
                "stdout": attempt["stdout"],
                "stderr": attempt["stderr"],
                "reason": "librecad saiu com código 0 mas não produziu SVG",
            }
        return {
            "status": "pass",
            "available": True,
            "dxf_path": str(dxf_path),
            "binary": binary_path,
            "attempt": used_attempt,
            "returncode": 0,
            "svg_bytes": out_path.stat().st_size,
            "stdout": attempt["stdout"],
            "stderr": attempt["stderr"],
        }


def gate_batch(
    dxf_paths: list[str | Path],
    *,
    binary: str = _BINARY,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Roda o gate G0.2 sobre vários DXFs e resume o lote.

    ``verdict`` é ``"pass"`` só se todos passaram; ``"unavailable"`` se
    o binário não existe (bateria não rodada); ``"fail"`` nos demais
    casos. O resumo nunca transforma indisponibilidade em aprovação.
    """
    results = [check_dxf(path, binary=binary, timeout_s=timeout_s) for path in dxf_paths]
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    unavailable = sum(1 for r in results if r["status"] == "unavailable")
    if unavailable:
        verdict = "unavailable"
    elif failed:
        verdict = "fail"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "unavailable": unavailable,
        "results": results,
    }
