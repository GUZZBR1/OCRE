"""Testes do gate G0.2 (LibreCAD via subprocesso).

Ressalva explícita: o binário ``librecad`` NÃO existe nesta máquina —
o comportamento "não disponível" é testado de verdade, e os caminhos
pass/fail são testados com um STUB executável que imita a CLI
(``librecad dxf2svg <dxf> [svg]``). Nenhum teste aqui prova que o
LibreCAD real abre um DXF; o gate real (10/10) só existe na bateria
G0.2, ainda não rodada.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from ocre_core.draw.librecad_gate import check_dxf, gate_batch, librecad_available

STUB_PASS = """#!/bin/sh
# Stub: imita `librecad dxf2svg <dxf> [svg]` com sucesso.
# Com saída explícita (4 args) escreve nela; sem ela (3 args),
# escreve <dxf>.svg ao lado da entrada.
if [ "$1" != "dxf2svg" ]; then echo "uso: librecad dxf2svg <dxf> [svg]" >&2; exit 2; fi
if [ -n "$3" ]; then OUT="$3"; else OUT="${2%.dxf}.svg"; fi
printf '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>\\n' > "$OUT"
exit 0
"""

STUB_FAIL = """#!/bin/sh
echo "librecad: erro simulado ao abrir o DXF" >&2
exit 1
"""

STUB_ONLY_IMPLICIT = """#!/bin/sh
# Stub: recusa a forma com saída explícita, aceita só `dxf2svg <dxf>`.
if [ -n "$3" ]; then echo "uso: librecad dxf2svg <dxf>" >&2; exit 2; fi
printf '<svg xmlns="http://www.w3.org/2000/svg"/>' > "${2%.dxf}.svg"
exit 0
"""

DXF_MINIMAL = """  0
SECTION
  2
HEADER
  9
$ACADVER
  1
AC1009
  9
$INSUNITS
 70
     4
  0
ENDSEC
  0
EOF
"""


def make_stub(tmp: Path, name: str, content: str) -> Path:
    path = tmp / name
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class LibreCADGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dxf = self.tmp / "teste.dxf"
        self.dxf.write_text(DXF_MINIMAL, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_binary_absent_returns_unavailable_not_pass(self):
        result = check_dxf(self.dxf, binary="librecad-inexistente-xyz")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["available"])
        self.assertIn("não encontrado", result["reason"])

    def test_librecad_available_returns_none_when_absent(self):
        self.assertIsNone(librecad_available("librecad-inexistente-xyz"))

    def test_stub_pass_returns_pass_with_svg_bytes(self):
        stub = make_stub(self.tmp, "librecad", STUB_PASS)
        result = check_dxf(self.dxf, binary=str(stub))
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["available"])
        self.assertGreater(result["svg_bytes"], 0)
        self.assertEqual(result["attempt"], 1)

    def test_stub_fail_returns_fail_with_stderr(self):
        stub = make_stub(self.tmp, "librecad", STUB_FAIL)
        result = check_dxf(self.dxf, binary=str(stub))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["available"])
        self.assertIn("erro simulado", result["stderr"])

    def test_fallback_to_implicit_output_form(self):
        stub = make_stub(self.tmp, "librecad", STUB_ONLY_IMPLICIT)
        result = check_dxf(self.dxf, binary=str(stub))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["attempt"], 2)

    def test_missing_dxf_raises(self):
        stub = make_stub(self.tmp, "librecad", STUB_PASS)
        with self.assertRaises(Exception):
            check_dxf(self.tmp / "ausente.dxf", binary=str(stub))

    def test_gate_batch_unavailable_when_binary_absent(self):
        summary = gate_batch(
            [self.dxf, self.dxf], binary="librecad-inexistente-xyz"
        )
        self.assertEqual(summary["verdict"], "unavailable")
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["unavailable"], 2)
        self.assertEqual(summary["passed"], 0)

    def test_gate_batch_pass_only_when_all_pass(self):
        stub = make_stub(self.tmp, "librecad", STUB_PASS)
        summary = gate_batch([self.dxf, self.dxf], binary=str(stub))
        self.assertEqual(summary["verdict"], "pass")
        self.assertEqual(summary["passed"], 2)

    def test_gate_batch_fail_when_any_fails(self):
        stub = make_stub(self.tmp, "librecad", STUB_FAIL)
        summary = gate_batch([self.dxf], binary=str(stub))
        self.assertEqual(summary["verdict"], "fail")
        self.assertEqual(summary["failed"], 1)


if __name__ == "__main__":
    unittest.main()
