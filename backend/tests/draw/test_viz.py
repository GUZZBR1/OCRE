"""Testes de visualização + PDF do beta (``ocre_core.draw.viz``).

Regras do módulo sob teste (ver ``viz.py``):

- render nunca é análise: todo resultado traz ``visualization_only``;
- sem campo no resultado, falha fechado — nunca se inventa cor;
- meshio 5.3.5 NÃO lê ``.frd`` (spike medido de verdade, não mock);
- LibreCAD/Inkscape/Blender só via subprocesso; binário ausente é
  skip honesto, nunca passo fingido;
- o extra ``ezdxf[draw]`` inclui PyMuPDF (AGPL): o fallback ezdxf
  nunca importa ``fitz`` — coberto aqui.

Fixture ``axial_bar_h5mm.frd``: cópia do run do worker FEA
(ver ``fixtures/README-viz.md``) — barra axial 100×10×10 mm,
S11 de referência 21.0 MPa, observado 21.0 MPa no ``metrics.json``
do run de origem.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest

from ocre_core.draw import viz
from ocre_core.draw.viz import VizError

# meshio é dep opcional (requirements-viz.txt): ausente = skip honesto
# do módulo inteiro, mesma regra dos binários externos (ver docstring).
meshio = pytest.importorskip("meshio")

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FRD = FIXTURES / "axial_bar_h5mm.frd"

PNG_MAGIC = b"\x89PNG"
PDF_MAGIC = b"%PDF"


def make_rect_dxf(path: Path) -> Path:
    """DXF R2000 mínimo: retângulo 100×10 mm, 4 linhas."""
    import ezdxf

    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_line((100, 0), (100, 10))
    msp.add_line((100, 10), (0, 10))
    msp.add_line((0, 10), (0, 0))
    doc.saveas(str(path))
    return path


def make_bare_vtu(path: Path) -> Path:
    """VTU de malha nua (sem point_data): render tem de falhar fechado."""
    points = np.array(
        [
            [0.0, 0.0, 0.0],
            [100.0, 0.0, 0.0],
            [100.0, 10.0, 0.0],
            [0.0, 10.0, 0.0],
            [0.0, 0.0, 10.0],
            [100.0, 0.0, 10.0],
            [100.0, 10.0, 10.0],
            [0.0, 10.0, 10.0],
        ]
    )
    cells = [("hexahedron", np.array([[0, 1, 2, 3, 4, 5, 6, 7]]))]
    meshio.write(str(path), meshio.Mesh(points, cells))
    return path


class TestMeshioFrdSpike(unittest.TestCase):
    """Spike do Senado, documentado com o arquivo real (não mock)."""

    def test_meshio_nao_le_frd_nesta_versao(self):
        self.assertEqual(viz.tool_versions()["meshio"], "5.3.5")
        with self.assertRaises(meshio.ReadError):
            meshio.read(str(FRD))
        with self.assertRaises(meshio.ReadError):
            meshio.read(str(FRD), file_format="frd")

    def test_read_frd_parser_ocre_le_fixture_real(self):
        mesh, info = viz.load_result_mesh(FRD)
        self.assertEqual(info["reader"], "ocre read_frd")
        self.assertEqual(info["points"], 998)
        self.assertEqual(info["cells"], 433)
        self.assertEqual(mesh.cells[0].type, "tetra10")
        for field in ("SXX", "SYY", "SZZ", "STRESS", "DISP", "D1"):
            self.assertIn(field, info["fields"])
        # S11 do caso axial: 21.0 MPa (metrics.json do run de origem).
        sxx = np.asarray(mesh.point_data["SXX"])
        self.assertAlmostEqual(float(sxx.min()), 21.0, places=2)
        self.assertAlmostEqual(float(sxx.max()), 21.0, places=2)

    def test_read_frd_malformado_levanta_vizerror(self):
        # .frd quebrado (campo não numérico, elemento apontando para nó
        # inexistente, cabeçalho curto) era ValueError/IndexError/
        # KeyError cru — crash do beta-run. Agora é VizError.
        ok = (
            "    2C                           4                                     1\n"
            " -1         1 0.00000E+00 0.00000E+00 0.00000E+00\n"
            " -1         2 1.00000E+00 0.00000E+00 0.00000E+00\n"
            " -1         3 0.00000E+00 1.00000E+00 0.00000E+00\n"
            " -1         4 0.00000E+00 0.00000E+00 1.00000E+00\n"
            " -3\n"
            "    3C                           1                                     1\n"
            " -1         1    3    0    1\n"
            " -2         1         2         3         4\n"
            " -3\n"
        )
        variants = (
            ok.replace("1.00000E+00 0", "XXXXXXXXXXXX 0"),   # float() cru
            ok.replace("         4\n -3", "        99\n -3"),  # index_of[nid]
            ok.replace(" -1         1    3    0    1", " -1         1"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "ok.frd"
            good.write_text(ok, encoding="utf-8")
            self.assertEqual(viz.read_frd(good).points.shape, (4, 3))
            for i, text in enumerate(variants):
                bad = Path(tmp) / f"bad{i}.frd"
                bad.write_text(text, encoding="utf-8")
                with self.subTest(variant=i), self.assertRaises(VizError):
                    viz.read_frd(bad)

    def test_read_frd_tipos_de_elemento_mistos_viram_dois_blocos(self):
        # Um .frd pode misturar tipos (ex.: C3D4 + C3D10 no mesmo caso):
        # antes, todas as células herdavam o tipo do ÚLTIMO bloco lido
        # e o np.array rachado saía como "malformado" — arquivo válido
        # rejeitado. Agora cada tipo vira seu próprio bloco meshio.
        def nodeline(i, x, y, z):
            return f" -1{i:>10d}{x:>12.5E}{y:>12.5E}{z:>12.5E}"

        coords = [
            (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1),          # tetra
            (2, 0, 0), (3, 0, 0), (2, 1, 0), (2, 0, 1),           # tetra10
            (2.5, 0, 0), (2.5, 0.5, 0), (2, 0.5, 0), (2.5, 0, 0.5),
            (2, 0.5, 0.5), (2, 0, 0.5),
        ]
        nodes = "\n".join(
            nodeline(i, *c) for i, c in enumerate(coords, start=1))
        elems = "\n".join([
            f" -1{1:>10d}    3    0    1",
            " -2" + "".join(f"{n:>10d}" for n in (1, 2, 3, 4)),
            f" -1{2:>10d}    6    0    1",
            " -2" + "".join(f"{n:>10d}" for n in range(5, 15)),
        ])
        frd = (
            f"    2C                          14                                     1\n"
            f"{nodes}\n -3\n"
            "    3C                           2                                     1\n"
            f"{elems}\n -3\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.frd"
            path.write_text(frd)
            mesh = viz.read_frd(path)
        blocks = {c.type: c.data.shape for c in mesh.cells}
        self.assertEqual(blocks, {"tetra": (1, 4), "tetra10": (1, 10)})

    def test_frd_to_vtu_preserva_malha_e_campos(self):
        with tempfile.TemporaryDirectory() as tmp:
            vtu = Path(tmp) / "axial.vtu"
            result = viz.frd_to_vtu(FRD, vtu)
            self.assertTrue(vtu.is_file())
            self.assertGreater(result["vtu_bytes"], 0)
            self.assertIn("SXX", result["fields"])
            back = meshio.read(str(vtu))
            self.assertEqual(back.points.shape[0], 998)
            self.assertEqual(sum(len(c.data) for c in back.cells), 433)
            np.testing.assert_allclose(
                np.asarray(back.point_data["SXX"]),
                np.asarray(viz.read_frd(FRD).point_data["SXX"]),
            )


class TestRenderField(unittest.TestCase):
    """Render PyVista do campo do solver para .png."""

    def test_render_sxx_produz_png_nao_vazio(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "axial-sxx.png"
            result = viz.render_field(FRD, out, field="SXX")
            self.assertTrue(out.is_file())
            self.assertGreater(result["png_bytes"], 0)
            self.assertTrue(out.read_bytes().startswith(PNG_MAGIC))
            self.assertTrue(result["visualization_only"])
            self.assertEqual(result["field"], "SXX")
            self.assertAlmostEqual(result["field_min_from_solver"], 21.0, places=2)
            self.assertAlmostEqual(result["field_max_from_solver"], 21.0, places=2)
            self.assertEqual(
                result["fingerprint_domain"], "DRAW:PAPER-APPROVAL:FINGERPRINT"
            )
            self.assertIn(
                result["render_backend"], ("worker-direct", "worker-xvfb-run")
            )

    def test_render_sem_campo_falha_fechado(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = make_bare_vtu(Path(tmp) / "bare.vtu")
            with self.assertRaises(VizError):
                viz.render_field(bare, Path(tmp) / "bare.png")

    def test_render_campo_inexistente_falha_fechado(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VizError):
                viz.render_field(FRD, Path(tmp) / "x.png", field="VON_MISES")

    def test_campo_multicomponente_exige_componente(self):
        mesh, _info = viz.load_result_mesh(FRD)
        with self.assertRaises(VizError):
            viz._resolve_field(mesh, "DISP", None)
        with self.assertRaises(VizError):
            viz._resolve_field(mesh, "STRESS", None)
        name = viz._resolve_field(mesh, "STRESS", 0)
        self.assertEqual(name, "STRESS[0]")
        np.testing.assert_allclose(
            np.asarray(mesh.point_data[name]),
            np.asarray(mesh.point_data["SXX"]),
        )


class TestToGlb(unittest.TestCase):
    """Export .glb (trimesh) para o viewer Three.js."""

    def _box_stl(self, tmp: Path) -> Path:
        import trimesh

        box = trimesh.creation.box(extents=[100.0, 10.0, 10.0])
        self.assertTrue(box.is_watertight)
        stl = tmp / "box.stl"
        box.export(str(stl))
        return stl

    def test_glb_watertight_e_escala_mm_para_m_medida(self):
        import trimesh

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "box.glb"
            result = viz.to_glb(self._box_stl(tmp), out)
            self.assertTrue(out.is_file())
            self.assertGreater(result["glb_bytes"], 0)
            self.assertTrue(result["is_watertight"])
            self.assertEqual(result["watertight_preflight"], "pass")
            self.assertEqual(result["scale_factor"], 0.001)
            self.assertFalse(result["color_0"])  # sem campo, sem cor inventada
            # Caixa 100×10×10 mm reaberta: 0.1×0.01×0.01 m (medido).
            reloaded = trimesh.load(str(out), force="mesh")
            extents = reloaded.bounds[1] - reloaded.bounds[0]
            np.testing.assert_allclose(extents, [0.1, 0.01, 0.01], atol=1e-6)

    def test_glb_com_campo_tem_color_0(self):
        import trimesh

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stl = self._box_stl(tmp)
            box = trimesh.load(str(stl), force="mesh")
            values = np.linspace(0.0, 21.0, len(box.vertices))
            result = viz.to_glb(
                stl, tmp / "box-field.glb", field_values=values, field_name="SXX"
            )
            self.assertTrue(result["color_0"])
            self.assertEqual(result["field_range_from_solver"], [0.0, 21.0])
            reloaded = trimesh.load(str(tmp / "box-field.glb"), force="mesh")
            colors = reloaded.visual.vertex_colors
            self.assertEqual(len(colors), len(reloaded.vertices))

    def test_glb_step_falha_fechado(self):
        with tempfile.TemporaryDirectory() as tmp:
            step = Path(tmp) / "peca.step"
            step.write_text("ISO-10303-21;\n", encoding="utf-8")
            with self.assertRaises(VizError):
                viz.to_glb(step, Path(tmp) / "peca.glb")

    def test_glb_field_values_com_tamanho_errado_falha(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with self.assertRaises(VizError):
                viz.to_glb(
                    self._box_stl(tmp),
                    tmp / "x.glb",
                    field_values=np.zeros(3),
                )


class TestPlotPdf(unittest.TestCase):
    """Plot DXF→PDF: LibreCAD dxf2pdf (principal) / ezdxf (fallback)."""

    def test_plot_pdf_dxf2pdf_quando_librecad_existe(self):
        if viz.librecad_available() is None:
            self.skipTest("librecad ausente nesta máquina (skip honesto)")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "folha.pdf"
            result = viz.plot_pdf(make_rect_dxf(tmp / "rect.dxf"), out)
            self.assertEqual(result["route"], "librecad dxf2pdf")
            self.assertTrue(out.is_file())
            self.assertGreater(result["pdf_bytes"], 0)
            self.assertTrue(out.read_bytes().startswith(PDF_MAGIC))

    def test_plot_pdf_fallback_ezdxf_sem_pymupdf(self):
        # Força o fallback monkeypatchando a detecção do LibreCAD.
        original = viz.librecad_available
        viz.librecad_available = lambda binary="librecad": None  # noqa: E731
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                out = tmp / "folha-fallback.pdf"
                result = viz.plot_pdf(make_rect_dxf(tmp / "rect.dxf"), out)
                self.assertEqual(result["route"], "ezdxf drawing (matplotlib)")
                self.assertTrue(out.is_file())
                self.assertGreater(result["pdf_bytes"], 0)
                self.assertTrue(out.read_bytes().startswith(PDF_MAGIC))
                self.assertNotIn("fitz", sys.modules)
                self.assertNotIn("pymupdf", sys.modules)
        finally:
            viz.librecad_available = original

    def test_plot_pdf_dxf_inexistente_falha_fechado(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VizError):
                viz.plot_pdf(Path(tmp) / "nao-existe.dxf", Path(tmp) / "x.pdf")


class TestSvgToPdfAndTools(unittest.TestCase):
    """Inkscape (fecha SVG→PDF) e disponibilidade de ferramentas."""

    def test_svg_to_pdf_sem_inkscape_e_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            svg = tmp / "folha.svg"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                'height="100"><rect width="100" height="100"/></svg>',
                encoding="utf-8",
            )
            result = viz.svg_to_pdf(svg, tmp / "folha.pdf")
            if viz.inkscape_available() is None:
                self.assertEqual(result["status"], "unavailable")
                self.assertFalse(result["available"])
            else:
                self.assertEqual(result["status"], "pass")
                pdf = tmp / "folha.pdf"
                self.assertTrue(pdf.is_file())
                self.assertGreater(result["pdf_bytes"], 0)
                self.assertTrue(pdf.read_bytes().startswith(PDF_MAGIC))

    def test_versoes_pinadas(self):
        versions = viz.tool_versions()
        self.assertEqual(versions["pyvista"], "0.49.2")
        self.assertEqual(versions["meshio"], "5.3.5")
        self.assertEqual(versions["trimesh"], "5.2.0")
        self.assertEqual(versions["matplotlib"], "3.11.2")
        self.assertEqual(versions["ezdxf"], "1.4.5")

    def test_blender_reportado_sem_falhar(self):
        # Instalado (4.0.2) ou ausente: o retorno é versão ou None,
        # nunca exceção — o .glb/.png do beta não depende dele.
        version = viz.blender_available()
        self.assertTrue(version is None or isinstance(version, str))


class TestReadFrdC3D4(unittest.TestCase):
    """O .frd do caso axial FEA do beta (C3D4, tipo 3) também é lido.

    O deck próprio do FEA usa tetraedros lineares C3D4 e o .frd do
    ccx traz elementos tipo 3 (4 nós) — o parser OCRE-side aceita
    tipo 3 (``tetra``) e tipo 6 (``tetra10``). Sem isso o ``beta-run``
    não renderizaria o campo do caso axial.
    """

    def test_read_frd_do_caso_axial_c3d4(self):
        import shutil

        if shutil.which("gmsh") is None or shutil.which("ccx") is None:
            self.skipTest("gmsh/ccx ausentes — .frd C3D4 não gerado")
        from ocre_core.draw.fea import build_bar_step, run_axial_case

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            built = build_bar_step(tmp_path / "gen")
            self.assertTrue(built["ok"], built.get("failure"))
            result = run_axial_case(built["step_path"], tmp_path / "case")
            self.assertEqual(result["status"], "passed", result)
            frd = tmp_path / "case" / "job.frd"
            mesh, info = viz.load_result_mesh(frd)
            self.assertEqual(info["reader"], "ocre read_frd")
            self.assertEqual(mesh.cells[0].type, "tetra")
            self.assertEqual(info["points"], result["measured"]["n_nodes"])
            self.assertEqual(info["cells"],
                             result["measured"]["n_elements"])
            for field in ("SXX", "SYY", "SZZ", "STRESS", "DISP", "D1"):
                self.assertIn(field, info["fields"])
            sxx = np.asarray(mesh.point_data["SXX"])
            self.assertAlmostEqual(float(sxx.min()), 21.0, places=2)
            self.assertAlmostEqual(float(sxx.max()), 21.0, places=2)


if __name__ == "__main__":
    unittest.main()
