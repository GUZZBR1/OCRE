"""PNG de teste mínimo e válido, só stdlib (struct + zlib).

Os testes de CLI precisam de PNGs reais agora que ``export`` valida o
IHDR contra o canvas do ``PaperSpec`` — os placeholders de magic bytes
não passam mais. Escreve um RGB branco de ``w`` x ``h`` px.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    out = struct.pack(">I", len(data)) + kind + data
    return out + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: Path, width: int = 850, height: int = 1200) -> Path:
    raw = b"\x00" + b"\xff" * (width * 3)
    idat = zlib.compress(raw * height, level=6)
    png = (
        PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return path
