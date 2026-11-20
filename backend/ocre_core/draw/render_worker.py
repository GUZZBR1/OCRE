"""Worker de render PyVista (rodado em subprocesso por ``viz.render_field``).

O render VTK roda AQUI, num processo separado, porque sem contexto GL
(sem display, sem EGL/OSMesa nesta máquina) o ``Plotter.screenshot``
pode dar segfault — e segfault não se captura com ``try/except``: ele
mataria o processo do OCRE. Em subprocesso, o crash mata só o worker e
``render_field`` tenta de novo sob Xvfb (ou falha honestamente).

Uso (só via ``viz.render_field``, nunca à mão no caminho padrão)::

    python -m ocre_core.draw.render_worker \
        --vtu resultado.vtu --field SXX --png saida.png [--title ...]

Saída: exit 0 + PNG existente e não vazio; qualquer outra coisa é
falha (stderr traz o motivo). Este módulo só importa PyVista + stdlib.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WATERMARK = "VISUALIZACAO — nao e analise | numeros: solver (ver sidecar)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vtu", required=True)
    parser.add_argument("--field", required=True)
    parser.add_argument("--png", required=True)
    parser.add_argument("--title", default="")
    args = parser.parse_args(argv)

    import pyvista as pv

    out_png = Path(args.png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    grid = pv.read(args.vtu)
    if args.field not in grid.point_data:
        print(f"campo {args.field!r} ausente no .vtu", file=sys.stderr)
        return 1
    plotter = pv.Plotter(off_screen=True)
    try:
        plotter.add_mesh(grid, scalars=args.field, show_edges=False)
        plotter.add_text(WATERMARK, font_size=10, position="upper_left")
        if args.title:
            plotter.add_text(args.title, font_size=9, position="lower_left")
        plotter.screenshot(str(out_png))
    finally:
        plotter.close()
    if not out_png.is_file() or out_png.stat().st_size == 0:
        print("PyVista não produziu PNG", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
