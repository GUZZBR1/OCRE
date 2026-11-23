"""Runner black-box de ``santleme/draw-core`` ``src/draw/exporters/vectorize.py`` (Fase 0).

Roda o script de vetorização do Draw como subprocesso, sem copiar o
script para o OCRE e sem importar nada dele. O script vive no Draw
(``src/draw/exporters/vectorize.py``, CLI verificada por leitura em 2026-11-05)::

    python src/draw/exporters/vectorize.py canvas.png --json saida.json
        [--out saida.svg] [--clean] [--stamps canvas_stamps.npz]
        [--primitives] [--epsilon 1.5] [--thinning zhangsuen|guohall]
        [--stats]

Entrada: PNG da camada confirmada (``captures/canvas_<epoch>.png``).
Saída: JSON com ``{"summary": {...}, "primitives": [...]}`` — cada
primitiva traz ``type`` (``point|line|circle|rectangle|polygon|polyline``)
e pontos em mm do papel com y-down (mesma orientação do documento
``draw.paper.v1``): ``points_mm`` (ou ``corners_mm`` no retângulo,
``x1_mm``..``y2_mm`` na linha, ``x_mm``/``y_mm`` no ponto). O círculo
chega só como ``cx_mm``/``cy_mm``/``r_mm`` e é discretizado aqui em
``CIRCLE_SEGMENTS`` segmentos (polilinha fechada). Quando o script roda
com ``--stamps``, cada primitiva pode trazer ``t_start_ms``/``t_end_ms``,
preservados na normalização.

Garantias (fail-closed):

- O sha256 do script é conferido ANTES de rodar; divergência recusa a
  execução (o Draw é alvo móvel — B8 do plano — e o pin protege contra
  rodar uma versão diferente da registrada no documento/sidecar).
- Timeout obrigatório (default 300 s, configurável); sem shell; cwd
  isolado (o diretório de saída); stdout/stderr capturados.
- Exit != 0, JSON ausente ou JSON sem a lista ``primitives`` => erro,
  nunca resultado parcial silencioso.

Este módulo NÃO converte primitivas em traços do documento — isso é
trabalho de ``document.py`` (outro implementador). Ele só normaliza a
lista de primitivas para polilinhas com ``points_mm``; primitivas sem
geometria reconhecida ficam fora de ``polylines`` e são contadas em
``primitives_without_points``.

Nada aqui depende da camada de IA (F3).
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CIRCLE_SEGMENTS",
    "VectorizeError",
    "sha256_file",
    "sha256_bytes",
    "run_vectorize",
    "normalize_primitives",
]

DEFAULT_TIMEOUT_S = 300
# Círculos do vectorize viram polilinhas fechadas de N vértices (o erro
# corda-arco fica ~r * pi^2/(4 N^2): para r=105 mm e N=64, < 0,1 mm).
CIRCLE_SEGMENTS = 64
# O vectorize real sempre escreve SVG ao lado do JSON (default: mesmo nome
# do PNG com sufixo .svg); o runner pede --out explícito para não escrever
# ao lado da entrada.
_MAX_CAPTURE_CHARS = 8000


class VectorizeError(RuntimeError):
    """Falha fail-closed do runner do vectorize (hash, timeout, saída)."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def circle_points_mm(
    cx_mm: float, cy_mm: float, r_mm: float, *, segments: int = CIRCLE_SEGMENTS
) -> list[list[float]]:
    """Discretiza um círculo ``(cx, cy, r)`` em ``segments`` vértices.

    Os pontos são dados na mesma convenção de mm y-down do vectorize
    (3 casas decimais); a polilinha fecha implicitamente do último
    vértice para o primeiro. A validação de limites da folha fica com
    ``document.add_stroke`` — um círculo que sai da folha é recusado
    lá, nunca clampado aqui.
    """
    step = 2.0 * math.pi / segments
    return [
        [
            round(cx_mm + r_mm * math.cos(step * i), 3),
            round(cy_mm + r_mm * math.sin(step * i), 3),
        ]
        for i in range(segments)
    ]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def normalize_primitives(primitives: list[dict]) -> tuple[list[dict], int]:
    """Normaliza primitivas do vectorize para polilinhas com ``points_mm``.

    Devolve ``(polylines, without_points)``. Cada polilinha é
    ``{"points_mm": [[x, y], ...], "closed": bool, "type": str}`` com
    ``t_start_ms``/``t_end_ms`` opcionais preservados da primitiva.
    Linhas com ``x1_mm``/``y1_mm``/``x2_mm``/``y2_mm`` são convertidas
    em dois pontos, ``point`` (``x_mm``/``y_mm``) vira polilinha de 1
    ponto e ``circle`` (``cx_mm``/``cy_mm``/``r_mm``) é discretizado em
    ``CIRCLE_SEGMENTS`` vértices (fechado; os parâmetros analíticos vão
    em ``circle_mm`` para uso futuro no CAD analítico). Formatos
    legados ``points_mm`` e ``corners_mm`` são preservados. Primitivas
    sem geometria reconhecida contam em ``without_points``.
    """
    polylines, skipped_types = _normalize_primitives_with_skipped_types(primitives)
    return polylines, len(skipped_types)


def _primitive_stamps(primitive: dict) -> dict[str, int]:
    """Extrai ``t_start_ms``/``t_end_ms`` da primitiva; inválido => erro."""
    stamps: dict[str, int] = {}
    for key in ("t_start_ms", "t_end_ms"):
        value = primitive.get(key)
        if value is None:
            continue
        number = _finite_number(value)
        if number is None or not number.is_integer() or number < 0:
            raise VectorizeError(
                f"primitiva {primitive.get('type')!r}: {key} inválido: {value!r}"
            )
        stamps[key] = int(number)
    return stamps


def _normalize_primitives_with_skipped_types(
    primitives: list[dict],
) -> tuple[list[dict], list[str]]:
    """Normaliza primitivas e devolve os tipos sem geometria reconhecida."""
    polylines: list[dict] = []
    skipped_types: list[str] = []
    for primitive in primitives:
        if not isinstance(primitive, dict):
            # Elemento malformado (null, string, número): conta como
            # tipo não reconhecido, nunca deixa AttributeError escapar
            # cru — o contrato do runner é VectorizeError/lista de skips.
            skipped_types.append("non-dict")
            continue
        polyline_type = str(primitive.get("type", "unknown"))
        stamps = _primitive_stamps(primitive)
        points = primitive.get("points_mm") or primitive.get("corners_mm")
        entry: dict[str, Any] | None = None
        if not points and polyline_type == "line":
            endpoints = ("x1_mm", "y1_mm", "x2_mm", "y2_mm")
            if all(key in primitive for key in endpoints):
                points = [
                    [primitive["x1_mm"], primitive["y1_mm"]],
                    [primitive["x2_mm"], primitive["y2_mm"]],
                ]
        if points:
            closed = polyline_type in ("polygon", "rectangle") or bool(
                primitive.get("closed", False)
            )
            # Mesma regra de document._check_points: bool e não-número
            # (str, None, ...) são recusados ANTES do float() — float(True)
            # = 1.0 e float("1.5") = 1.5 aceitariam coordenada corrompida
            # em silêncio, e NaN/Inf atravessariam como ponto não finito.
            if not all(
                isinstance(point, (list, tuple))
                and len(point) == 2
                and all(
                    not isinstance(v, bool)
                    and isinstance(v, (int, float))
                    and math.isfinite(float(v))
                    for v in point
                )
                for point in points
            ):
                raise VectorizeError(
                    f"primitiva {polyline_type!r} com points_mm malformados "
                    f"(pares [x, y] finitos exigidos): {points!r}"
                )
            clean_points = [[float(x), float(y)] for x, y in points]
            entry = {
                "points_mm": clean_points,
                "closed": closed,
                "type": polyline_type,
            }
        elif polyline_type == "circle":
            cx = _finite_number(primitive.get("cx_mm"))
            cy = _finite_number(primitive.get("cy_mm"))
            radius = _finite_number(primitive.get("r_mm"))
            if cx is not None and cy is not None and radius is not None and radius > 0:
                entry = {
                    "points_mm": circle_points_mm(cx, cy, radius),
                    "closed": True,
                    "type": "circle",
                    "circle_mm": {
                        "cx_mm": cx,
                        "cy_mm": cy,
                        "r_mm": radius,
                        **{
                            "radial_rmse_px": rmse
                            for key in ("radial_rmse_px",)
                            if (rmse := _finite_number(primitive.get(key))) is not None
                        },
                    },
                }
        elif polyline_type == "point":
            x = _finite_number(primitive.get("x_mm"))
            y = _finite_number(primitive.get("y_mm"))
            if x is not None and y is not None:
                entry = {
                    "points_mm": [[x, y]],
                    "closed": False,
                    "type": "point",
                }
        if entry is None:
            skipped_types.append(polyline_type)
            continue
        entry.update(stamps)
        polylines.append(entry)
    return polylines, skipped_types


def run_vectorize(
    png_path: str | Path,
    script_path: str | Path,
    expected_sha256: str,
    output_dir: str | Path | None = None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    extra_args: list[str] | None = None,
    stamps_path: str | Path | None = None,
    clean: bool = False,
    epsilon: float | None = None,
    thinning: str | None = None,
    paper_mm: tuple[float, float] | None = None,
) -> dict:
    """Roda o vectorize do Draw sobre ``png_path`` e devolve o resultado.

    ``expected_sha256`` é o hash pinado do ``src/draw/exporters/vectorize.py``; o
    script é conferido antes de qualquer execução. O JSON de saída é
    escrito num diretório isolado (``output_dir`` ou um temporário, que
    é removido ao fim quando criado aqui) e lido de volta.
    """
    png_path = Path(png_path).resolve()
    script_path = Path(script_path).resolve()
    if stamps_path is not None:
        stamps_path = Path(stamps_path).resolve()
    if not png_path.is_file():
        raise VectorizeError(f"PNG de entrada não encontrado: {png_path}")
    if not script_path.is_file():
        raise VectorizeError(f"script vectorize não encontrado: {script_path}")
    actual_sha256 = sha256_file(script_path)
    if actual_sha256 != expected_sha256:
        raise VectorizeError(
            "sha256 do vectorize divergiu do pinado "
            f"(esperado {expected_sha256}, obtido {actual_sha256}); "
            "recusado — revalidar o pin antes de rodar (B8)"
        )

    own_tmpdir: tempfile.TemporaryDirectory | None = None
    if output_dir is None:
        own_tmpdir = tempfile.TemporaryDirectory(prefix="ocre-vectorize-")
        out_dir = Path(own_tmpdir.name)
    else:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    try:
        json_path = out_dir / "vectorize.json"
        svg_path = out_dir / "vectorize.svg"
        command = [
            sys.executable,
            str(script_path),
            str(png_path),
            "--json",
            str(json_path),
            "--out",
            str(svg_path),
        ]
        if clean:
            command.append("--clean")
        if stamps_path is not None:
            command += ["--stamps", str(stamps_path)]
        if epsilon is not None:
            command += ["--epsilon", str(epsilon)]
        if thinning is not None:
            command += ["--thinning", thinning]
        if paper_mm is not None:
            command += [
                "--paper-w-mm", str(paper_mm[0]),
                "--paper-h-mm", str(paper_mm[1]),
            ]
        if extra_args:
            command += list(extra_args)

        try:
            completed = subprocess.run(
                command,
                cwd=str(out_dir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VectorizeError(
                f"vectorize excedeu o timeout de {timeout_s}s "
                f"(parcial: {str(exc.stdout or '')[:500]!r})"
            ) from exc

        stdout = (completed.stdout or "")[-_MAX_CAPTURE_CHARS:]
        stderr = (completed.stderr or "")[-_MAX_CAPTURE_CHARS:]
        if completed.returncode != 0:
            raise VectorizeError(
                f"vectorize saiu com código {completed.returncode}; "
                f"stderr: {stderr!r}"
            )
        if not json_path.is_file():
            raise VectorizeError(
                f"vectorize terminou com código 0 mas não escreveu {json_path}"
            )
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise VectorizeError(f"JSON do vectorize inválido: {exc}") from exc
        primitives = payload.get("primitives") if isinstance(payload, dict) else None
        if not isinstance(primitives, list):
            raise VectorizeError(
                "JSON do vectorize sem a lista 'primitives' — saída "
                "incompatível com o formato esperado"
            )
        polylines, skipped_types = _normalize_primitives_with_skipped_types(primitives)
        return {
            "script_path": str(script_path),
            "script_sha256": actual_sha256,
            "png_path": str(png_path),
            "json_path": str(json_path),
            "svg_path": str(svg_path),
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "summary": payload.get("summary") if isinstance(payload, dict) else None,
            "primitives": primitives,
            "polylines": polylines,
            "primitives_total": len(primitives),
            "polylines_total": len(polylines),
            "primitives_without_points": len(skipped_types),
            "skipped_primitive_types": skipped_types,
        }
    finally:
        if own_tmpdir is not None:
            own_tmpdir.cleanup()
