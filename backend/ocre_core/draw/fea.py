"""FEA axial reference case (beta v0.1): Gmsh + CalculiX, stdlib only.

Pipeline (arquivo primeiro, CLI em cima de arquivo)::

    STEP --gmsh--> .msh --(parser proprio)--> .inp --ccx--> .frd/.dat/.sta
      --> resumo validado + sidecar ``draw-paper-provenance/0.1``
      + fingerprint de dominio ``DRAW:PAPER-APPROVAL:FINGERPRINT``

Escopo honesto (decisao do Senado, corrida senado-corrida-beta,
formula fechada, A 12 | B 9 | C 0): Gmsh + CalculiX sao o caminho FEA
do beta PROVISORIO. "Aprovado" aqui significa selecionado para o beta,
NAO provado contra conjunto selado -- nao existe conjunto selado, e o
gate G4.1 abaixo e a unica prova que este modulo oferece: reproduzir o
caso axial de referencia dentro de <=1%.

O caso axial de referencia (o UNICO caso do beta):

- Barra 100 x 10 x 10 mm, E = 210000 N/mm2 (MPa), NU = 0.3.
- NU = 0.3 vem do benchmark sintetico do OCRE -- errata do Senado:
  NU nao consta das fichas da biblioteca. Registrado no sidecar.
- Referencia: Rx = 2100 N, S11 = 21 MPa.
- Carga por deslocamento prescrito: ux = +0.01 mm na face carregada
  (deformacao 1e-4 x E = 21 MPa nominais). Controle por deslocamento,
  nao por forca: com forca pontual (*CLOAD) nos nos da face, a
  concentracao local estouraria o S11 maximo; com deslocamento
  uniforme o campo e uniaxial limpo e Rx e S11 continuam sendo as
  grandezas de referencia (Rx sai da reacao, nao da carga imposta).
- Vinculos minimos na face fixa (ux em todos os nos; uy so na aresta
  y=0; uz so na aresta z=0): trava os 6 modos de corpo rigido e deixa
  a contracao lateral livre, para nao criar concentracao de tensao
  na face fixa. Fixar uy/uz em toda a face fixa elevaria o S11 local
  e quebraria o gate por artefato de contorno, nao por fisica.

Regras de execucao (ficha formatos.md, "CLI como subprocesso"):

- Gmsh e CalculiX rodam SOMENTE como subprocesso: argv lista,
  cwd isolado (o ``workdir`` do caso), timeout obrigatorio,
  ``shell=False``, ``capture_output=True``. Nunca ``import gmsh`` no
  processo OCRE (importar libgmsh seria linkagem GPL).
- O ``ProcessBatchHost`` do OCRE (``ocre_core.integrations``) NAO foi
  usado: ele cria um cwd novo por invocacao, e este pipeline precisa
  de UM workdir estavel compartilhado por gmsh -> parse -> .inp ->
  ccx. O subprocesso direto aqui segue as mesmas regras do host.
- O .inp NAO e o .inp do Gmsh: a ficha registra que o .inp do Gmsh
  geralmente precisa de pos-filtro antes do CalculiX (ordenacao de
  nos C3D10). Este modulo le o .msh (MSH 4.1) e escreve o proprio
  deck com tetraedros lineares C3D4 -- o problema de ordenacao
  C3D10 nao existe para C3D4, e o deck escrito e auditavel.
- As faces de contorno vem dos Physical Groups do Gmsh
  (``fixed``/``loaded``); se a malha nao trouxer os grupos, ha
  fallback por coordenada (faces em x=min / x=max da barra),
  registrado em ``bc_source`` no resumo e no sidecar.
- Fail-closed: caso incompleto (sem material, carga ou BC
  declarados) = job BLOQUEADO antes de qualquer estimativa; gate
  fora de 1% = job FALHOU, nunca estimado, nunca apresentado como
  resultado valido. Erro/timeout de solver vira falha registrada
  no resumo, nunca excecao silenciosa.

Saida de :func:`run_axial_case` (dict, sempre -- nunca levanta
excecao por falha do caso; so erros de programacao levantam):

- ``status``: ``"passed"`` | ``"failed"`` | ``"blocked"``.
- ``measured``: Rx (soma das reacoes RF1 na face fixa), S11 maximo
  (sobre os elementos), ux maximo na face carregada.
- ``gate``: G4.1, tolerancia 1%, erros percentuais.
- ``artifacts``: .msh/.inp/.frd/.dat/.sta com sha256 (brutos ficam
  no workdir como artefatos).
- ``sidecar``: ``draw-paper-provenance/0.1`` (document_sha256 = sha
  do documento Draw quando houver; sem documento, sha do STEP, com
  a substituicao registrada em ``export_parameters``).
- ``fingerprint``: digest SHA-256 com separador de dominio
  ``DRAW:PAPER-APPROVAL:FINGERPRINT`` sobre o resumo validado em
  JCS (mesmo codificador de ``ocre_core.draw.fingerprint``). Nao e
  um DRAW-AF/1 de 10 campos: DRAW-AF/1 e para documentos
  ``draw.paper.v1`` e o caso FEA nao tem documento Draw.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fingerprint import DOMAIN as _FINGERPRINT_DOMAIN
from .fingerprint import jcs as _jcs
from .sidecar import SIDECAR_VERSION, build_draw_sidecar

__all__ = [
    "AXIAL_CASE_ID",
    "AxialCaseSpec",
    "FINGERPRINT_DOMAIN_LABEL",
    "GATE_ID",
    "GATE_TOLERANCE_PCT",
    "FeaResult",
    "build_bar_step",
    "parse_ccx_dat",
    "parse_gmsh_msh",
    "run_axial_case",
    "write_ccx_inp",
]

AXIAL_CASE_ID = "axial-bar-100x10x10"
GATE_ID = "G4.1"
GATE_TOLERANCE_PCT = 1.0
FINGERPRINT_DOMAIN_LABEL = "DRAW:PAPER-APPROVAL:FINGERPRINT"
FEA_VERSION = "0.1.0"

# Referencia do caso axial (mm, N, MPa = N/mm2).
BAR_LENGTH_MM = 100.0
BAR_WIDTH_MM = 10.0
BAR_HEIGHT_MM = 10.0
YOUNG_MODULUS_MPA = 210000.0
# Errata do Senado: NU = 0.3 vem do benchmark sintetico do OCRE,
# nao consta das fichas da biblioteca.
POISSON_RATIO = 0.3
# ux = eps * L, eps = S11/E = 21/210000 = 1e-4 -> ux = 0.01 mm.
PRESCRIBED_UX_MM = 0.01
RX_REF_N = 2100.0
S11_REF_MPA = 21.0

DEFAULT_GMSH_BIN = "/usr/bin/gmsh"
DEFAULT_CCX_BIN = "/usr/bin/ccx"
DEFAULT_MESH_SIZE_MM = 2.5
DEFAULT_TIMEOUT_S = 300.0
_MAX_CAPTURE_CHARS = 8000
# CalculiX nao aceita mais de 16 entradas por linha de .inp.
_INP_ENTRIES_PER_LINE = 8

# Elementos Gmsh -> nos por elemento (so os tipos que lemos).
_MSH_NODES_PER_ELEMENT = {1: 2, 2: 3, 3: 4, 4: 4, 5: 8, 11: 10, 15: 1}
_MSH_TET4 = 4
_MSH_TRI3 = 2


@dataclass(frozen=True)
class AxialCaseSpec:
    """Caso axial de referencia, tudo declarado -- nada implicito.

    ``young_modulus_mpa``, ``poisson_ratio`` e ``prescribed_ux_mm``
    aceitam ``None`` so para que a falta seja detectavel: um spec
    incompleto bloqueia o job em vez de rodar com default silencioso.
    """

    step_path: str | Path
    young_modulus_mpa: float | None = YOUNG_MODULUS_MPA
    poisson_ratio: float | None = POISSON_RATIO
    prescribed_ux_mm: float | None = PRESCRIBED_UX_MM
    length_mm: float = BAR_LENGTH_MM
    width_mm: float = BAR_WIDTH_MM
    height_mm: float = BAR_HEIGHT_MM
    mesh_size_mm: float = DEFAULT_MESH_SIZE_MM
    timeout_gmsh_s: float = DEFAULT_TIMEOUT_S
    timeout_ccx_s: float = DEFAULT_TIMEOUT_S

    def issues(self) -> list[str]:
        """Declaracoes faltando/invalidas -- vazio significa completo."""
        found: list[str] = []
        if not str(self.step_path):
            found.append("step_path must be a non-empty path")
        elif not Path(self.step_path).is_file():
            found.append(f"step file not found: {self.step_path}")
        if self.young_modulus_mpa is None:
            found.append("material missing: young_modulus_mpa not declared")
        elif not math.isfinite(self.young_modulus_mpa) or self.young_modulus_mpa <= 0:
            found.append("young_modulus_mpa must be finite and positive")
        if self.poisson_ratio is None:
            found.append("material missing: poisson_ratio not declared")
        elif not math.isfinite(self.poisson_ratio) or not -1.0 < self.poisson_ratio < 0.5:
            found.append("poisson_ratio must be finite and in (-1, 0.5)")
        if self.prescribed_ux_mm is None:
            found.append("load missing: prescribed_ux_mm not declared")
        elif not math.isfinite(self.prescribed_ux_mm) or self.prescribed_ux_mm == 0:
            found.append("prescribed_ux_mm must be finite and non-zero")
        if not math.isfinite(self.mesh_size_mm) or self.mesh_size_mm <= 0:
            found.append("mesh_size_mm must be finite and positive")
        if not math.isfinite(self.timeout_gmsh_s) or self.timeout_gmsh_s <= 0:
            found.append("timeout_gmsh_s must be finite and positive")
        if not math.isfinite(self.timeout_ccx_s) or self.timeout_ccx_s <= 0:
            found.append("timeout_ccx_s must be finite and positive")
        return found


# Alias de leitura: o resumo do caso e um dict simples e JSON-serializavel.
FeaResult = dict


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _truncate(text: str, limit: int = _MAX_CAPTURE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} chars omitted]"


def _run(argv: list[str], cwd: Path, timeout_s: float) -> dict[str, Any]:
    """Subprocesso com as regras da ficha: argv lista, cwd isolado,
    timeout obrigatorio, shell=False, capture_output=True."""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            timeout=timeout_s,
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timed_out": True,
            "returncode": None,
            "stdout": _truncate(exc.stdout.decode("utf-8", "replace")
                                if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            "stderr": _truncate(exc.stderr.decode("utf-8", "replace")
                                if isinstance(exc.stderr, bytes) else (exc.stderr or "")),
            "failure": f"timeout after {timeout_s}s: {' '.join(argv)}",
        }
    except OSError as exc:
        return {
            "ok": False,
            "timed_out": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "failure": f"could not start {' '.join(argv)}: {exc}",
        }
    return {
        "ok": completed.returncode == 0,
        "timed_out": False,
        "returncode": completed.returncode,
        "stdout": _truncate(completed.stdout or ""),
        "stderr": _truncate(completed.stderr or ""),
        "failure": "" if completed.returncode == 0 else (
            f"{argv[0]} exited with returncode {completed.returncode}"),
    }


def _tool_version(binary: str, argv: list[str], timeout_s: float,
                  cwd: Path) -> str:
    """Versao de uma ferramenta externa (para o pin do sidecar)."""
    result = _run(argv, cwd, timeout_s)
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    return text.strip().splitlines()[0].strip() if text.strip() else "unknown"


# ---------------------------------------------------------------------------
# STEP da barra (so para o teste do gate; o STEP de desenho vem depois,
# do worker STEP, pelo mesmo caminho -- este modulo aceita qualquer STEP).
# ---------------------------------------------------------------------------

_BAR_GEO = """SetFactory("OpenCASCADE");
Box(1) = {{0, 0, 0, {length:g}, {width:g}, {height:g}}};
"""


def build_bar_step(workdir: str | Path, *, gmsh_bin: str = DEFAULT_GMSH_BIN,
                   length_mm: float = BAR_LENGTH_MM,
                   width_mm: float = BAR_WIDTH_MM,
                   height_mm: float = BAR_HEIGHT_MM,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Gera o STEP da barra de referencia com o Gmsh/OCC.

    A barra e um Box primitivo (``Box(1) = {0,0,0,L,W,H}``) exportado
    para STEP AP214 pelo Open CASCADE embutido no Gmsh -- declarado
    aqui porque nao ha build123d neste venv: o STEP conhecido do gate
    vem do Gmsh, nao de um CAD externo. Retorna dict com
    ``ok``/``step_path``/``failure``.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    geo_path = workdir / "bar.geo"
    step_path = workdir / "bar.step"
    geo_path.write_text(
        _BAR_GEO.format(length=length_mm, width=width_mm, height=height_mm),
        encoding="utf-8")
    result = _run([gmsh_bin, "-3", str(geo_path), "-format", "step",
                   "-o", str(step_path)], workdir, timeout_s)
    if not result["ok"]:
        return {"ok": False, "step_path": None,
                "failure": result["failure"],
                "stdout": result["stdout"], "stderr": result["stderr"]}
    if not step_path.is_file():
        return {"ok": False, "step_path": None,
                "failure": "gmsh reported success but bar.step was not written",
                "stdout": result["stdout"], "stderr": result["stderr"]}
    return {"ok": True, "step_path": str(step_path), "failure": "",
            "stdout": result["stdout"], "stderr": result["stderr"]}


# ---------------------------------------------------------------------------
# Leitura do .msh (MSH 4.1, como o Gmsh 4.12 escreve)
# ---------------------------------------------------------------------------

def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("$") and not stripped.startswith("$End"):
            current = stripped
            sections[current] = []
        elif stripped.startswith("$End"):
            current = None
        elif current is not None:
            sections[current].append(line)
    return sections


def parse_gmsh_msh(path: str | Path) -> dict[str, Any]:
    """Le um .msh MSH 4.1 e devolve nos, tetraedros e grupos fisicos.

    Layout real do Gmsh 4.12 (verificado contra o arquivo, nao contra
    a memoria do formato): em ``$Nodes`` cada bloco lista primeiro
    TODOS os tags dos nos e depois TODAS as coordenadas; em
    ``$Elements`` cada elemento e uma linha (``tag nos...``). Os
    Physical Groups chegam via ``$PhysicalNames`` + ``$Entities``
    (o tag de entidade do bloco de elementos e geometrico, nao
    fisico). Retorna ``{"nodes", "tets", "face_nodes", "physical"}``;
    ``face_nodes`` mapeia o nome do Physical Surface (``fixed`` /
    ``loaded``) para o conjunto de nos dos triangulos daquela face.
    Levanta ValueError se o arquivo nao for MSH 4.1 legivel.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    sections = _split_sections(text)
    fmt = sections.get("$MeshFormat", [])
    if not fmt or not fmt[0].strip().startswith("4.1"):
        raise ValueError(f"unsupported mesh format (expected MSH 4.1): "
                         f"{fmt[0].strip() if fmt else '<missing>'}")
    physical: dict[tuple[int, int], str] = {}
    for line in sections.get("$PhysicalNames", [])[1:]:
        parts = line.split()
        if len(parts) >= 3:
            physical[(int(parts[0]), int(parts[1]))] = parts[2].strip('"')
    # $Entities: (dim, entity tag) -> physical tags.
    entity_physical: dict[tuple[int, int], set[int]] = {}
    entity_lines = sections.get("$Entities", [])
    if entity_lines:
        counts = [int(x) for x in entity_lines[0].split()]
        index = 1
        for dim, count in enumerate(counts):
            for _ in range(count):
                parts = entity_lines[index].split()
                index += 1
                tag = int(parts[0])
                if dim == 0:
                    n_phys = int(parts[4])
                    tags = [abs(int(x)) for x in parts[5:5 + n_phys]]
                else:
                    n_phys = int(parts[7])
                    tags = [abs(int(x)) for x in parts[8:8 + n_phys]]
                entity_physical[(dim, tag)] = set(tags)
    # $Nodes: blocos (dim, tag, param, count); tags primeiro, coords depois.
    node_lines = sections.get("$Nodes", [])
    if not node_lines:
        raise ValueError("missing $Nodes section")
    n_blocks = int(node_lines[0].split()[0])
    index = 1
    nodes: dict[int, tuple[float, float, float]] = {}
    for _ in range(n_blocks):
        _, _, param, count = (int(x) for x in node_lines[index].split())
        index += 1
        tags = [int(node_lines[index + k]) for k in range(count)]
        index += count
        for k in range(count):
            parts = node_lines[index].split()
            index += 1
            # Coordenadas sao sempre os 3 primeiros valores da linha;
            # nos parametricos (param=1) trazem u/v extras depois,
            # ignorados aqui.
            nodes[tags[k]] = (float(parts[0]), float(parts[1]),
                              float(parts[2]))
    # $Elements: uma linha por elemento.
    elem_lines = sections.get("$Elements", [])
    if not elem_lines:
        raise ValueError("missing $Elements section")
    n_blocks = int(elem_lines[0].split()[0])
    index = 1
    tets: list[list[int]] = []
    face_nodes: dict[str, set[int]] = {}
    for _ in range(n_blocks):
        dim, entity, elem_type, count = (
            int(x) for x in elem_lines[index].split())
        index += 1
        try:
            n_nodes = _MSH_NODES_PER_ELEMENT[elem_type]
        except KeyError as exc:
            raise ValueError(
                f"unsupported Gmsh element type {elem_type}") from exc
        for _ in range(count):
            parts = [int(x) for x in elem_lines[index].split()]
            index += 1
            conn = parts[1:1 + n_nodes]
            if dim == 3 and elem_type == _MSH_TET4:
                tets.append(conn)
            elif dim == 2 and elem_type == _MSH_TRI3:
                for phys_tag in entity_physical.get((dim, entity), set()):
                    name = physical.get((dim, phys_tag))
                    if name:
                        face_nodes.setdefault(name, set()).update(conn)
    return {"nodes": nodes, "tets": tets, "face_nodes": face_nodes,
            "physical": physical}


def _face_nodes_by_coordinates(nodes: dict[int, tuple[float, float, float]],
                               tol: float = 1e-6) -> dict[str, set[int]]:
    """Fallback quando a malha nao traz Physical Groups: faces em
    x=min (``fixed``) e x=max (``loaded``) por coordenada."""
    xs = [coord[0] for coord in nodes.values()]
    xmin, xmax = min(xs), max(xs)
    fixed = {nid for nid, (x, _, _) in nodes.items() if abs(x - xmin) <= tol}
    loaded = {nid for nid, (x, _, _) in nodes.items() if abs(x - xmax) <= tol}
    return {"fixed": fixed, "loaded": loaded}


# ---------------------------------------------------------------------------
# Escrita do .inp (deck proprio, C3D4 -- nao o .inp do Gmsh)
# ---------------------------------------------------------------------------

def write_ccx_inp(mesh: dict[str, Any], path: str | Path, *,
                  young_modulus_mpa: float = YOUNG_MODULUS_MPA,
                  poisson_ratio: float = POISSON_RATIO,
                  prescribed_ux_mm: float = PRESCRIBED_UX_MM) -> dict[str, Any]:
    """Escreve o deck CalculiX a partir da malha parseada.

    Devolve ``{"face_nodes", "bc_source", "uy_nodes", "uz_nodes"}``.
    ``bc_source`` e ``"physical-groups"`` ou ``"coordinates"``
    (fallback). Vinculos minimos na face fixa, ver docstring do modulo.
    """
    nodes = mesh["nodes"]
    tets = mesh["tets"]
    face_nodes = mesh["face_nodes"]
    if "fixed" in face_nodes and "loaded" in face_nodes:
        bc_source = "physical-groups"
    else:
        face_nodes = _face_nodes_by_coordinates(nodes)
        bc_source = "coordinates"
    fixed = face_nodes["fixed"]
    loaded = face_nodes["loaded"]
    if not fixed or not loaded:
        raise ValueError("empty boundary face: fixed or loaded node set "
                         "is empty")
    tol = 1e-6
    uy_nodes = sorted(nid for nid in fixed if abs(nodes[nid][1]) <= tol)
    uz_nodes = sorted(nid for nid in fixed if abs(nodes[nid][2]) <= tol)
    lines: list[str] = [
        "*HEADING",
        f"axial bar, E={young_modulus_mpa:g} MPa, nu={poisson_ratio:g}, "
        f"ux={prescribed_ux_mm:g} mm on loaded face",
        "*NODE",
    ]
    for nid in sorted(nodes):
        x, y, z = nodes[nid]
        lines.append(f"{nid}, {x:.9g}, {y:.9g}, {z:.9g}")
    lines.append("*ELEMENT, TYPE=C3D4, ELSET=bar")
    for number, conn in enumerate(tets, start=1):
        lines.append(f"{number}, {', '.join(str(n) for n in conn)}")

    def _nset(name: str, ids: set[int] | list[int]) -> None:
        ordered = sorted(ids)
        lines.append(f"*NSET, NSET={name}")
        for start in range(0, len(ordered), _INP_ENTRIES_PER_LINE):
            lines.append(", ".join(
                str(n) for n in ordered[start:start + _INP_ENTRIES_PER_LINE]))

    _nset("fixed", fixed)
    _nset("loaded", loaded)
    _nset("uyline", uy_nodes)
    _nset("uzline", uz_nodes)
    lines += [
        "*MATERIAL, NAME=steel",
        "*ELASTIC",
        f"{young_modulus_mpa:.9g}, {poisson_ratio:.9g}",
        "*SOLID SECTION, ELSET=bar, MATERIAL=steel",
        "*BOUNDARY",
        "fixed, 1, 1, 0.0",
        "uyline, 2, 2, 0.0",
        "uzline, 3, 3, 0.0",
        f"loaded, 1, 1, {prescribed_ux_mm:.9g}",
        "*STEP",
        "*STATIC",
        "*NODE FILE",
        "U, RF",
        "*EL FILE",
        "S",
        "*NODE PRINT, NSET=fixed",
        "RF",
        "*NODE PRINT, NSET=loaded",
        "U",
        "*EL PRINT, ELSET=bar",
        "S",
        "*END STEP",
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"face_nodes": {"fixed": sorted(fixed), "loaded": sorted(loaded)},
            "bc_source": bc_source,
            "uy_nodes": uy_nodes, "uz_nodes": uz_nodes}


# ---------------------------------------------------------------------------
# Leitura do .dat do CalculiX
# ---------------------------------------------------------------------------

def parse_ccx_dat(text: str) -> dict[str, Any]:
    """Le Rx (soma de RF1 na face fixa), S11 max e ux max do .dat.

    Levanta ValueError se alguma das tres secoes estiver ausente --
    um .dat sem as secoes pedidas nao e um resultado, e um artefato
    incompleto.
    """
    forces: list[float] = []
    in_forces = False
    for line in text.splitlines():
        if line.startswith(" forces (fx,fy,fz) for set FIXED"):
            in_forces = True
            continue
        if in_forces:
            parts = line.split()
            if len(parts) == 4 and parts[0].isdigit():
                forces.append(float(parts[1]))
            elif parts and not parts[0].isdigit() and forces:
                in_forces = False
    if not forces:
        raise ValueError("no RF section for set FIXED in .dat")

    s11_values: list[float] = []
    in_stress = False
    for line in text.splitlines():
        if line.startswith(" stresses (elem, integ.pnt.,sxx,syy,szz,sxy,sxz,syz)"):
            in_stress = True
            continue
        if in_stress:
            parts = line.split()
            if len(parts) >= 8 and parts[0].isdigit() and parts[1].isdigit():
                s11_values.append(float(parts[2]))
            elif parts and not parts[0].isdigit() and s11_values:
                in_stress = False
    if not s11_values:
        raise ValueError("no S section for set BAR in .dat")

    ux_values: list[float] = []
    in_disp = False
    for line in text.splitlines():
        # CalculiX 2.21 escreve "(vx,vy,vz)"; outras versoes escrevem
        # "(ux,uy,uz)" -- aceitar as duas grafias.
        if (line.startswith(" displacements (")
                and "for set LOADED" in line):
            in_disp = True
            continue
        if in_disp:
            parts = line.split()
            if len(parts) == 4 and parts[0].isdigit():
                ux_values.append(float(parts[1]))
            elif parts and not parts[0].isdigit() and ux_values:
                in_disp = False
    if not ux_values:
        raise ValueError("no U section for set LOADED in .dat")
    return {"rx_N": sum(forces),
            "s11_max_MPa": max(s11_values),
            "s11_min_MPa": min(s11_values),
            "ux_max_mm": max(ux_values),
            "n_fixed_nodes": len(forces),
            "n_elements": len(s11_values)}


# ---------------------------------------------------------------------------
# Geo de malha (Gmsh) e caso completo
# ---------------------------------------------------------------------------

_MESH_GEO = """SetFactory("OpenCASCADE");
Merge "input.step";
v() = Volume{{:}};
Physical Volume("bar") = v();
s() = Unique(Abs(Boundary{{ Volume{{v()}}; }}));
Physical Surface("fixed") = {{}};
Physical Surface("loaded") = {{}};
For i In {{0:#s()-1}}
  bb() = BoundingBox Surface{{s(i)}};
  cx = (bb(0)+bb(3))/2;
  If (cx < {xmin_tol:g})
    Physical Surface("fixed") += {{s(i)}};
  EndIf
  If (cx > {xmax_tol:g})
    Physical Surface("loaded") += {{s(i)}};
  EndIf
EndFor
Mesh.CharacteristicLengthMax = {mesh_size:g};
Mesh.ElementOrder = 1;
Mesh.Algorithm3D = 1;
Mesh 3;
Save "bar.msh";
"""


def _pct_error(measured: float, reference: float) -> float:
    return abs(abs(measured) - reference) / reference * 100.0


def _fingerprint_summary(summary: dict[str, Any]) -> str:
    """Digest com separador de dominio DRAW:PAPER-APPROVAL:FINGERPRINT
    sobre o resumo validado em JCS (RFC 8785)."""
    framed = _FINGERPRINT_DOMAIN + _jcs(summary)
    return hashlib.sha256(framed).hexdigest()


def _base_result(status: str, spec: AxialCaseSpec) -> FeaResult:
    return {
        "status": status,
        "case": AXIAL_CASE_ID,
        "gate": {"id": GATE_ID, "tolerance_pct": GATE_TOLERANCE_PCT,
                 "passed": False},
        "reference": {
            "length_mm": spec.length_mm, "width_mm": spec.width_mm,
            "height_mm": spec.height_mm,
            "young_modulus_MPa": spec.young_modulus_mpa,
            "poisson_ratio": spec.poisson_ratio,
            "poisson_ratio_source": "OCRE synthetic benchmark "
                                    "(not in library fichas -- Senate errata)",
            "prescribed_ux_mm": spec.prescribed_ux_mm,
            "rx_N": RX_REF_N, "s11_MPa": S11_REF_MPA,
        },
        "measured": None,
        "errors_pct": None,
        "artifacts": {},
        "sidecar": None,
        "fingerprint": None,
        "failures": [],
        "notes": [],
    }


def run_axial_case(step_path: str | Path, workdir: str | Path, *,
                   spec: AxialCaseSpec | None = None,
                   doc_id: str | None = None,
                   document_sha256: str | None = None,
                   gmsh_bin: str = DEFAULT_GMSH_BIN,
                   ccx_bin: str = DEFAULT_CCX_BIN,
                   mesh_size_mm: float | None = None) -> FeaResult:
    """Roda o caso axial de referencia (gate G4.1). Ver docstring do modulo.

    ``step_path`` e o STEP de entrada (barra de referencia para o
    gate; STEP de desenho depois, pelo mesmo caminho). ``workdir`` e
    o diretorio isolado do caso -- todos os artefatos ficam la.
    ``document_sha256``/``doc_id`` ligam o caso a um documento
    ``draw.paper.v1`` quando houver; sem documento, o sidecar usa o
    sha do STEP e registra a substituicao.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if spec is None:
        spec = AxialCaseSpec(step_path=step_path)
    if mesh_size_mm is not None:
        spec = AxialCaseSpec(
            step_path=spec.step_path,
            young_modulus_mpa=spec.young_modulus_mpa,
            poisson_ratio=spec.poisson_ratio,
            prescribed_ux_mm=spec.prescribed_ux_mm,
            length_mm=spec.length_mm, width_mm=spec.width_mm,
            height_mm=spec.height_mm, mesh_size_mm=mesh_size_mm,
            timeout_gmsh_s=spec.timeout_gmsh_s,
            timeout_ccx_s=spec.timeout_ccx_s)
    result = _base_result("blocked", spec)

    # --- Preflight fail-closed: caso incompleto nao roda nada. ---
    issues = spec.issues()
    if issues:
        result["failures"] = [
            f"blocked: incomplete case -- {issue}" for issue in issues]
        return result

    step_src = Path(step_path)
    step_sha = _sha256_file(step_src)
    input_step = workdir / "input.step"
    shutil.copyfile(step_src, input_step)

    # --- Gmsh: STEP -> .msh com Physical Groups fixed/loaded. ---
    xmin_tol = 0.1 * spec.length_mm / 100.0  # 0.1 mm para a barra 100 mm
    xmax_tol = spec.length_mm - xmin_tol
    geo_path = workdir / "mesh.geo"
    geo_path.write_text(_MESH_GEO.format(
        xmin_tol=xmin_tol, xmax_tol=xmax_tol,
        mesh_size=spec.mesh_size_mm), encoding="utf-8")
    gmsh = _run([gmsh_bin, "-3", str(geo_path)], workdir,
                spec.timeout_gmsh_s)
    if not gmsh["ok"]:
        result["status"] = "failed"
        result["failures"] = [f"gmsh failed: {gmsh['failure']}"]
        result["notes"] = [_truncate(gmsh["stderr"] or gmsh["stdout"])]
        return result
    msh_path = workdir / "bar.msh"
    if not msh_path.is_file():
        result["status"] = "failed"
        result["failures"] = ["gmsh reported success but bar.msh "
                              "was not written"]
        return result
    try:
        mesh = parse_gmsh_msh(msh_path)
    except (ValueError, IndexError) as exc:
        result["status"] = "failed"
        result["failures"] = [f"could not parse bar.msh: {exc}"]
        return result
    if not mesh["tets"]:
        result["status"] = "failed"
        result["failures"] = ["mesh has no C3D4 volume elements"]
        return result

    # --- Deck proprio (.inp) a partir do .msh parseado. ---
    inp_path = workdir / "job.inp"
    try:
        inp_info = write_ccx_inp(
            mesh, inp_path,
            young_modulus_mpa=spec.young_modulus_mpa or YOUNG_MODULUS_MPA,
            poisson_ratio=spec.poisson_ratio or POISSON_RATIO,
            prescribed_ux_mm=spec.prescribed_ux_mm or PRESCRIBED_UX_MM)
    except ValueError as exc:
        result["status"] = "failed"
        result["failures"] = [f"could not write job.inp: {exc}"]
        return result
    result["notes"].append(
        f"boundary faces from {inp_info['bc_source']}; "
        f"{len(mesh['tets'])} C3D4 elements, {len(mesh['nodes'])} nodes; "
        f".inp written by ocre_core.draw.fea (own writer, not Gmsh .inp)")

    # --- CalculiX. ---
    ccx = _run([ccx_bin, "-i", "job"], workdir, spec.timeout_ccx_s)
    dat_path = workdir / "job.dat"
    sta_path = workdir / "job.sta"
    frd_path = workdir / "job.frd"
    for name, path in (("mesh", msh_path), ("inp", inp_path),
                       ("frd", frd_path), ("dat", dat_path),
                       ("sta", sta_path)):
        if path.is_file():
            result["artifacts"][name] = {
                "file": path.name, "sha256": _sha256_file(path)}
    if not ccx["ok"]:
        result["status"] = "failed"
        result["failures"] = [f"ccx failed: {ccx['failure']}"]
        result["notes"].append(_truncate(ccx["stderr"] or ccx["stdout"]))
        return result
    if not dat_path.is_file():
        result["status"] = "failed"
        result["failures"] = ["ccx reported success but job.dat was "
                              "not written"]
        return result
    try:
        measured = parse_ccx_dat(dat_path.read_text(
            encoding="utf-8", errors="replace"))
    except ValueError as exc:
        result["status"] = "failed"
        result["failures"] = [f"could not parse job.dat: {exc}"]
        return result

    rx_error = _pct_error(measured["rx_N"], RX_REF_N)
    s11_error = _pct_error(measured["s11_max_MPa"], S11_REF_MPA)
    gate_passed = (rx_error <= GATE_TOLERANCE_PCT
                   and s11_error <= GATE_TOLERANCE_PCT)
    result["measured"] = {
        "rx_N": measured["rx_N"],
        "s11_max_MPa": measured["s11_max_MPa"],
        "s11_min_MPa": measured["s11_min_MPa"],
        "ux_max_mm": measured["ux_max_mm"],
        "n_fixed_nodes": measured["n_fixed_nodes"],
        "n_elements": measured["n_elements"],
        "n_nodes": len(mesh["nodes"]),
        "bc_source": inp_info["bc_source"],
    }
    result["errors_pct"] = {"rx": rx_error, "s11": s11_error}
    result["gate"]["passed"] = gate_passed
    result["status"] = "passed" if gate_passed else "failed"
    if not gate_passed:
        result["failures"] = [
            f"gate {GATE_ID} failed: Rx error {rx_error:.4f}% and/or "
            f"S11 error {s11_error:.4f}% exceed {GATE_TOLERANCE_PCT}%"]

    # --- Sidecar + fingerprint sobre o resumo validado. ---
    summary_for_print = {
        "case": AXIAL_CASE_ID,
        "status": result["status"],
        "reference": result["reference"],
        "measured": result["measured"],
        "errors_pct": result["errors_pct"],
        "gate": result["gate"],
        "artifacts": result["artifacts"],
    }
    digest = _fingerprint_summary(summary_for_print)
    result["fingerprint"] = {
        "domain": FINGERPRINT_DOMAIN_LABEL,
        "algorithm": "sha256(JCS summary, domain-separated)",
        "digest": digest,
    }
    doc_sha = document_sha256 or step_sha
    doc_label = doc_id or AXIAL_CASE_ID
    export_parameters: dict[str, Any] = {
        "case": AXIAL_CASE_ID,
        "step_sha256": step_sha,
        "document_sha256_source": ("draw_document" if document_sha256
                                   else "step_sha256 (no draw document)"),
        "young_modulus_MPa": spec.young_modulus_mpa,
        "poisson_ratio": spec.poisson_ratio,
        "poisson_ratio_source": "OCRE synthetic benchmark "
                                "(not in library fichas -- Senate errata)",
        "prescribed_ux_mm": spec.prescribed_ux_mm,
        "loading_mode": "prescribed displacement (ux on loaded face)",
        "boundary_conditions": "ux=0 on fixed face; uy=0 on y=0 edge; "
                               "uz=0 on z=0 edge (minimal constraints)",
        "bc_source": inp_info["bc_source"],
        "mesh_size_mm": spec.mesh_size_mm,
        "element_type": "C3D4",
        "inp_writer": "ocre_core.draw.fea (own writer; Gmsh .inp not used "
                      "-- C3D10 node-ordering post-filter issue avoided)",
        "gate": GATE_ID,
        "gate_tolerance_pct": GATE_TOLERANCE_PCT,
        "fea_version": FEA_VERSION,
    }
    gmsh_version = _tool_version(gmsh_bin, [gmsh_bin, "--version"], 30.0,
                                 workdir)
    ccx_version = _tool_version(ccx_bin, [ccx_bin, "-v"], 30.0, workdir)
    fea_file = Path(__file__).resolve()
    derived: list[dict[str, Any]] = []
    if "frd" in result["artifacts"]:
        derived.append({"artifact_id": "job-frd", "sha256":
                        result["artifacts"]["frd"]["sha256"],
                        "format": "ccx-frd"})
    derived.append({"artifact_id": "job-dat", "sha256":
                    result["artifacts"]["dat"]["sha256"],
                    "format": "ccx-dat"})
    try:
        sidecar = build_draw_sidecar(
            doc_id=doc_label,
            document_sha256=doc_sha,
            revision=0,
            source_artifacts=[{
                "artifact_id": "input-step",
                "uri": "input.step",
                "sha256": step_sha,
                "origin": "synthetic",
                "artifact_kind": "step",
            }],
            derived_artifacts=derived,
            adapters=[
                {"stage_id": "mesh", "adapter_id": "draw.fea.gmsh",
                 "version": gmsh_version or "unknown",
                 "binary_sha256": _sha256_file(gmsh_bin),
                 "capabilities_sha256": _sha256_bytes(
                     f"gmsh {gmsh_version} mesh-3d-c3d4".encode("utf-8"))},
                {"stage_id": "solve", "adapter_id": "draw.fea.ccx",
                 "version": ccx_version or "unknown",
                 "binary_sha256": _sha256_file(ccx_bin),
                 "capabilities_sha256": _sha256_bytes(
                     f"ccx {ccx_version} static-c3d4".encode("utf-8"))},
                {"stage_id": "case", "adapter_id": "draw.fea",
                 "version": FEA_VERSION,
                 "binary_sha256": _sha256_file(fea_file),
                 "capabilities_sha256": _sha256_bytes(
                     f"draw.fea {FEA_VERSION} axial-g4.1".encode("utf-8"))},
            ],
            export_parameters=export_parameters,
            fingerprint_digest=digest,
        )
        result["sidecar"] = sidecar
    except (ValueError, OSError) as exc:
        # O sidecar nao invalida o resultado numerico, mas a falta dele
        # e registrada -- sem proveniencia, o resultado nao e auditavel.
        result["failures"].append(f"sidecar could not be built: {exc}")
        if result["status"] == "passed":
            result["status"] = "failed"
    return result
