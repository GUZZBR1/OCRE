#!/usr/bin/env python3
"""Gerador do ``canvas.png`` da fixture beta-exemplo (só stdlib).

Desenha dois retângulos pretos (traço 6 px) sobre fundo branco num
canvas 850x1200 px (o canvas A4 do Draw, 850x1200) e escreve um PNG
RGB com zlib — sem PIL/OpenCV, para a fixture ser reproduzível em
qualquer máquina. Os retângulos em px correspondem, na escala do
papel A4 (210x297 mm), aos dois retângulos do ``document.json``:

- A: cantos (10, 30)-(90, 70) mm  -> px x 40..364, y 121..283
- B: cantos (120, 30)-(180, 70) mm -> px x 485..728, y 121..283

(escala 850/210 = 4.0476 px/mm em x; 1200/297 = 4.0404 px/mm em y;
o PNG é só a entrada visual — o documento vem do vectorize, não do
PNG; o fake_vectorize da fixture nem lê o PNG.)

Uso: ``python make_canvas.py [saida.png]`` (default: canvas.png aqui).
"""
import struct
import sys
import zlib
from pathlib import Path

W, H = 850, 1200
SCALE_X = W / 210.0
SCALE_Y = H / 297.0


def _mm_to_px(x_mm: float, y_mm: float) -> tuple[int, int]:
    return round(x_mm * SCALE_X), round(y_mm * SCALE_Y)


def _rect_outline(buf: bytearray, x0: int, y0: int, x1: int, y1: int,
                  thick: int = 6) -> None:
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if x - x0 < thick or x1 - x < thick or y - y0 < thick or y1 - y < thick:
                i = (y * W + x) * 3
                buf[i] = buf[i + 1] = buf[i + 2] = 0


def _chunk(kind: bytes, data: bytes) -> bytes:
    out = struct.pack(">I", len(data)) + kind + data
    return out + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def main(argv=None) -> int:
    out = Path(argv[1] if argv and len(argv) > 1 else
               Path(__file__).resolve().parent / "canvas.png")
    buf = bytearray(b"\xff" * (W * H * 3))
    for (ax, ay), (bx, by) in ((((10.0, 30.0), (90.0, 70.0)),
                                ((120.0, 30.0), (180.0, 70.0)))):
        x0, y0 = _mm_to_px(ax, ay)
        x1, y1 = _mm_to_px(bx, by)
        _rect_outline(buf, x0, y0, x1, y1)
    raw = b"".join(b"\x00" + bytes(buf[y * W * 3:(y + 1) * W * 3])
                   for y in range(H))
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, level=6))
           + _chunk(b"IEND", b""))
    out.write_bytes(png)
    print(f"{out}: {len(png)} bytes, {W}x{H} RGB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
