"""Visualização + PDF do beta OCRE↔Draw (ficha ``visualizacao-pdf.md``).

Seleção do Senado do beta (fórmula fechada): entram PyVista, meshio,
trimesh, Three.js (viewer, fora deste módulo), Blender, LibreCAD
``dxf2pdf``, ezdxf drawing e Inkscape. FORA: MaterialX, F3D/ParaView/
VTK/VisIt, PdfCraft, CADCraft PLOT, TechDraw.

**REGRA DURA: render nunca é apresentado como análise.** Os números
vêm dos solvers; o visual só traduz. Por isso:

- todo ``.png`` carrega o texto ``VISUALIZAÇÃO — não é análise`` na
  própria imagem, e todo resultado devolve ``visualization_only: True``;
- nenhum campo é inventado: se o resultado do solver não traz o campo
  pedido, a função falha fechada (``VizError``) em vez de colorir com
  um valor qualquer;
- o domínio do fingerprint citado aqui é
  ``DRAW:PAPER-APPROVAL:FINGERPRINT`` (errata do Senado: não
  "DRAW-AF/1", que é só o rótulo da família).

Licenças / isolamento:

- Em processo vivem só libs permissivas: PyVista (MIT), meshio (MIT),
  trimesh (MIT), ezdxf núcleo (MIT), matplotlib (PSF). Nada GPL/AGPL
  é importado.
- LibreCAD (GPL-2.0), Inkscape (GPL-3.0) e Blender (GPL) só via
  subprocesso, com timeout obrigatório e ``QT_QPA_PLATFORM=offscreen``
  onde cabe. O binário ausente devolve ``status == "unavailable"``
  (skip honesto), nunca um passo fingido.
- O extra ``ezdxf[draw]`` inclui PyMuPDF (AGPL): o fallback ezdxf
  aqui usa SÓ ``SVGBackend`` e o backend matplotlib (``qsave``) e
  nunca importa ``fitz``/PyMuPDF — o teste cobre isso.

Spikes medidos nesta máquina (2026-11-05, venv ``.venv-beta``):

- meshio 5.3.5 **NÃO lê ``.frd``** (CalculiX): ``meshio.read`` falha
  com ``ReadError`` (extensão desconhecida) e o formato explícito
  ``file_format='frd'`` falha com ``Unknown file format 'frd'``. O
  job precisa de um conversor antes do PyVista — este módulo traz o
  conversor OCRE-side ``read_frd``/``frd_to_vtu`` (parser ASCII do
  formato CalculiX, só MIT/NumPy), que é a rota do beta. Alternativa
  externa, se um dia preferirem: ``cgx`` (não instalado aqui) lendo o
  ``.frd`` e exportando malha.
- PyVista 0.49.2 ``off_screen=True`` **direto falha** sem display
  (sem EGL, sem OSMesa nesta máquina): o ``screenshot`` chega a dar
  segfault (medido com a malha real), que não se captura com
  ``try/except``. Por isso o render roda num **worker em subprocesso**
  (``render_worker.py``: timeout + arquivo como fronteira) e, se o
  direto falhar, tenta de novo sob ``xvfb-run -a``; o caminho usado
  fica registrado em ``render_backend``.
- Escala mm→m (fator 1000, convenção glTF): ``to_glb`` aplica o
  fator quando ``units="mm"`` (padrão OCRE) e o teste confere a
  caixa reaberta (100×10×10 mm → 0.1×0.01×0.01 m). Nada é afirmado
  sem medir: o fator aplicado e os bounds antes/depois ficam no
  resultado.
- Blender 4.0.2 **foi instalado** (``/usr/bin/blender``, apt, 19:56)
  mas **sumiu às ~20:03 junto com librecad/gmsh/ccx** num reset do
  container (o ``/home`` sobreviveu; ``/usr`` não) — reinstalação via
  apt tentada em seguida (``apt-get update`` lento; ver relatório).
  O ``.glb``/``.png`` do beta não depende dele; só
  ``blender_available()`` o reporta (``None`` enquanto ausente).
  Inkscape **ausente** — o passo SVG→PDF devolve ``"unavailable"``
  sem falhar o resto.
- ``librecad dxf2svg`` TRAVA nesta build (exit 124, fato do
  coordenador); ``dxf2pdf`` FUNCIONA (exit 0, fato do coordenador).
  ``plot_pdf`` usa ``dxf2pdf`` como rota principal e **nunca**
  ``dxf2svg``. Atenção: o binário sumiu no reset acima, por isso o
  teste de ``dxf2pdf`` pula honestamente enquanto ele não voltar —
  a rota ezdxf (fallback) foi a testada de verdade nesta sessão.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FINGERPRINT_DOMAIN",
    "VizError",
    "blender_available",
    "frd_to_vtu",
    "inkscape_available",
    "librecad_available",
    "load_result_mesh",
    "plot_pdf",
    "read_frd",
    "render_field",
    "svg_to_pdf",
    "to_glb",
    "tool_versions",
]

DEFAULT_TIMEOUT_S = 120
FINGERPRINT_DOMAIN = "DRAW:PAPER-APPROVAL:FINGERPRINT"
WATERMARK = "VISUALIZACAO — nao e analise | numeros: solver (ver sidecar)"

# Tipos de elemento CalculiX .frd suportados pelo parser OCRE-side.
# 6 = C3D10 (tetraedro quadrático, 10 nós). Qualquer outro tipo falha
# fechado — o parser não inventa conectividade.
_FRD_ELEMENT_TYPES = {6: ("tetra10", 10)}


class VizError(RuntimeError):
    """Erro fechado de visualização/plot: nada é inventado nem fingido."""


def tool_versions() -> dict:
    """Versões pinadas das ferramentas de visualização (venv .venv-beta)."""
    import importlib.metadata

    names = ("pyvista", "meshio", "trimesh", "matplotlib", "ezdxf", "vtk", "numpy")
    versions = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def blender_available(binary: str = "blender") -> str | None:
    """Devolve a versão do Blender (ex.: ``"4.0.2"``) ou ``None``."""
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    first = (completed.stdout or "").splitlines()
    if first and first[0].lower().startswith("blender"):
        return first[0].split()[-1]
    return "presente (versão não lida)"


def librecad_available(binary: str = "librecad") -> str | None:
    """Devolve o caminho do binário ``librecad`` ou ``None`` se ausente."""
    return shutil.which(binary)


def inkscape_available(binary: str = "inkscape") -> str | None:
    """Devolve o caminho do binário ``inkscape`` ou ``None`` se ausente."""
    return shutil.which(binary)


# ---------------------------------------------------------------- .frd


def read_frd(frd_path: str | Path):
    """Lê um resultado CalculiX ``.frd`` e devolve um ``meshio.Mesh``.

    Parser OCRE-side do formato ASCII do CalculiX (blocos ``2C``
    nós, ``3C`` elementos, ``-4``/``-5``/``-1`` resultados por nó).
    Cada componente de resultado vira uma entrada própria em
    ``point_data`` (``SXX``, ``SYY``, ..., ``D1``, ``D2``, ``D3``);
    ``DISP`` também é guardado como vetor ``(n, 3)``. Nenhum campo
    derivado (ex.: von Mises) é calculado — o visual mostra o que o
    solver escreveu, e só isso.

    Suporta só elemento tipo 6 (C3D10/tetra10); outro tipo levanta
    ``VizError``. Arquivo sem bloco de resultado ainda é lido (malha
    nua, sem campo); ``render_field`` é quem falha fechado nesse caso.
    """
    import numpy as np
    import meshio

    frd_path = Path(frd_path)
    if not frd_path.is_file():
        raise VizError(f".frd não encontrado: {frd_path}")

    points: dict[int, tuple[float, float, float]] = {}
    cells: list[list[int]] = []
    cell_type: str | None = None
    # nome do bloco -> {node_id: [valores...]}; componentes na ordem.
    blocks: dict[str, dict[int, list[float]]] = {}
    block_components: dict[str, list[str]] = {}

    section: str | None = None  # "nodes" | "elements" | "results"
    current_block: str | None = None
    current_ncomp: int = 0
    current_nnodes: int = 0

    with frd_path.open("r", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            # Cabeçalhos de bloco têm indentação fixa ("    2C", "    3C",
            # "    1PSTEP", "  100CL") — testar sem a indentação.
            stripped = line.lstrip()
            if stripped.startswith("2C"):
                section = "nodes"
                continue
            if stripped.startswith("3C"):
                section = "elements"
                continue
            if stripped.startswith("1P"):
                section = "results"
                current_block = None
                continue
            if stripped.startswith("100CL"):
                continue
            if section == "nodes" and line.startswith(" -1"):
                # Largura fixa (valores colados, ex.: 0.00000E+00-5.00000E+00):
                # id em [3:13], x/y/z em campos de 12 a partir de 13.
                nid = int(line[3:13])
                points[nid] = (
                    float(line[13:25]),
                    float(line[25:37]),
                    float(line[37:49]),
                )
                continue
            if section == "elements":
                if line.startswith(" -1"):
                    parts = line.split()
                    etype = int(parts[2])
                    if etype not in _FRD_ELEMENT_TYPES:
                        raise VizError(
                            f".frd {frd_path}: tipo de elemento {etype} não "
                            "suportado pelo parser (só C3D10/tipo 6)"
                        )
                    cell_type, _n = _FRD_ELEMENT_TYPES[etype]
                    current_nnodes = _n
                    continue
                if line.startswith(" -2"):
                    # ids em campos de 10 a partir da coluna 3.
                    cells.append(
                        [
                            int(line[3 + 10 * i : 13 + 10 * i])
                            for i in range(current_nnodes)
                        ]
                    )
                    continue
                if line.startswith(" -3"):
                    section = None
                    continue
            if section == "results":
                if line.startswith(" -4"):
                    parts = line.split()
                    current_block = parts[1]
                    current_ncomp = int(parts[2])
                    blocks.setdefault(current_block, {})
                    block_components.setdefault(current_block, [])
                    continue
                if line.startswith(" -5") and current_block is not None:
                    parts = line.split()
                    block_components[current_block].append(parts[1])
                    continue
                if line.startswith(" -1") and current_block is not None:
                    # Largura fixa: id em [3:13], valores em campos de 12.
                    # O nº de valores vem do comprimento da linha (DISP
                    # declara 4 mas grava 3 por nó; ALL é derivado).
                    nid = int(line[3:13])
                    nvals = (len(line.rstrip()) - 13) // 12
                    values = [
                        float(line[13 + 12 * i : 25 + 12 * i])
                        for i in range(nvals)
                    ]
                    # Nº esperado de valores por nó: nº de componentes -5,
                    # menos 1 se o último for ALL (derivado, não gravado
                    # por nó — padrão de DISP e FORC no .frd do CalculiX).
                    comps = block_components.get(current_block, [])
                    expected = len(comps)
                    if comps and comps[-1] == "ALL":
                        expected -= 1
                    if not expected:
                        expected = current_ncomp
                    if len(values) != expected:
                        raise VizError(
                            f".frd {frd_path}: bloco {current_block} com "
                            f"{len(values)} valores, esperado {expected}"
                        )
                    blocks[current_block][nid] = values
                    continue
                if line.startswith(" -3"):
                    current_block = None
                    continue

    if not points:
        raise VizError(f".frd {frd_path}: nenhum nó lido (bloco 2C ausente?)")
    if not cells:
        raise VizError(f".frd {frd_path}: nenhum elemento lido (bloco 3C ausente?)")

    node_ids = sorted(points)
    index_of = {nid: i for i, nid in enumerate(node_ids)}

    pts = np.array([points[nid] for nid in node_ids], dtype=float)
    conn = np.array(
        [[index_of[nid] for nid in elem] for elem in cells], dtype=np.int64
    )
    point_data: dict[str, object] = {}
    for name, values in blocks.items():
        if set(values) != set(node_ids):
            raise VizError(
                f".frd {frd_path}: bloco {name} não cobre todos os nós "
                f"({len(values)} de {len(node_ids)})"
            )
        arr = np.array([values[nid] for nid in node_ids], dtype=float)
        if name == "DISP" and arr.shape[1] == 3:
            point_data["DISP"] = arr
            for i, comp in enumerate(("D1", "D2", "D3")):
                point_data[comp] = arr[:, i]
        else:
            comps = block_components.get(name) or []
            if comps and comps[-1] == "ALL":
                comps = comps[:-1]
            if len(comps) == arr.shape[1]:
                for i, comp in enumerate(comps):
                    point_data[comp] = arr[:, i]
            point_data[name] = arr
    return meshio.Mesh(
        points=pts,
        cells=[(cell_type or "tetra10", conn)],
        point_data=point_data,
    )


def frd_to_vtu(frd_path: str | Path, vtu_path: str | Path) -> dict:
    """Converte ``.frd`` (CalculiX) em ``.vtu`` via ``read_frd`` + meshio.

    É o conversor que o job precisa porque meshio 5.3.5 não lê
    ``.frd`` (spike medido; ver docstring do módulo).
    """
    import meshio

    mesh = read_frd(frd_path)
    vtu_path = Path(vtu_path)
    vtu_path.parent.mkdir(parents=True, exist_ok=True)
    meshio.write(str(vtu_path), mesh)
    return {
        "frd_path": str(frd_path),
        "vtu_path": str(vtu_path),
        "vtu_bytes": vtu_path.stat().st_size,
        "points": int(mesh.points.shape[0]),
        "cells": sum(len(c.data) for c in mesh.cells),
        "fields": sorted(mesh.point_data.keys()),
        "converter": "ocre_core.draw.viz.read_frd (parser OCRE-side; "
        "meshio 5.3.5 não lê .frd)",
        "visualization_only": True,
        "fingerprint_domain": FINGERPRINT_DOMAIN,
    }


def load_result_mesh(result_path: str | Path):
    """Carrega um resultado FEA e devolve ``(meshio.Mesh, info)``.

    ``.frd`` vai pelo parser OCRE-side; os demais formatos vão pelo
    ``meshio.read`` (``.vtu``, ``.vtk``, ``.msh``, ...). Qualquer
    falha vira ``VizError`` — nunca um mesh vazio fingido.
    """
    import meshio

    result_path = Path(result_path)
    if not result_path.is_file():
        raise VizError(f"resultado FEA não encontrado: {result_path}")
    if result_path.suffix.lower() == ".frd":
        mesh = read_frd(result_path)
        info = {"source": str(result_path), "reader": "ocre read_frd"}
    else:
        try:
            mesh = meshio.read(str(result_path))
        except Exception as exc:  # meshio.ReadError e outros
            raise VizError(
                f"meshio não leu {result_path}: {type(exc).__name__}: {exc}"
            ) from exc
        info = {"source": str(result_path), "reader": "meshio.read"}
    info["points"] = int(mesh.points.shape[0])
    info["cells"] = sum(len(c.data) for c in mesh.cells)
    info["fields"] = sorted(mesh.point_data.keys())
    return mesh, info


# ---------------------------------------------------------------- render


def _resolve_field(mesh, field: str | None, component: int | None) -> str:
    """Escolhe o campo escalar a renderizar, sem inventar cor.

    - ``field`` dado: tem de existir em ``point_data``; se tiver >1
      componente, ``component`` é obrigatório.
    - ``field`` omitido: prefere ``SXX``; senão, usa o único array
      1-D existente; senão falha fechado listando os campos.
    Devolve o nome do array escalar (cria ``<field>[<component>]`` se
    preciso) e o registra no mesh.
    """
    import numpy as np

    point_data = mesh.point_data
    if field is not None:
        if field not in point_data:
            raise VizError(
                f"campo {field!r} ausente no resultado; disponíveis: "
                f"{sorted(point_data.keys()) or 'nenhum (malha nua)'}"
            )
        arr = np.asarray(point_data[field])
    else:
        if "SXX" in point_data:
            field = "SXX"
            arr = np.asarray(point_data[field])
        else:
            scalars_1d = [
                k for k, v in point_data.items() if np.asarray(v).ndim == 1
            ]
            if len(scalars_1d) == 1:
                field = scalars_1d[0]
                arr = np.asarray(point_data[field])
            elif not point_data:
                raise VizError(
                    "resultado sem campo (malha nua): o visual não inventa "
                    "cor — passe um resultado com point_data do solver"
                )
            else:
                raise VizError(
                    "campo ambíguo: passe field= explicitamente; "
                    f"disponíveis: {sorted(point_data.keys())}"
                )
    if arr.ndim == 1:
        return field
    if arr.ndim == 2 and arr.shape[1] > 1:
        if component is None:
            raise VizError(
                f"campo {field!r} tem {arr.shape[1]} componentes; passe "
                "component=<índice 0-based> (ex.: STRESS componente 0 = SXX)"
            )
        if not (0 <= component < arr.shape[1]):
            raise VizError(
                f"component={component} fora de 0..{arr.shape[1] - 1} "
                f"para o campo {field!r}"
            )
        name = f"{field}[{component}]"
        mesh.point_data[name] = arr[:, component]
        return name
    raise VizError(f"campo {field!r} com forma inesperada {arr.shape}")


def _direct_gl_likely() -> bool:
    """True se um render direto tem chance (display ou EGL/OSMesa).

    Sem DISPLAY e sem libEGL/libOSMesa, o ``screenshot`` direto do
    VTK dá segfault (medido) — nesse caso o Xvfb vai primeiro e o
    direto fica só como fallback, para não pagar o crash à toa.
    """
    import ctypes.util

    if os.environ.get("DISPLAY"):
        return True
    return bool(
        ctypes.util.find_library("EGL") or ctypes.util.find_library("OSMesa")
    )


def _render_via_worker(
    vtu_path: Path, field: str, out_png: Path, title: str, timeout_s: float
) -> tuple[str, list[dict]]:
    """Renderiza via worker em subprocesso (segfault do VTK não mata o OCRE).

    Ordem: direto primeiro quando há GL provável; senão ``xvfb-run``
    primeiro e direto como fallback. Devolve ``(backend_usado,
    tentativas)``; levanta ``VizError`` se nenhuma produzir PNG.
    """
    backend_dir = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = backend_dir + os.pathsep + env.get("PYTHONPATH", "")
    worker_cmd = [
        sys.executable,
        "-m",
        "ocre_core.draw.render_worker",
        "--vtu",
        str(vtu_path),
        "--field",
        field,
        "--png",
        str(out_png),
        "--title",
        title,
    ]
    attempts: list[dict] = []
    order = (
        ("worker-direct", worker_cmd),
        ("worker-xvfb-run", ["xvfb-run", "-a"] + worker_cmd),
    )
    if not _direct_gl_likely():
        order = (order[1], order[0])
    for label, command in order:
        if label == "worker-xvfb-run" and shutil.which("xvfb-run") is None:
            attempts.append(
                {"backend": label, "ok": False, "reason": "xvfb-run ausente"}
            )
            continue
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
            ok = (
                completed.returncode == 0
                and out_png.is_file()
                and out_png.stat().st_size > 0
            )
            attempts.append(
                {
                    "backend": label,
                    "ok": ok,
                    "returncode": completed.returncode,
                    "stdout": (completed.stdout or "")[-2000:],
                    "stderr": (completed.stderr or "")[-2000:],
                }
            )
            if ok:
                return label, attempts
        except subprocess.TimeoutExpired:
            attempts.append(
                {"backend": label, "ok": False, "reason": f"timeout {timeout_s}s"}
            )
    raise VizError(f"render PyVista falhou em todas as tentativas: {attempts}")


def render_field(
    resultado_fea: str | Path,
    out_png: str | Path,
    *,
    field: str | None = None,
    component: int | None = None,
    title: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Renderiza o campo escalar de um resultado FEA para ``.png``.

    O ``.png`` é **visualização, não análise**: carrega a marca na
    imagem e ``visualization_only: True`` no retorno. Os valores de
    mínimo/máximo devolvidos são os do solver, só mostrados aqui.

    ``resultado_fea`` pode ser ``.frd`` (via parser OCRE-side +
    ``.vtu`` temporário) ou qualquer formato que ``meshio``/PyVista
    leia (``.vtu`` direto). Sem campo no resultado, falha fechado.
    O render VTK roda num worker em subprocesso (ver
    ``render_worker.py``): sem display/EGL/OSMesa o ``screenshot``
    direto dá segfault, e segfault não se captura em processo.
    """
    import numpy as np

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    mesh, info = load_result_mesh(resultado_fea)
    scalar_name = _resolve_field(mesh, field, component)
    values = np.asarray(mesh.point_data[scalar_name], dtype=float)

    with tempfile.TemporaryDirectory(prefix="ocre-viz-") as tmp:
        vtu_path = Path(tmp) / "result.vtu"
        import meshio

        meshio.write(str(vtu_path), mesh)
        title_text = title or f"{Path(resultado_fea).name} :: {scalar_name}"
        backend, attempts = _render_via_worker(
            vtu_path, scalar_name, out_png, title_text, timeout_s
        )

    return {
        "out_png": str(out_png),
        "png_bytes": out_png.stat().st_size,
        "field": scalar_name,
        "field_min_from_solver": float(values.min()),
        "field_max_from_solver": float(values.max()),
        "points": info["points"],
        "cells": info["cells"],
        "fields_available": info["fields"],
        "mesh_source": info["source"],
        "reader": info["reader"],
        "render_backend": backend,
        "render_attempts": attempts,
        "watermark": WATERMARK,
        "visualization_only": True,
        "note": "Render não é análise: os números vêm do solver; "
        "o visual só traduz o campo em cor.",
        "fingerprint_domain": FINGERPRINT_DOMAIN,
    }


# ---------------------------------------------------------------- .glb


def to_glb(
    malha_ou_step: str | Path,
    out_glb: str | Path,
    *,
    field_values=None,
    field_name: str | None = None,
    units: str = "mm",
) -> dict:
    """Exporta uma malha para ``.glb`` (viewer Three.js), via trimesh.

    - ``is_watertight`` é o pré-voo: registrado no retorno; se a
      entrada não for estanque, exporta mesmo assim mas sinaliza
      (o chamador decide; sólido furado não vira STEP nem volume).
    - Escala mm→m (fator 0.001, convenção glTF) quando
      ``units="mm"`` (padrão OCRE); ``units="m"`` não escala.
    - ``COLOR_0`` só quando ``field_values`` (um valor por vértice)
      é passado: o campo vira cor por vértice (viridis). Sem campo,
      sai geometria nua — nunca se inventa cor.
    - STEP/STP/IGES não entram: trimesh não lê B-rep; isso é trabalho
      do worker 3D (build123d/FreeCAD) — falha fechado.
    """
    import numpy as np
    import trimesh

    src = Path(malha_ou_step)
    if not src.is_file():
        raise VizError(f"malha não encontrada: {src}")
    if src.suffix.lower() in {".step", ".stp", ".iges", ".igs"}:
        raise VizError(
            f"{src.suffix} é B-rep: trimesh não lê STEP/IGES; use o worker "
            "3D (build123d/FreeCAD) para converter antes do .glb"
        )
    try:
        mesh = trimesh.load(str(src), force="mesh")
    except Exception as exc:
        raise VizError(f"trimesh não carregou {src}: {exc}") from exc
    if not isinstance(mesh, trimesh.Trimesh):
        raise VizError(f"{src} não é uma malha única (veio {type(mesh).__name__})")
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise VizError(f"{src}: malha vazia")

    bounds_before = mesh.bounds.copy()
    watertight = bool(mesh.is_watertight)
    if units == "mm":
        scale_factor = 0.001
    elif units == "m":
        scale_factor = 1.0
    else:
        raise VizError(f"units={units!r} desconhecido; use 'mm' ou 'm'")
    if scale_factor != 1.0:
        mesh.apply_scale(scale_factor)

    color_0 = False
    field_range = None
    if field_values is not None:
        values = np.asarray(field_values, dtype=float)
        if values.shape != (len(mesh.vertices),):
            raise VizError(
                f"field_values tem {values.shape}, esperado "
                f"({len(mesh.vertices)},) — um valor por vértice"
            )
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        cmap = plt.get_cmap("viridis")
        lo, hi = float(values.min()), float(values.max())
        norm = (values - lo) / (hi - lo) if hi > lo else np.zeros_like(values)
        rgba = (cmap(norm) * 255).astype(np.uint8)
        mesh.visual.vertex_colors = rgba
        color_0 = True
        field_range = [lo, hi]

    out_glb = Path(out_glb)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(out_glb))
    if not out_glb.is_file() or out_glb.stat().st_size == 0:
        raise VizError(f"trimesh não produziu .glb em {out_glb}")

    # Reabertura de conferência: bounds e COLOR_0 medidos, não afirmados.
    reloaded = trimesh.load(str(out_glb), force="mesh")
    reloaded_bounds = reloaded.bounds
    has_color_0 = False
    try:
        vc = reloaded.visual.vertex_colors
        has_color_0 = vc is not None and len(vc) == len(reloaded.vertices)
    except Exception:
        has_color_0 = False

    return {
        "out_glb": str(out_glb),
        "glb_bytes": out_glb.stat().st_size,
        "is_watertight": watertight,
        "watertight_preflight": "pass" if watertight else "fail",
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "input_units": units,
        "output_units": "m",
        "scale_factor": scale_factor,
        "bounds_before_m": bounds_before.tolist(),
        "bounds_after_m": reloaded_bounds.tolist(),
        "color_0": bool(has_color_0) if field_values is not None else False,
        "field_name": field_name,
        "field_range_from_solver": field_range,
        "visualization_only": True,
        "note": "GLB é visualização para o viewer Three.js; não é análise.",
        "fingerprint_domain": FINGERPRINT_DOMAIN,
    }


# ---------------------------------------------------------------- PDF


def _run_subprocess(command: list[str], timeout_s: float) -> dict:
    """Roda um binário GPL via subprocesso; devolve returncode/stdout/stderr."""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
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
        raise VizError(
            f"{command[0]} excedeu o timeout de {timeout_s}s em "
            f"{command[-1] if command else '?'}"
        ) from exc
    return {
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }


def svg_to_pdf(
    svg_path: str | Path,
    out_pdf: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Fecha SVG→PDF com o Inkscape (subprocesso, GPL-3.0).

    Sem o binário, devolve ``status == "unavailable"`` (skip honesto).
    """
    svg_path = Path(svg_path)
    out_pdf = Path(out_pdf)
    if not svg_path.is_file():
        raise VizError(f"SVG não encontrado: {svg_path}")
    binary = inkscape_available()
    if binary is None:
        return {
            "status": "unavailable",
            "available": False,
            "svg_path": str(svg_path),
            "reason": "binário 'inkscape' não encontrado no PATH; "
            "passo SVG→PDF não rodado (skip honesto, não é aprovação)",
        }
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    result = _run_subprocess(
        [
            binary,
            str(svg_path),
            "--export-type=pdf",
            f"--export-filename={out_pdf}",
            "--export-area-page",
            "--export-pdf-version=1.7",
        ],
        timeout_s,
    )
    if result["returncode"] != 0 or not out_pdf.is_file() or out_pdf.stat().st_size == 0:
        return {
            "status": "fail",
            "available": True,
            "svg_path": str(svg_path),
            "binary": binary,
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "reason": "inkscape saiu com código != 0 ou não produziu PDF",
        }
    return {
        "status": "pass",
        "available": True,
        "svg_path": str(svg_path),
        "binary": binary,
        "out_pdf": str(out_pdf),
        "pdf_bytes": out_pdf.stat().st_size,
    }


def _plot_pdf_librecad(
    dxf_path: Path, out_pdf: Path, timeout_s: float
) -> dict | None:
    """Rota principal: LibreCAD ``dxf2pdf`` (subprocesso, GPL-2.0).

    Devolve o dict de resultado ou ``None`` se o binário não existe.
    Nunca usa ``dxf2svg`` (trava nesta build, exit 124).
    """
    binary = librecad_available()
    if binary is None:
        return None
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    # Forma 1: saída explícita, A4 paisagem, ajuste à página.
    result = _run_subprocess(
        [binary, "dxf2pdf", "-a", "-p", "297x210", "-o", str(out_pdf), str(dxf_path)],
        timeout_s,
    )
    attempt = 1
    if result["returncode"] != 0 or not out_pdf.is_file() or out_pdf.stat().st_size == 0:
        # Forma 2: sem saída explícita, numa cópia em diretório temporário
        # (o LibreCAD escreve ao lado da entrada).
        with tempfile.TemporaryDirectory(prefix="ocre-viz-pdf-") as tmp:
            copy_path = Path(tmp) / dxf_path.name
            copy_path.write_bytes(dxf_path.read_bytes())
            result = _run_subprocess(
                [binary, "dxf2pdf", str(copy_path)], timeout_s
            )
            produced = Path(tmp) / (dxf_path.stem + ".pdf")
            attempt = 2
            if result["returncode"] == 0 and produced.is_file() and produced.stat().st_size:
                shutil.copyfile(produced, out_pdf)
    ok = (
        result["returncode"] == 0
        and out_pdf.is_file()
        and out_pdf.stat().st_size > 0
    )
    return {
        "route": "librecad dxf2pdf",
        "attempt": attempt,
        "ok": ok,
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


def _plot_pdf_ezdxf_matplotlib(dxf_path: Path, out_pdf: Path) -> dict:
    """Fallback: ezdxf drawing, backend matplotlib → PDF direto.

    Núcleo MIT, in-process. NÃO importa PyMuPDF (AGPL): só
    ``qsave``/matplotlib. A escala mm→polegadas é calculada pelo job
    (A4 paisagem = 11.69×8.27 pol).
    """
    import ezdxf
    from ezdxf.addons.drawing.matplotlib import qsave

    doc = ezdxf.readfile(str(dxf_path))
    qsave(doc.modelspace(), str(out_pdf), size_inches=(11.69, 8.27), dpi=300)
    ok = out_pdf.is_file() and out_pdf.stat().st_size > 0
    return {"route": "ezdxf drawing (matplotlib)", "ok": ok}


def _plot_pdf_ezdxf_svg_inkscape(dxf_path: Path, out_pdf: Path, timeout_s: float) -> dict:
    """Fallback 2: ezdxf SVGBackend → SVG → Inkscape → PDF."""
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.svg import SVGBackend

    doc = ezdxf.readfile(str(dxf_path))
    backend = SVGBackend()
    Frontend(RenderContext(doc), backend).draw_layout(doc.modelspace())
    with tempfile.TemporaryDirectory(prefix="ocre-viz-svg-") as tmp:
        svg_path = Path(tmp) / (dxf_path.stem + ".svg")
        svg_path.write_text(backend.get_string(), encoding="utf-8")
        result = svg_to_pdf(svg_path, out_pdf, timeout_s=timeout_s)
    if result["status"] == "pass":
        return {"route": "ezdxf drawing (SVGBackend) + inkscape", "ok": True}
    return {
        "route": "ezdxf drawing (SVGBackend) + inkscape",
        "ok": False,
        "reason": result.get("reason", result["status"]),
    }


def plot_pdf(
    dxf_path: str | Path,
    out_pdf: str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Plota um DXF em PDF (folha A4 paisagem).

    Rotas, nesta ordem: (1) LibreCAD ``dxf2pdf`` (principal, GPL via
    subprocesso); (2) ezdxf drawing matplotlib → PDF (MIT, sem
    PyMuPDF); (3) ezdxf SVGBackend → Inkscape → PDF. A rota usada e
    as tentativas ficam no retorno. Se todas falharem, ``VizError``
    — nunca um PDF vazio fingido.
    """
    dxf_path = Path(dxf_path)
    out_pdf = Path(out_pdf)
    if not dxf_path.is_file():
        raise VizError(f"DXF não encontrado: {dxf_path}")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[dict] = []

    librecad = _plot_pdf_librecad(dxf_path, out_pdf, timeout_s)
    if librecad is not None:
        attempts.append(librecad)
        if librecad["ok"]:
            return _pdf_result(out_pdf, attempts)
    else:
        attempts.append({"route": "librecad dxf2pdf", "ok": False,
                         "reason": "binário 'librecad' ausente"})

    try:
        fallback = _plot_pdf_ezdxf_matplotlib(dxf_path, out_pdf)
    except Exception as exc:  # noqa: BLE001 — fallback registra e segue
        fallback = {
            "route": "ezdxf drawing (matplotlib)",
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    attempts.append(fallback)
    if fallback["ok"]:
        return _pdf_result(out_pdf, attempts)

    try:
        fallback2 = _plot_pdf_ezdxf_svg_inkscape(dxf_path, out_pdf, timeout_s)
    except Exception as exc:  # noqa: BLE001 — última rota; erro vira VizError
        fallback2 = {
            "route": "ezdxf drawing (SVGBackend) + inkscape",
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    attempts.append(fallback2)
    if fallback2["ok"]:
        return _pdf_result(out_pdf, attempts)

    raise VizError(
        f"plot_pdf falhou em todas as rotas para {dxf_path}: {attempts}"
    )


def _pdf_result(out_pdf: Path, attempts: list[dict]) -> dict:
    used = next(a for a in attempts if a.get("ok"))
    return {
        "out_pdf": str(out_pdf),
        "pdf_bytes": out_pdf.stat().st_size,
        "route": used["route"],
        "attempts": attempts,
        "paper": "A4 paisagem (297x210 mm)",
        "visualization_only": True,
        "note": "Plot da folha; não é análise.",
        "fingerprint_domain": FINGERPRINT_DOMAIN,
    }
