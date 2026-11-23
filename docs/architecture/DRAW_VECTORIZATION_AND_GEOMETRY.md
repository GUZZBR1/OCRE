# Melhorias de Vetorização e Geometria: OCRE ↔ `draw-core`

Este documento detalha o diagnóstico técnico e o plano de evolução para a fronteira de **vetorização** e **reconstrução geométrica 2D → 3D** entre o **OCRE** (`backend/ocre_core/draw/`) e o **`santleme/draw-core`** (`src/draw/`).

---

## Contexto e Fronteira Atual

Atualmente, o OCRE consome o `draw-core` exclusivamente como uma ferramenta *black-box* offline via subprocesso em `backend/ocre_core/draw/vectorize_runner.py` (`run_vectorize()`), invocando `src/draw/exporters/vectorize.py` com validação prévia de SHA-256.

Embora o `draw-core` já produza primitivas classificadas, carimbos temporais (`--stamps`), métricas de fidelidade e suporte a múltiplos formatos de papel, parte dessa informação é descartada antes de chegar ao documento mestre (`draw.paper.v1`), ao DXF (`dxf_export.py`) e ao sólido STEP (`step_export.py` / `step_worker.py`).

A evolução recomendada divide-se em duas fases sequenciais:

1. **Fase 1 — Vetorização (`vectorize.py` → `draw.paper.v1`)**: eliminar perdas de primitivas, metadados temporais, métricas e escala na fronteira de entrada.
2. **Fase 2 — Geometria 2D → 3D (`draw.paper.v1` → DXF / STEP)**: evoluir a modelagem CAD para suportar furos/ilhas, curvas analíticas e traços auxiliares sem quebrar o modelo *fail-closed*.

---

## Fase 1 — Vetorização (`vectorize.py` → `draw.paper.v1`)

### 1.1 Suporte completo às primitivas `circle` e `point`

#### Estado atual e gap
No `draw-core`, `src/draw/exporters/vectorize.py` (`classify_primitive()`) classifica traços nos tipos:
- `point`: `{"type": "point", "x_mm": float, "y_mm": float}`
- `line`: `{"type": "line", "x1_mm": float, "y1_mm": float, "x2_mm": float, "y2_mm": float, "length_mm": float, "max_dev_px": float}`
- `circle`: `{"type": "circle", "cx_mm": float, "cy_mm": float, "r_mm": float, "radial_rmse_px": float}` (ajuste Kåsa)
- `rectangle`: `{"type": "rectangle", "center_mm": [x, y], "width_mm": float, "height_mm": float, "angle_deg": float, "corners_mm": [[x, y], ...]}`
- `polygon` / `polyline`: `{"type": "polygon"|"polyline", "vertices": int, "points_mm": [[x, y], ...]}`

No OCRE, `backend/ocre_core/draw/vectorize_runner.py` (`_normalize_primitives_with_skipped_types()`) converte `line` (`x1_mm`/`y1_mm`/`x2_mm`/`y2_mm`) e `rectangle` (`corners_mm`) para polilinhas, mas **descarta `circle` e `point`**, contabilizando-os apenas em `skipped_primitive_types` e `primitives_without_points`. A própria docstring de `normalize_primitives()` registra que a conversão círculo → polilinha fechada pertence a `document.py`, porém essa conversão não existe hoje.

**Impacto:** Qualquer círculo desenhado no papel (furo, pino, eixo, seção cilíndrica) desaparece antes de entrar em `draw.paper.v1`, DXF e STEP.

#### O que implementar
1. **Conversão para `draw.paper.v1`**:
   - Em `backend/ocre_core/draw/document.py` (ou camada de ingestão chamada por `cli.py:_cmd_export()`), converter primitivas `circle` em traços fechados (`closed=True`, `shape="circle"`) com discretização controlada em `points_mm` dentro dos limites do papel.
   - Avaliar a preservação dos parâmetros analíticos (`cx_mm`, `cy_mm`, `r_mm`, `radial_rmse_px`) em metadado opcional do traço/schema para evitar perda de exatidão no CAD (ver item 2.2).
   - Para `point` (`len == 1`), registrar como traço de 1 ponto no documento mestre (já previsto por `dxf_export.py`, que pula traços `< 2` pontos com `reason="single_point"` no relatório, mantendo o documento como mestre).
2. **Critérios de aceitação**:
   - `circle` emitido pelo `vectorize.py` gera traço `closed=True` válido em `draw.paper.v1` com `shape="circle"`, sem violar os limites da folha em `validator_draw.py`.
   - O relatório de exportação não descarta mais círculos silenciosamente em `skipped_primitive_types`.

---

### 1.2 Propagação de carimbos temporais (`--stamps`)

#### Estado atual e gap
- O `draw-core` produz planos temporais `first_seen_ms` e `last_update_ms` (ou `last_seen_ms`) em `.npz` e `src/draw/exporters/vectorize.py` (`load_stamps()`) aceita `--stamps ARQUIVO.npz`, orientando polilinhas abertas do menor para o maior carimbo e anexando `"t_start_ms": int` e `"t_end_ms": int` nas primitivas.
- No OCRE, `backend/ocre_core/draw/document.py` já define `ORDER_KINDS = ("temporal-stamps", "reading-centroid", "unknown")` e `schemas/draw-paper/0.1.0.schema.json` suporta `t_start_ms` / `t_end_ms`.
- Porém, `vectorize_runner.py:_normalize_primitives_with_skipped_types()` copia apenas `points_mm`, `closed` e `type` (descartando `t_start_ms`/`t_end_ms`), e `cli.py:_cmd_export()` não expõe nem repassa `stamps_path` para `run_vectorize()`.

**Impacto:** Perde-se a ordem cronológica real de criação dos traços, prejudicando auditoria, reprodutibilidade e futura classificação contextual na camada de IA (`DRAW_AI_LAYER`).

#### O que implementar
1. Expor `--stamps` nos subcomandos `export` e `beta-run` em `backend/ocre_core/draw/cli.py`, repassando `stamps_path` para `run_vectorize()`.
2. Preservar `t_start_ms` e `t_end_ms` (quando presentes na primitiva) na saída de `_normalize_primitives_with_skipped_types()` em `vectorize_runner.py`.
3. Quando `--stamps` for fornecido e os traços tiverem `t_start_ms`/`t_end_ms`, inicializar o documento com `order_kind="temporal-stamps"` em `new_document()` e preencher os carimbos em `add_stroke()`.

---

### 1.3 Persistência das métricas de fidelidade (`summary`) no sidecar

#### Estado atual e gap
`src/draw/exporters/vectorize.py` calcula e devolve no JSON um objeto `summary` completo contendo:
- `pixels_tinta`, `polilinhas`, `polilinhas_fechadas`, `polilinhas_de_um_ponto`, `saltos_de_caneta`
- `distancias.polilinha_para_tinta` e `distancias.tinta_para_polilinha` (`n`, `media_px`, `mediana_px`, `p95_px`, `max_px`, `media_mm`)
- `primitivas_contagem`, `polaridade_tinta`, `epsilon_px`, `thinning`

`run_vectorize()` devolve esse `summary` no dicionário de retorno, mas `cli.py:_cmd_export()` grava apenas contagens simples (`primitives_total`, `polylines_total`, `primitives_without_points`, `skipped_primitive_types`), descartando as métricas de distância geométrica.

**Impacto:** Sem essas métricas registradas na proveniência, não há visibilidade quantitativa de erro de vetorização (por exemplo, desvio `p95_px` ou `max_px` alto antes de gerar CAD/malha).

#### O que implementar
1. Registrar o resumo geométrico (`distancias`, `saltos_de_caneta`, `polaridade_tinta`, `primitivas_contagem`, `epsilon_px`, `thinning`) no sidecar gerado por `backend/ocre_core/draw/sidecar.py` (`draw-paper-provenance/0.1`).
2. Opcionalmente, expor um *threshold* configurável de alerta/bloqueio (ex.: `p95_mm` acima do limite) no relatório de validação ou `ConversionReport`.

---

### 1.4 Alinhamento de formatos de papel e orientações

#### Estado atual e gap
- O `draw-core` suporta `a4`, `a5`, `a3`, `carta` e `caderno` em `retrato` e `paisagem` (`src/draw/config.py`: `FORMATS`, `ORIENTATIONS`, `paper_grid()`, `paper_mm()`, `apply_format()`) e `src/draw/exporters/export_vectors.py` aceita `paper_w_mm`, `paper_h_mm` e `scale`.
- Em contraste, `src/draw/exporters/vectorize.py` hardcoda A4 retrato:
  - `PAPER_W_PX = 850`, `PAPER_H_PX = 1200`
  - `PAPER_W_MM = 210.0`, `PAPER_H_MM = 297.0`
  - Além disso, `src/draw/config.py` documenta uma diferença de `0.375 mm` na grade A4 legada (`850 * (297 / 1200) = 210.375 mm`).
- No OCRE, `backend/ocre_core/draw/paper.py` já possui `PaperSpec` e `resolve_paper()`, mas a ponta de entrada (`vectorize.py`) ignora formatos diferentes de A4 retrato.

**Impacto:** Qualquer captura em `paisagem` (`1200x850 px`) ou em outro formato (`a3`, `a5`, `carta`, `caderno`) terá coordenadas e escala em `mm` incorretas ao passar pelo `vectorize.py`.

#### O que implementar
1. Parametrizar dimensões/formato no `vectorize.py` (ou aceitar dimensões da folha via CLI / metadados da captura) mantendo `A4` retrato como padrão retrocompatível.
2. Alinhar o registro `PaperSpec` em `backend/ocre_core/draw/paper.py` com os formatos e orientações definidos em `draw-core/src/draw/config.py`.
3. Validar coerência entre a resolução do PNG de entrada e o `PaperSpec` resolvido antes de aceitar as coordenadas em `mm`.

---

## Fase 2 — Geometria 2D → 3D (`draw.paper.v1` → DXF / STEP)

### 2.1 Suporte a furos e ilhas (contorno externo + loops internos)

#### Estado atual e gap
Em `backend/ocre_core/draw/step_worker.py` (`_job_export()`), cada perfil recebido em `profiles` gera um sólido isolado:
```python
wire = Polyline(*points, close=True)
face = make_face(wire)
solid = extrude(face, amount=float(thickness))
```
Quando há múltiplos perfis, todos os sólidos são apenas agrupados em `Compound(children=solids)`.

**Impacto:** Se o usuário desenhar uma chapa retangular com um furo circular interno (mesmo após resolver o item 1.1), o resultado no STEP será **dois sólidos sobrepostos** (a chapa cheia + um cilindro dentro dela), em vez de uma única chapa furada. O cálculo de volume esperado em `step_export.py` (`sum(_polygon_area_mm2(p)) * thickness_mm`) também soma as áreas em vez de subtrair os furos.

#### O que implementar
1. **Detecção de hierarquia de contenção 2D** no host (`step_export.py`) ou no worker (`step_worker.py`):
   - Verificar aninhamento ponto-em-polígono / contenção entre perfis fechados (regra par-ímpar: profundidade par = contorno externo; profundidade ímpar = furo/loop interno).
   - Recusar (*fail-closed*) contornos que se auto-intersectam ou se cruzam parcialmente entre si.
2. **Construção booleana / face com furos no `build123d`**:
   - Construir a face externa com seus loops internos (`make_face` com *outer wire* + *inner wires*, ou subtração booleana após extrusão) antes de exportar para STEP.
3. **Atualizar a verificação de volume** em `step_export.py`:
   - Calcular a área líquida esperada (`área_externa - soma(áreas_dos_furos)`) para validar `VOLUME_TOLERANCE_REL = 1e-3` contra o sólido OCCT retornado.

---

### 2.2 Preservação de geometria analítica para círculos no STEP (e DXF)

#### Estado atual e gap
- `dxf_export.py` explicita *"NUNCA CIRCLE: um círculo vetorizado continua sendo uma polilinha fechada"*.
- `step_worker.py` recebe apenas listas de `[x, y]` e constrói `Polyline(*points, close=True)`.

**Impacto:** Um círculo discretizado em `N` segmentos vira um prisma facetado de `N` faces planas no STEP. Isso degrada a qualidade da malha tetraédrica no Gmsh, dificulta a aplicação de condições de contorno cilíndricas/furos e aumenta artificialmente o número de arestas B-Rep.

#### O que implementar
1. Quando um traço tiver `shape == "circle"` e parâmetros analíticos válidos (`cx_mm`, `cy_mm`, `r_mm`), enviar o descritor analítico no payload do job para `step_worker.py` (mantendo `points_mm` como representação compatível em `draw.paper.v1`).
2. No `step_worker.py`, construir o *wire* com `Circle(radius)` posicionado em `(cx_mm, height_mm - cy_mm)` no plano CAD, gerando face cilíndrica exata no OCCT/STEP.
3. No `dxf_export.py`, manter o padrão atual por compatibilidade ou tornar a emissão de `CIRCLE` opt-in explícito se o consumidor CAD exigir.

---

### 2.3 Política para traços abertos no pipeline STEP

#### Estado atual e gap
Em `backend/ocre_core/draw/step_export.py` (`_closed_stable_profiles()`):
```python
if not stroke.get("closed", False):
    raise StepExportError(
        f"traço {stroke_id}: traço aberto não vira sólido "
        "(só traços fechados 'stable' vão para o STEP)"
    )
```
Qualquer traço `stable` aberto no documento — como uma linha de centro, cota, anotação ou marcação auxiliar — **aborta a exportação STEP inteira**.

#### O que implementar
1. Manter o comportamento *fail-closed* por padrão quando **não houver nenhum** traço fechado `stable` ou quando um contorno estrutural falhar validação geométrica.
2. Introduzir uma política explícita (ex.: `open_strokes_policy="fail" | "skip-aux"`, ou classificação semântica via `DRAW_AI_LAYER` / papel do traço) que permita ignorar traços abertos classificados como auxiliares/anotação, registrando-os em `skipped` no relatório do STEP sem perder rastreabilidade.

---

## Resumo de Arquivos Impactados

| Fase | Item | Arquivos no OCRE | Arquivos no `draw-core` |
| --- | --- | --- | --- |
| 1.1 | `circle` e `point` | `backend/ocre_core/draw/vectorize_runner.py`, `document.py`, `cli.py` | `src/draw/exporters/vectorize.py` (consumo) |
| 1.2 | `--stamps` | `backend/ocre_core/draw/vectorize_runner.py`, `cli.py`, `document.py` | `src/draw/exporters/vectorize.py` (consumo) |
| 1.3 | Métricas `summary` | `backend/ocre_core/draw/cli.py`, `sidecar.py` | `src/draw/exporters/vectorize.py` (consumo) |
| 1.4 | Formatos e orientações | `backend/ocre_core/draw/paper.py`, `vectorize_runner.py` | `src/draw/config.py`, `src/draw/exporters/vectorize.py` |
| 2.1 | Furos e ilhas no STEP | `backend/ocre_core/draw/step_export.py`, `step_worker.py` | — |
| 2.2 | Círculo analítico no STEP | `backend/ocre_core/draw/step_export.py`, `step_worker.py`, `dxf_export.py` | — |
| 2.3 | Política de traços abertos | `backend/ocre_core/draw/step_export.py`, `cli.py` | — |

---

## Verificação Recomendada ao Implementar

Para cada item implementado, validar na suíte existente e com novos casos de regressão em `backend/tests/draw/`:
- `test_vectorize_runner.py`, `test_document.py`, `test_cli.py`, `test_sidecar_draw.py` (Fase 1)
- `test_step_export.py`, `test_dxf_export.py`, `test_paper.py`, `test_beta_cli.py` (Fase 2)
