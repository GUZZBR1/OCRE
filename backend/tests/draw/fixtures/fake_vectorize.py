#!/usr/bin/env python3
"""FIXTURE DE TESTE — NÃO É O vectorize REAL DO DRAW.

Imita apenas a interface de linha de comando de
``tools/vectorize.py`` (``input.png --json out.json [--out out.svg]``)
e devolve um payload fixo no mesmo formato
(``{"summary": {...}, "primitives": [...]}``), para testar o runner
sem depender do Draw, do OpenCV ou de um PNG real.

Primitivas fixas (em mm, y-down, como o vectorize real emite):
- um retângulo (``corners_mm``),
- uma polilinha aberta (``points_mm``),
- um círculo (``cx_mm``/``cy_mm``/``r_mm``, SEM pontos — exercita o
  caminho ``primitives_without_points`` do runner),
- uma linha (``points_mm``).

Uso: ``python fake_vectorize.py canvas.png --json saida.json [--out saida.svg]``
"""

import argparse
import json
import sys
from pathlib import Path

PRIMITIVES = [
    {
        "type": "rectangle",
        "center_mm": [50.0, 50.0],
        "width_mm": 80.0,
        "height_mm": 40.0,
        "angle_deg": 0.0,
        "corners_mm": [[10.0, 30.0], [90.0, 30.0], [90.0, 70.0], [10.0, 70.0]],
        "closed": True,
    },
    {
        "type": "polyline",
        "vertices": 3,
        "points_mm": [[120.0, 10.0], [150.0, 40.0], [180.0, 10.0]],
        "closed": False,
    },
    {
        "type": "circle",
        "cx_mm": 150.0,
        "cy_mm": 150.0,
        "r_mm": 25.0,
        "radial_rmse_px": 0.5,
        "closed": True,
    },
    {
        "type": "line",
        "points_mm": [[10.0, 200.0], [100.0, 200.0]],
        "closed": False,
    },
]


def main(argv=None):
    parser = argparse.ArgumentParser(description="FIXTURE: fake do vectorize.py")
    parser.add_argument("input", help="PNG do canvas de tinta (não lido)")
    parser.add_argument("--out", default=None, help="SVG de saída")
    parser.add_argument("--json", default=None, help="JSON de saída")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--stamps", default=None)
    parser.add_argument("--epsilon", type=float, default=1.5)
    parser.add_argument("--thinning", default="zhangsuen")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--no-json", action="store_true",
                        help="sai com 0 sem escrever o JSON (testa fail-closed)")
    parser.add_argument("--bad-json", action="store_true",
                        help="escreve JSON inválido (testa fail-closed)")
    parser.add_argument("--fail", action="store_true",
                        help="sai com código 3 (testa fail-closed)")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="dorme N segundos (testa timeout)")
    args = parser.parse_args(argv)

    if args.sleep:
        import time

        time.sleep(args.sleep)
    if args.fail:
        print("fake vectorize: falha simulada", file=sys.stderr)
        return 3
    if args.out:
        Path(args.out).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n', encoding="utf-8"
        )
    if args.json and not args.no_json:
        if args.bad_json:
            Path(args.json).write_text("{isto não é json", encoding="utf-8")
        else:
            payload = {
                "summary": {
                    "pixels_tinta": 1234,
                    "polilinhas": 4,
                    "polilinhas_fechadas": 2,
                    "primitivas_contagem": {
                        "rectangle": 1,
                        "polyline": 1,
                        "circle": 1,
                        "line": 1,
                    },
                },
                "primitives": PRIMITIVES,
            }
            Path(args.json).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    print("fake vectorize: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
