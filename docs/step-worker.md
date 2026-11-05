# Worker STEP (build123d) — beta v0.1

Documento ``draw.paper.v1`` -> sólido STEP, via worker build123d em
subprocesso. Decisão do Senado (corrida senado-corrida-beta, fórmula
fechada, A 12 | B 9 | C 0): **build123d é o worker 3D único pinado;
CadQuery é plano B e NUNCA pode ser instalado no mesmo venv**
(conflito OCP: ``cadquery-ocp`` e ``cadquery-ocp-novtk`` instalam o
mesmo pacote de topo ``OCP/`` — o último instalado vence e quebra o
outro). FreeCAD é o oráculo STEP.

"Aprovado" = selecionado para o beta provisório, NÃO provado contra
conjunto selado. Seja honesto nos gaps: este módulo NÃO prova nada
contra conjunto real selado.

## Arquivos

| Arquivo | Papel |
|---|---|
| `backend/ocre_core/draw/step_export.py` | Host OCRE (só stdlib). Valida o documento, traduz traços em perfis, chama o worker, confere o volume. NUNCA importa build123d/OCP. |
| `backend/ocre_core/draw/step_worker.py` | Worker: roda SÓ como script sob `.venv-beta/bin/python`. Importa build123d/OCP, faz perfil -> `extrude` -> STEP, BRepCheck, DXF AC1015. |
| `backend/tests/draw/test_step_export.py` | Testes (documentos sintéticos; bateria real é passo posterior). |
| `.venv-beta/` | Venv próprio do beta (não versionado). Só build123d + deps. |
| `.external/` (gitignore) | Oráculo FreeCAD: AppImage oficial 1.0.2 + extração `squashfs-root/` (3,7 GB; não versionado — ver "Oráculo FreeCAD" abaixo). |

## Instalação (venv próprio, nunca global)

```sh
cd <worktree>
python3 -m venv .venv-beta
# /tmp é tmpfs pequeno (512 MB) nesta máquina: pip precisa de TMPDIR
# num disco com espaço, senão falha com "No space left on device".
TMPDIR=$PWD/.tmp .venv-beta/bin/pip install --no-cache-dir build123d
```

Versão pinada (registrada após a instalação; ver `requirements-step-worker.txt`):

```sh
.venv-beta/bin/pip freeze | grep -iE 'build123d|^OCP'
```

**NUNCA** instale CadQuery neste venv, **NUNCA** instale nada global
(sem `--system`, sem `sudo pip`).

## Uso (a partir do host OCRE)

```python
from ocre_core.draw.step_export import document_to_step, oracle_step, validate_step

report = document_to_step(document, thickness_mm=10.0, material="PLA",
                          out_path="peca.step")
# report: output_path, output_sha256, volume_mm3, bbox_mm,
#         file_schema (AP real emitido), worker_versions, ...
check = validate_step("peca.step")        # BRepCheck via OCP (worker)
oracle = oracle_step("peca.step")         # FreeCAD, se disponível
```

Regras fail-closed (o host recusa antes de chamar o worker):

- Espessura e material são DECLARADOS por humano — nunca inventados.
  `thickness_mm=None`/<=0 ou `material` vazio -> `StepExportError`.
- Traço aberto `stable` -> recusa (nunca vira sólido).
- Documento sem traço fechado `stable` -> recusa.
- `erased` nunca entra; `pending` é pulado e registrado no relatório.
- Y do papel (y-down) vira Y CAD (y-up): `y_cad = altura_efetiva - y_papel`.

O host confere o volume do sólido contra área do perfil x espessura
(tolerância relativa 0,1%); divergência maior -> erro, não relatório.

## Spikes do Senado (resultados em `docs/step-spikes-RESULTADO.md`)

- **(a) DXF AC1015 + LibreCAD:** `export_dxf_ac1015(doc, "perfil.dxf")`
  usa `ExportDXF(version="AC1015", unit=Unit.MM)` do build123d; a
  abertura é checada pelo gate F0 (`librecad_gate.check_dxf`).
- **(b) FILE_SCHEMA real:** o relatório traz a linha `FILE_SCHEMA`
  lida do STEP gerado — o AP é o que o OCCT de fato emitiu (a ficha
  dizia "AP242 a confirmar"; o resultado medido está no relatório de
  spikes, não assumido aqui).
- **(c) Determinismo:** `document_to_step(..., timestamp="...")` passa
  `timestamp=` a `export_step` quando o build123d instalado aceita o
  parâmetro; sem ele, o `FILE_NAME` carrega o relógio e o hash cru
  difere — o teste compara então o hash com a linha do timestamp
  mascarada e registra qual caso aconteceu.

## Oráculo FreeCAD

`freecadcmd` NÃO existe via apt nesta máquina (sem candidato). A rota
do beta é o AppImage oficial 1.0.2, **extraído** (a montagem direta
falha sem FUSE nesta VM):

```sh
cd <worktree>/.external
# Download oficial (nome exato; sem "-conda-...-py311" dá 404):
# https://github.com/FreeCAD/FreeCAD/releases/download/1.0.2/FreeCAD_1.0.2-conda-Linux-x86_64-py311.AppImage
# SHA-256 oficial (conferir contra o -SHA256.txt da release):
# e00be00ad9fdb12b05c5002bfd1aa2ea8126f2c1d4e2fb603eb7423b72904f61
sha256sum FreeCAD.AppImage   # tem que bater com o SHA acima
./FreeCAD.AppImage --appimage-extract   # cria squashfs-root/ (~3 GB)
```

O oráculo (`step_export._freecad_command`) procura, nesta ordem:
`freecadcmd` no PATH > `FREECAD_CMD` (env) >
`.external/squashfs-root/usr/bin/freecadcmd` > AppImage em
`FREECAD_APPIMAGE` ou `.external/FreeCAD.AppImage` (só monta onde há
FUSE). Sem FUSE, aponte `FREECAD_CMD` para o binário extraído.

`.external/` (3,7 GB: AppImage de 796 MB + extração) **nunca é
versionado** — está no `.gitignore`; outra máquina repete o download
e a extração acima. O AppImage e a extração vivem só no worktree
local; nada disso entra no commit.

Se o AppImage não estiver presente, `oracle_step` devolve
`{"status": "unavailable", ...}` — skip honesto, nunca passo fingido.
O oráculo lê o JSON escrito pelo script (nunca confia no exit code).
Enquanto o oráculo estiver indisponível, a validação do STEP é o
BRepCheck via OCP dentro do worker (`validate_step`) — checagem
interna do próprio kernel que gerou o arquivo, NÃO validação
independente; o gap fica registrado no relatório e no código.

## Limites conhecidos

- O STEP emitido pelo OCCT NÃO carrega o material: ele é declarado no
  relatório/sidecar, não no arquivo.
- O STEP é só saída: round-trip dá no máximo projeções de arestas, sem
  `stroke_id`/`order`/`status` — o documento `draw.paper.v1` continua
  sendo o mestre; o fingerprint é `DRAW:PAPER-APPROVAL:FINGERPRINT`
  (errata do Senado; nunca o rótulo antigo `DRAW-AF/1`).
- Perfis que se auto-intersectam ou com área zero falham no worker
  (`make_face`/`extrude`) e viram erro de job — fail-closed.
