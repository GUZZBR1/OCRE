# Fixture beta-exemplo — documento de exemplo do `beta-run`

Cenário mínimo ponta a ponta do beta v0.1 OCRE↔Draw: uma folha A4 com
dois retângulos, um vivo e um apagado.

## Arquivos

- `canvas.png` — PNG sintético 850×1200 (canvas A4 do Draw), dois
  retângulos pretos sobre branco. Gerado por `make_canvas.py` (só
  stdlib; rode-o para regenerar). O PNG é só a entrada visual — o
  `fake_vectorize` da fixture nem o lê.
- `fake_vectorize.py` — symlink para o fake da F0
  (`../fake_vectorize.py`). É o MESMO arquivo, reutilizado, não uma
  cópia e não um vetorizador novo. O `beta-run` aponta para ele com
  `--vectorize-script` e pin com o sha256 do arquivo resolvido.
- `document.json` — o documento de exemplo `draw.paper.v1`
  (doc `11111111-2222-4333-8444-555555555555`, revisão 6):
  - `s-0001`: retângulo 80×40 mm, fechado, `stable` — vai para o CAD
    vivo, para o STEP e para o PDF;
  - `s-0002`: retângulo 60×40 mm, fechado, `erased` (tombstone,
    `erased_reason: manual`, `erased_at_ms: 5000`) — prova a borracha
    no passo OCS (`sync_document` prova a ausência dele no CAD).
  - O `s-0001` tem os MESMOS pontos do retângulo que o
    `fake_vectorize` emite: o documento de exemplo é a mesma folha
    que o passo 1 vetoriza, com a borracha aplicada. Validado com o
    validador estrito antes de ser guardado aqui.

## Como o `beta-run` usa a fixture

```
PYTHONPATH=backend .venv-beta/bin/python -m ocre_core.draw.cli beta-run \
  --png backend/tests/draw/fixtures/beta-exemplo/canvas.png \
  --vectorize-script backend/tests/draw/fixtures/beta-exemplo/fake_vectorize.py \
  --vectorize-sha256 <sha256 do arquivo resolvido> \
  --document backend/tests/draw/fixtures/beta-exemplo/document.json \
  --out-dir <dir> --origin upstream-canvas-png \
  --draw-repo santleme/draw --draw-commit aaaa... (40 hex) \
  --thickness-mm 2.0 --material steel \
  --ocs-binary <OpenCADStudio>
```

- Passo 1 (export) roda o `fake_vectorize` sobre o PNG e produz o
  documento F0 (`document.json` + DXF + sidecar + fingerprint) em
  `<out-dir>/export/`. O documento F0 tem 3 traços (o fake emite
  também uma polilinha aberta e uma linha, que o STEP recusa
  fail-closed — por isso o passo 3 usa o documento de exemplo).
- Passos 2+ usam o `--document` (o documento de exemplo) como
  documento de trabalho: é ele que vai para o CAD vivo, para o STEP
  (só o `s-0001` fechado e vivo), para o FEA (como proveniência do
  caso axial) e para o PDF. O relatório registra os dois documentos
  (sha do F0 e sha do exemplo) e a ligação entre eles.
- Sem `--document`, o `beta-run` usa o documento do passo 1 como
  documento de trabalho; nesse caso não há traço apagado e a prova
  da borracha fica como `not-triggered` no relatório.
