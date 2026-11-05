# Spikes STEP — resultados medidos (beta v0.1, 2026-11-05)

Trabalho: `backend/ocre_core/draw/step_export.py` + `step_worker.py`
(worker build123d 0.13.0 / OCP 8.0.1.1 / OCCT 8.0.1.1, venv
`.venv-beta`), testes em `backend/tests/draw/test_step_export.py`.
Documento de teste: retângulo fechado `stable` 100×10 mm, espessura
declarada 10 mm, material declarado `PLA` — volume esperado 10000 mm³.

## (a) ExportDXF AC1015 + abertura no LibreCAD

- `ExportDXF(version="AC1015", unit=Unit.MM)` do build123d funciona:
  DXF gerado com `$ACADVER AC1015`, `$INSUNITS` em mm, 4 entidades
  `LINE` fechando o retângulo exato (coordenadas CAD y-up conferidas
  por releitura com ezdxf 1.4.5: `dxfversion == "AC1015"`).
- **Abertura no LibreCAD NÃO verificada nesta máquina:** o
  `librecad dxf2svg` (LibreCAD 2.2.0.2, `/usr/bin/librecad`) TRAVA
  (timeout, CPU ~0 — espera ociosa) para QUALQUER DXF, reproduzido
  com um DXF trivial do próprio ezdxf F0, com `QT_QPA_PLATFORM=offscreen`,
  stdin fechado, `XDG_RUNTIME_DIR` definido e sob `xvfb-run`.
  Não é problema do DXF do build123d — é o binário/ambiente. O teste
  verifica a estrutura via ezdxf e tenta o gate real com timeout de
  20 s, virando skip honesto quando trava. **Gap aberto:** confirmar
  a abertura num ambiente onde o LibreCAD converta (ou com outro
  leitor DXF), antes de afirmar o gate G0.2 para este DXF.

## (b) FILE_SCHEMA real do STEP gerado

- Linha lida do STEP: `FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));`
- **AP214, NÃO AP242.** A ficha `cad.md` dizia "AP242 a confirmar" —
  está confirmado o contrário: o build123d 0.13.0 / OCCT 8.0.1.1 emite
  AP214 por padrão. O relatório (`document_to_step`) traz o
  `file_schema` real lido do arquivo, sem assumir. **Gap aberto:**
  AP242 exigiria `write.step.schema` no OCCT / outro exportador —
  não tentado neste beta; se o plano pinar AP242, é trabalho futuro
  (e o teste de `file_schema` terá de mudar junto).

## (c) Determinismo de `export_step(..., timestamp=...)`

- O build123d 0.13.0 **aceita** `timestamp=` em `export_step` e o
  worker o usa quando o job traz `timestamp`.
- Dois exports do mesmo documento com `timestamp="2026-11-05T00:00:00"`:
  **mesmo SHA-256** (`7dc6af81…e224e`, 15501 bytes) — determinístico
  até no hash cru. O `FILE_NAME` carrega o timestamp declarado
  (`'2026-11-05T00:00:00'`), não o relógio.
- Sem `timestamp`, o OCCT escreve o relógio no `FILE_NAME` e o hash
  cru difere; o teste cobre esse caso comparando o hash com a linha
  do timestamp mascarada.

## Oráculo FreeCAD (fora dos 3 spikes, mas pedido no task)

- `freecadcmd` NÃO existe via apt (sem candidato) — confirmado.
- **Obtido:** AppImage oficial 1.0.2
  (`FreeCAD_1.0.2-conda-Linux-x86_64-py311.AppImage`, 795.892.216 bytes,
  SHA-256 `e00be00ad9fdb12b05c5002bfd1aa2ea8126f2c1d4e2fb603eb7423b72904f61`
  — conferido contra o `-SHA256.txt` oficial) baixado de
  `github.com/FreeCAD/FreeCAD/releases/download/1.0.2/`.
  Primeira tentativa falhou com URL errada (`FreeCAD_1.0.2-Linux-x86_64.AppImage`
  -> 404 "Not Found", 9 bytes); o nome correto tem `-conda-...-py311`.
- A montagem direta falha sem FUSE nesta VM (`No suitable fusermount
  binary found`); **extraído** com `--appimage-extract` para
  `.external/squashfs-root/` (3,0 GB) e o oráculo usa
  `.external/squashfs-root/usr/bin/freecadcmd`.
- **Oráculo confirma o STEP:** FreeCAD 1.0.2, `Part.Shape().read()`,
  `is_valid: true`, `is_closed: true`, `volume_mm3: 9999.999999999998`,
  `solids: 1`, bbox `[10, 266.47, 0] .. [110, 276.47, 10]` — exato.
  O oráculo roda via subprocesso e lê JSON (nunca confia no exit code).
- Gap residual: o AppImage extraído (3 GB) e o `.AppImage` (796 MB)
  vivem em `.external/` (não versionados); sem FUSE, outra máquina
  precisa repetir a extração. O código trata a ausência como
  `status == "unavailable"` (skip honesto).

## Checagem independente (BRepCheck, worker)

- `validate_step` (OCP `BRepCheck_Analyzer` no worker): `brepcheck_valid:
  true`, 1 sólido, volume 9999.999999999998 mm³. É a checagem do próprio
  kernel que gerou o arquivo — NÃO substitui o oráculo FreeCAD acima.

## Reconfirmação — finalização do beta (2026-11-05, worker de finalização)

Rodado de novo, do zero, no mesmo worktree/venv (não só lido o doc):

- **(c) Determinismo:** dois exports com `timestamp="2026-11-05T00:00:00"`
  deram o **mesmo SHA-256** (`7c69f345…66aa2`, 15510 bytes) —
  determinístico até no hash cru. Ressalva honesta: esse hash difere
  do registrado acima pelo worker anterior (`7dc6af81…e224e`, 15501
  bytes) — a causa não foi estabelecida (provável diferença no
  documento/timestamp exatos daquela medição, não no worker); a
  propriedade que importa — mesmas entradas, mesma saída — valeu nas
  duas medições.
- **(b) FILE_SCHEMA:** `FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));`
  — **AP214 confirmado de novo**, também no relatório.
- **(a) DXF AC1015:** ezdxf releu `dxfversion == "AC1015"`, 4 `LINE`s
  fechando o retângulo exato em coords CAD (y-up 266.47–276.47).
  **LibreCAD:** nesta sessão o binário **nem existe**
  (`which librecad` vazio, `librecad_available() -> None`) — o skip
  do teste foi por `unavailable`, não por timeout/trava como na
  medição anterior. A abertura no LibreCAD continua NÃO verificada;
  o motivo exato nesta sessão é ausência do binário, não o travamento.
- **Oráculo FreeCAD (reconfirmado de verdade):** `oracle_step` sobre o
  STEP gerado, via `.external/squashfs-root/usr/bin/freecadcmd`:
  `status: pass`, `is_valid: true`, `is_closed: true`,
  `volume_mm3: 9999.999999999998`, `solids: 1`, bbox exata
  `[10, 266.47, 0] .. [110, 276.47, 10]`. O teste do oráculo PASSOU
  na suíte (não foi skip). BRepCheck do worker: `valid: true`, mesmo
  volume.
- **Suíte completa:** `PYTHONPATH=backend .venv-beta/bin/python -m pytest
  backend/tests tests/eir -q` → **327 passed, 1 skipped, 100 subtests
  passed** (base 317 + 10 do `test_step_export.py`; o skip é o teste
  do DXF/LibreCAD, por `unavailable`).
- **`.external/` (3,7 GB):** mantido no worktree (o código o referencia
  em `step_export._freecad_command`), mas incluído no `.gitignore` —
  não entra no commit. Como obter está em `docs/step-worker.md`.

- Nada foi provado contra conjunto selado/real: só documento sintético.
- O STEP não carrega o material (limite do OCCT): material declarado
  vive no relatório/sidecar, não no arquivo.
- STEP é só saída: sem `stroke_id`/`order`/`status` no round-trip.
- Perfis auto-intersectantes falham no worker (fail-closed), não testados
  com geometria real.
