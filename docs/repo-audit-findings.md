# Auditoria do repositório OCRE — findings (Fase 3)

Data: 2026-11-23 · Auditado contra `origin/main` = `05a3df1c` · Executor: Devin
Escopo: `backend/ocre_core`, `backend/tests`, `tests`, `experiments`, `client/src`, `schemas`, `docs`. Ignorados: `.venv*`, `node_modules`, `.tmp`, `.worktrees` (contém repo estrangeiro `santleme/draw-core` e arquivos arquivados), `__pycache__`, `dist`.
Método: plano `docs/repo-audit-plan.md` fases 0–3. Brutos em `.tmp/audit/`.

## Baseline de testes

- `backend/.venv/bin/python -m pytest -q` → **aborta na coleta**: `test_viz.py` importa `meshio` (dep só em `requirements-viz.txt`). Com `--continue-on-collection-errors`: **635 passed, 11 skipped, 1 error, 221 subtests** (~188 s). Detalhe: `.tmp/audit/baseline-pytest.txt`.
- `cd client && npm run check` → **vermelho** em `tsc --noEmit`: `src/mesh-data.ts(26,36) error TS7006`.
- Cobertura `backend/ocre_core`: **79%** (coverage 7.17.4; `.tmp/audit/coverage.txt`).

## Exclusões da Fase 0 (`.tmp/audit/exclusions.md`)

- **PRs abertos:** nenhum (`gh pr list --state open` = `[]`).
- **Branches abertas (unmerged):** `docs/vectorization-status` e `feat/cloud-compute-architecture` — ambas **docs-only**; nenhum arquivo de código em branch aberta. Único conflito possível: findings que tocam `docs/architecture/DRAW_VECTORIZATION_AND_GEOMETRY.md` (F-020).
- **Mergeadas de fato:** `chore/limpeza-5` (conteúdo já em main via `limpeza-5-residual`, `git cherry` = `-` para os 2 commits), `chore/bugs-draw`, `feat/beta-paper`, `feat/draw-step-geometry`, `feat/dxf-circle-optin`, `feat/physics-impulse-envelope`, `feat/vectorize-module`, `feat/vectorize-python`, `fix/beta-pontas-soltas`.
- **Fora do escopo:** `.worktrees/draw-core` (repo estrangeiro), `.worktrees/arquivo-prs-1-2` e `.worktrees/runs` (artefatos), `.worktrees/venv-beta`.

## Sumário

44 findings (F-001–F-044). Por categoria e severidade:

| categoria | findings | por severidade |
|---|---|---|
| bug | 8 | alta ×2 (F-002, F-003), média ×1 (F-013), baixa ×5 |
| morto | 12 | baixa ×12 |
| contrato | 7 | média ×1 (F-001), baixa ×6 |
| simplificação | 17 | risco baixo ×6, médio ×10, alto ×1 (F-036) |

Por confiança: **evidência** ×37 · **inferência** ×6 (F-038–F-043) · **mista** ×1 (F-034, F-035 parcial).

## Contratos congelados: divergências reportadas (não alterar)

### F-001 — Validador não confere `pending[].json_pointer` do relatório de migração
- categoria: contrato (congelado EIR v0.2)
- severidade: média
- confiança: evidência
- local: `docs/architecture/EIR_V02_CONTRACT.md:136` × `backend/ocre_core/validation/validator.py:456-458`
- evidência: doc exige "a validação semântica confirma que … cada `pending[].json_pointer` corresponde a campo ainda não resolvido"; o validador só confere `report_artifact_id not in artifacts → migration_report_unresolved` e nunca lê o relatório. `grep -n "json_pointer\|pending" backend/ocre_core/validation/validator.py` → 0 hits de verificação de pendências. Mitigação: `EIR_VALIDATION_ARCHITECTURE.md:152` lista "verificação de pendências ponta a ponta" como decisão aberta.
- ação sugerida: reportar ao dono — implementar a checagem de `pending[]` no validador ou emitir errata do contrato. **Não tocar schema/digest.**
- esforço: —
- risco de remoção/mudança: — (report-only)
- teste: draft migrado com `null` não listado em `pending[]` deveria falhar hoje e não falha.
- conflito: nenhum

Outros itens congelados verificados OK: digests `schema-lock-0.2.0.json` conferidos em runtime (`validator.py:111-117`, `validator_draw.py:178-198`) e em teste (`tests/eir/test_schemas.py:101-106`); framing OCRE-AF/1 (`approval_fingerprint.py:18,:222`) conforme contrato. `schemas/eir/mesh-group-manifest-0.1.0.schema.json` não tem consumidor de código (só doc + digest pinado) — congelado, apenas registrado.

---

## Grupo 1 — Bugs, severidade alta

### F-002 — `pytest -q` aborta na coleta: `test_viz.py` importa `meshio` ausente
- categoria: bug
- severidade: alta (o comando de teste documentado no README falha inteiro)
- confiança: evidência
- local: `backend/tests/draw/test_viz.py:24` (`import meshio`); dep declarada só em `backend/requirements-viz.txt:13`
- evidência: `backend/.venv/bin/python -m pytest -q` → `ImportError … No module named 'meshio'` → `1 error in 1.40s`, coleção interrompida (`.tmp/audit/baseline-pytest.txt`).
- ação sugerida: `meshio = pytest.importorskip("meshio")` no topo do módulo — condizente com a convenção do próprio teste ("binário ausente é skip honesto", docstring :9) e com os demais gates opcionais do repo.
- esforço: S
- risco de remoção/mudança: baixo (quando a dep existir, o módulo roda igual)
- teste: `pytest --collect-only -q` passa sem erro; `test_viz.py` aparece como `s` (skip) neste venv.
- conflito: nenhum

### F-003 — `npm run check` vermelho: implicit any em `mesh-data.ts`
- categoria: bug
- severidade: alta (gate do client quebrado em main)
- confiança: evidência
- local: `client/src/mesh-data.ts:26`
- evidência: `cd client && npm run check` → `src/mesh-data.ts(26,36): error TS7006: Parameter 'nodeId' implicitly has an 'any' type.` Causa: `Array.isArray(data.elements)` (linha 13) estreita `readonly ElementRecord[]` para `any[]` → `element.nodeIds.some((nodeId) => …)` sem tipo inferível.
- ação sugerida: anotar `const elements: readonly ElementRecord[] = Array.isArray(...) ? data.elements : []` (idem `nodes`); o callback passa a inferir `string`.
- esforço: S
- risco de remoção/mudança: baixo (só tipagem; comportamento idêntico)
- teste: `npm run typecheck` verde; `npm run check` completo verde.
- conflito: nenhum

## Grupo 2 — Código morto com zero referências (evidência)

> Confirmação aplicada a todos: vulture/lista + `grep -rnw` zero refs em `backend tests experiments docs client schemas` + verificação de uso dinâmico (`getattr`/`importlib`/`find_spec`/decorator registry/`python -m`). Tabela completa: `.tmp/audit/2b-dead-candidates.md`.

### F-004 — `AdapterError` sem nenhum uso
- categoria: morto · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/integrations/contracts.py:104-108`
- evidência: `grep -rnw AdapterError backend tests experiments docs client schemas` → só a definição. Sem `__all__`, sem subclasse, sem `getattr`.
- ação sugerida: remover a dataclass (ou agregar a F-011 se o módulo inteiro for aprovado para remoção).
- esforço: S · risco de remoção/mudança: baixo
- teste: `pytest -q` mantém 635 passando; grep zero refs colado no PR.
- conflito: nenhum (relaciona F-011: mesmo módulo)

### F-005 — `Proposal.is_confirmed` / `is_rejected` sem caller
- categoria: morto · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/ai/proposals.py:63-68`
- evidência: `grep -rnw is_confirmed`/`is_rejected` → só os `def` (e `is_pending` usado em `test_ai_scaffold.py:251` — manter só ele). `confirm`/`reject`/`apply_proposal` leem `.state` direto.
- ação sugerida: remover os dois métodos.
- esforço: S · risco: baixo
- teste: suíte inalterada.
- conflito: nenhum

### F-006 — `get_model` sem call site
- categoria: morto · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/ai/registry.py:71-73`
- evidência: `grep -rnw get_model` → def + re-export `__init__.py:53,83` + docstring. `require_model` é o usado (`cli.py` via registry).
- ação sugerida: remover função e a linha de re-export.
- esforço: S · risco: baixo
- teste: suíte inalterada.
- conflito: nenhum

### F-007 — Protocol `MaterialAdvisor` + método `advise` sem uso (nome diverge do contrato)
- categoria: morto · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/ai/interfaces.py:93-102`
- evidência: `grep -rnw MaterialAdvisor`/`advise\b` → só `interfaces.py`, `__init__.py:35,70`, docs. O runtime consulta `advise_material` via `getattr` (`semantics.py:97`) — o Protocol declara `advise`, que nenhum adapter implementa nem ninguém chama.
- ação sugerida: remover o Protocol (ou renomear `advise`→`advise_material` se o dono preferir manter o contrato tipado alinhado ao runtime).
- esforço: S · risco: médio-baixo (documentado em `docs/architecture/ai/material.md` e `DRAW_AI_LAYER.md:194` — decidir remoção × correção de nome)
- teste: suíte inalterada.
- conflito: nenhum

### F-008 — `IdempotencyRecord.result_reference` nunca lido nem persistido
- categoria: morto · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/persistence/jobs.py:46`; escritas posicionais em `application/services.py:234,306`
- evidência: `grep -rn "result_reference" backend tests` → só campo+construtores. `INSERT INTO receipts` (`sqlite.py:123,156`) grava só tenant/operation/key/fingerprint/receipt; `codec.py:18,24` registra `IdempotencyRecord` em `_TYPES` mas nada o serializa — remover o campo é seguro (codec serializa por `fields()`), anotar no PR que blobs hipotéticos com o campo rejeitariam no decode.
- ação sugerida: remover o campo e ajustar os 2 construtores posicionais.
- esforço: S · risco: baixo
- teste: `test_persistence`/`test_app` continuam verdes.
- conflito: nenhum

### F-009 — `MigrationResult.draft_sha256` nunca lido
- categoria: morto · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/migration/v01_to_v02.py:39`
- evidência: `grep -rn "draft_sha256" backend tests` → só a declaração e construção; consumidores leem `report["output"]["sha256"]` (`:319,:325`).
- ação sugerida: remover o campo do dataclass.
- esforço: S · risco: baixo
- teste: `backend/tests/migration/` verde.
- conflito: nenhum

### F-010 — Clusters test-only em `draw/ai` (router.py, config.py, apply_proposal/ApplicationResult)
- categoria: morto · severidade: baixa · confiança: evidência (zero refs de produção; scaffolding documentado → aprovação por item)
- local: `backend/ocre_core/draw/ai/router.py:34-82`; `draw/ai/config.py:22-54`; `draw/ai/proposals.py:72-81,135-169` (+campo `applied_at`)
- evidência: `grep -rnw route\|AIConfig\|apply_proposal` em `backend/ocre_core` → zero chamadores fora dos próprios módulos/`__init__`; callers = só `test_ai_scaffold.py` (router :198-236, config, apply :277-348). `applied_at` nunca lido. `cli.py` usa `registry` direto.
- ação sugerida: decidir por item — remover módulos/testes ou manter como scaffolding planejado (`docs/architecture/ai/roteador.md`, `DRAW_AI_LAYER.md:196-198` os descrevem). Se remover: ajustar `__init__.py` e apagar os testes correspondentes.
- esforço: M · risco: médio (documentado como degrau de gate G3.2/D6 e plano F3)
- teste: suíte sem os testes removidos continua verde.
- conflito: nenhum

### F-011 — `integrations/contracts.py` inteiro sem consumidor de código
- categoria: morto · severidade: baixa · confiança: evidência (zero consumidores; módulo espelha contrato documentado)
- local: `backend/ocre_core/integrations/contracts.py:1-140`
- evidência: único consumidor de código = `integrations/__init__.py:3` (re-export). `grep -rnw` de cada símbolo (`Adapter`, `ExecutionPort`, `ExecutionEvent`, `ResultManifest`, …) → só docs (`ADAPTER_CONTRACT.md`, `WORKER_ADAPTER_RUNTIME_CONTRACT.md`). `AdapterExecutionState` (:14-22) é homônimo morto de `jobs.py:25`.
- ação sugerida: decisão do dono — remover o módulo (o contrato vive nos docs) ou manter como scaffolding pendente (`WORKER_ADAPTER_RUNTIME_CONTRACT.md:50` registra implementação pendente). Se manter, deduplicar o enum (F-035).
- esforço: M · risco: médio-alto (superfície de contrato; trabalho planejado pode depender dele)
- teste: suíte inalterada (nada importa o módulo).
- conflito: nenhum (relaciona F-004, F-035)

### F-012 — Fixtures `.inp` duplicados byte-idênticos em `experiments/fea-reference/runs/` (topo)
- categoria: morto (dados) · severidade: baixa · confiança: evidência · **PRESERVAR até aprovação explícita**
- local: `experiments/fea-reference/runs/axial_bar_h1.25mm.inp`, `h2.5mm.inp`, `h5mm.inp`
- evidência: `cmp -s runs/axial_bar_h{1.25,2.5,5}mm.inp runs/baseline-c3d8/<homônimo>` → **byte-idêntico nos 3** (verificado nesta auditoria); zero refs diretas (`.tmp/audit/fixtures-refs.md`); ausentes de `runs/manifest.json`; resíduo do commit `5c3bf664`.
- ação sugerida: remover **somente com aprovação explícita** (regra global: fixtures/dados nunca deletados sem OK).
- esforço: S · risco: baixo (duplicatas exatas)
- teste: suíte inalterada.
- conflito: nenhum

## Grupo 3 — Bugs médios/baixos

### F-013 — Mensagem já `processed` nunca vira `delivered`: outbox pendente para sempre
- categoria: bug
- severidade: média
- confiança: evidência (caminho de código lido e fiação confirmada; o gatilho crash→janela é inferência)
- local: `backend/ocre_core/persistence/sqlite.py:217-228` (`consume`), `:193-199` (`dispatch`), `:186-187` (`mark_delivered`); fiação em `integrations/synthetic_runtime.py:141,145` (`publish = consumer.consume`)
- evidência: `consume` devolve `False` no `IntegrityError` (:220) para `'processing'` **e** `'processed'`; `dispatch` só marca `delivered` quando `publish(...) is not False` (:196). Crash entre `consume→True` e `mark_delivered` ⇒ linha `consumed='processed'` + `outbox.delivered=0`: todo `dispatch()` posterior re-tenta, recebe `False`, e a mensagem nunca drena. **Reforço da verificação:** `test_synthetic_runtime.py:195-208` (`test_duplicate_callback_and_publish_before_ack…`) já executa o cenário exato — `deliver` marca `'processed'` sem `delivered` e o `drain` posterior não drena — mas nenhum assert verifica `pending_outbox()`, então o bug existe silenciosamente dentro do teste. `test_sqlite_jobs.py:116-128` cobre crash-no-ack com publish sempre-True — não cobre publish=consumer dedupe.
- ação sugerida: `consume` distinguir já-`processed` (retornar `True`/sentinela "already") de `'processing'` em disputa (`False`), ou `dispatch` marcar `delivered` quando o consumer atesta `processed`.
- esforço: M
- risco de remoção/mudança: baixo-médio (semântica do fake; testes de replay dependem)
- teste (repro): `consumer.consume(msg)` → `True`; `dispatcher.dispatch()` com a mesma `message_id` pendente → `pending_outbox()` deveria drenar e não drena (falha em main, passa no PR).
- conflito: nenhum

### F-014 — `with sqlite3.connect()` não fecha a conexão (falsa sensação de cleanup)
- categoria: bug
- severidade: baixa
- confiança: evidência (comportamento verificado por execução); impacto é inferência leve
- local: `backend/ocre_core/persistence/sqlite.py` (~14 blocos, ex.: `:37,:82,:147,:163,:206,:214-227`); `backend/ocre_core/persistence/sync.py:17,25,29,68`
- evidência: `__exit__` de `sqlite3.connect` faz commit/rollback, **não** close — verificado executando: após o `with`, `db.execute("SELECT 1")` continua funcionando. O próprio código usa o padrão correto em `persistence/sync.py:40,65` (`db=connect()` + `finally: db.close()`). Em CPython a conexão morre por refcount no fim do frame, mas exceções retêm frames via traceback → FD latente.
- ação sugerida: uniformizar para o padrão `connect()+try/finally: close()` já usado no módulo (ou um helper `_connect()` context manager real).
- esforço: M (muitos call sites)
- risco: baixo (mesma semântica transacional; o `with` continua fazendo commit/rollback via `__exit__` — manter `with` dentro do try/finally)
- teste: suíte de persistência verde; opcionalmente teste contando conexões abertas após erro.
- conflito: nenhum

## Grupo 4 — Contrato × implementação, não congelado (ação: reportar ao dono)

> Detalhe completo: `.tmp/audit/2a-contract-candidates.md` (118 afirmações verificadas em 19 docs: 95 ok, 8 divergências, 15 ambiguidades). O contrato congelado está em F-001. Nenhum finding deste grupo gera PR — vai como nota ao dono.

### F-015 — API local diverge do draft `CLOUD_API_CONTRACT.md` em 6 pontos de envelope/campos
- categoria: contrato · severidade: baixa · confiança: evidência
- local / evidência (doc × código):
  1. erro sem `type`/`request_id`/`detail`/`retryable` — doc L46 × `app.py:57-61` (emite só `{code,title,status,extras}`)
  2. commit sync sem `head_revision_id`/`revision_sequence` — doc L131-132 × `services.py:193-199`
  3. `sync_conflict` omite `expected_base_revision_id` (o domínio o carrega, `sync.py:16`) — doc L135-136 × `services.py:188`
  4. submit sem `status_url`/`events_url` — doc L207-208 × `services.py:246-253`
  5. `job_status` sem `adapter_execution` (snapshot existe, `sqlite.py:166-168`) — doc L213-214 × `services.py:270-278`
  6. eventos sem envelope público (`aggregate_*`,`event_type`), cursor inteiro não opaco, sem `head_cursor` — doc L263-303 × `services.py:322-329`
- ação sugerida: reportar ao dono — o doc é draft explícito (L3-7); decidir se o slice local cobre os campos ou se o draft marca-os como propostas.
- esforço: — · risco: — (report-only)
- teste: —
- conflito: nenhum

### F-016 — `ValidationReport`: vocabulário divergente (`execution_readiness`, rollup por etapa, catálogo de códigos)
- categoria: contrato · severidade: baixa · confiança: evidência
- local: `EIR_VALIDATION_ARCHITECTURE.md:115-119` × `backend/ocre_core/validation/validator.py:162-172,:189-199,:622-630`
- evidência: doc sugere `not_requested` para autoria — código emite `null` (:194,:628-630); doc pede rollup por etapa (`passed`/`warning`/`failed`/`not_assessed`) — relatório não carrega agregação; catálogo doc em `SCREAMING_SNAKE` vs `snake_case` implementado.
- ação sugerida: reportar ao dono — alinhar vocabulário normativo.
- esforço: — · risco: — · teste: — · conflito: nenhum

### F-017 — Ambiguidades editoriais do contrato de adaptador (`prepare` assinatura, `progress` no evento, `CapabilityManifest`)
- categoria: contrato · severidade: baixa · confiança: evidência
- local / evidência:
  1. `ADAPTER_CONTRACT.md:12` diz `prepare(request, immutable_input_manifest)`; código `contracts.py:130`, `WORKER_ADAPTER_RUNTIME_CONTRACT.md:15` e `INTEGRATION_ARCHITECTURE.md:94` usam `immutable_revision` — o ADAPTER_CONTRACT é o divergente.
  2. `ADAPTER_CONTRACT.md:88` lista "progresso opcional" no evento; `ExecutionEvent` (`contracts.py:55-61`) não tem `progress` (existe só em `ExecutionStatus:68`).
  3. `ADAPTER_CONTRACT.md:32-49` exige `platforms`/`units_policy`/`software.proposed_target_version`; `models.py:46-62` usa `resource_requirements`/`software.version` (formato do WORKER doc).
- ação sugerida: reportar ao dono — reconciliação editorial (o próprio `JOB_PERSISTENCE_CONTRACT.md:9` registra pendência entre docs).
- esforço: — · risco: — · teste: — · conflito: nenhum

### F-018 — Envelope sync: `schema_version`/`actor_id` ignorados, `payload` inline, escopo `sync:read` fora do draft
- categoria: contrato · severidade: baixa · confiança: evidência
- local: `CLOUD_API_CONTRACT.md:81-82,:113-129` × `backend/ocre_core/api/app.py:21-31` (`extra="ignore"`) × `application/services.py:166,:202`
- evidência: `schema_version` e `actor_id` divergente do corpo são ignorados (não rejeitados); `payload` inline base64 vs `payload_ref` do draft; `sync:read` usado sem constar da lista draft.
- ação sugerida: reportar ao dono.
- esforço: — · risco: — · teste: — · conflito: nenhum

### F-019 — Estado inicial da proposta: `proposed` (doc) vs `pending` (código)
- categoria: contrato · severidade: baixa · confiança: evidência
- local: `DRAW_AI_LAYER.md:90-91,:179-180` × `backend/ocre_core/draw/ai/proposals.py:38,:55,:92`
- evidência: doc usa `proposed`; código usa `pending`; o próprio adendo do doc (:207-208) fala "propostas pendentes" — doc internamente inconsistente, código segue o adendo.
- ação sugerida: reportar ao dono (editorial).
- esforço: — · risco: — · teste: — · conflito: nenhum

### F-020 — `DRAW_VECTORIZATION_AND_GEOMETRY.md`: 7 afirmações "estado atual" desatualizadas
- categoria: contrato · severidade: baixa · confiança: evidência
- local: doc L32,L52,L71,L105-113,L129,L144-152 × `vectorize_runner.py:116,:162-175,:231-263`, `cli.py:152-158,:297,:317-321,:396,:465-473`, `step_export.py:87,:479-491,:715-760`, `dxf_export.py:14-20`
- evidência: o doc diz que `circle`/`point` são descartados, stamps não propagados, métricas de fidelidade ausentes, furos somados (não subtraídos), "NUNCA CIRCLE" no DXF e traço aberto sempre fatal — tudo já implementado (círculo/ponto convertidos; `_primitive_stamps`; `_fidelity_summary` + gate `--max-p95-px`; área líquida par-ímpar; CIRCLE opt-in `--dxf-circles`; `--open-strokes-policy`).
- ação sugerida: reportar ao dono — atualizar o doc (a branch `docs/vectorization-status` já marca o status; avaliar cobertura completa das 7 afirmações).
- esforço: — · risco: — · teste: —
- conflito: **`docs/vectorization-status`** (branch aberta toca este arquivo; o finding pode estar parcialmente coberto por ela)

## Grupo 5 — Simplificações

### F-021 — `cli.py:_beta_step_step`: `temp_context.cleanup()` repetido em 7 early-returns + sucesso → `try/finally`
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/cli.py:1186-1271`
- evidência: `grep -n "temp_context.cleanup" cli.py` → **8 chamadas** do mesmo invariante: 7 em early-returns (`:1197,:1207,:1221,:1229,:1239,:1249,:1257`) + 1 no caminho de sucesso (`:1268`); `try/finally` elimina as cópias e blinda novos caminhos de erro.
- ação sugerida: `try: … finally: if temp_context is not None: temp_context.cleanup()`.
- esforço: S · risco: baixo (cleanup idempotente; coberto por `test_beta_cli.py`/`test_cli.py`)
- teste: suíte verde idêntica (equivalência).
- conflito: nenhum

### F-022 — `ocs.py`: guarda `audit → dropped → save_verified` duplicada 2×
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/ocs.py:743-751` (`sync_document`) e `:979-985` (`sync_strokes`)
- evidência: bloco de 7 linhas idêntico (`.tmp/audit/duplicates.txt`); extrair `_save_if_audit_clean(out_path)`.
- ação sugerida: método privado único com o mesmo `code="audit_lossy"`.
- esforço: S · risco: baixo (coberto por `test_ocs.py`)
- teste: equivalência — testes de sucesso e recusa existentes.
- conflito: nenhum

### F-023 — `step_export.py`: payload `regions` do job do worker duplicado 2×
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/step_export.py:819-825` (`document_to_step`) e `:973-981` (`export_dxf_ac1015`)
- evidência: listcomp `"regions": [{"outer": _wire_desc(...), "holes": [...]}]` byte-idêntica; extrair `_regions_payload(regions)`.
- ação sugerida: helper único; callers só adicionam campos próprios.
- esforço: S · risco: baixo (coberto por `test_step_export.py`/`test_ai_pipeline.py`)
- teste: equivalência.
- conflito: nenhum

### F-024 — `synthetic_worker.py`: construção de `CancellationEvidence` duplicada (só difere `kind`)
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/integrations/synthetic_worker.py:107-112` e `:128-133`
- evidência: dois ramos constroem o mesmo dataclass com `kind=WORKER_START_PREVENTED` ou `PROCESS_TERMINATED_BY_CANCELLATION` conforme `origin`; extrair `_evidence(job_id, origin)` (mapa já re-verificado em `jobs.py:302-310`/`sqlite.py:110`).
- ação sugerida: método único com o mapa origem→kind.
- esforço: S · risco: baixo (93% cobertura: `test_synthetic_worker.py`, `test_synthetic_runtime.py`)
- teste: equivalência.
- conflito: nenhum

### F-025 — `cli.py:_build_parser`: ~12 `add_argument` idênticos entre `export` e `beta-run`
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/cli.py:265-327` × `:356-435`
- evidência: flags `--vectorize-*`, `--out-dir`, `--origin`, `--draw-repo`, `--draw-commit`, `--clean`, `--stamps`, `--paper`, `--draw-config`, `--timeout-s` repetidos com texto quase idêntico; extrair `_add_common_args(sub)`.
- ação sugerida: helper de args comuns; normalizar (ou manter) pequenas divergências de help.
- esforço: S · risco: baixo-médio (parser resultante deve ser idêntico — conferir diff de `parse_args`)
- teste: e2e `main()` cobre (`test_cli.py`, `test_beta_cli.py`).
- conflito: nenhum

### F-026 — `persistence/sqlite.py:_commit` (CC 51): separar validação de registros da invariante de cancelamento
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/persistence/sqlite.py:64-130`
- evidência: `radon cc` CC=51; dois blocos puros precedem a escrita (checagem de porto `:65-81`; re-verificação de cancelamento `:101-119`) → `_validate_commit_records` + `_check_cancellation_consistency`.
- ação sugerida: split em 2 helpers; corpo transacional linear.
- esforço: M · risco: baixo-médio (96% cobertura: `test_sqlite_jobs.py`, `test_job_port_contract.py`)
- teste: equivalência — cada `ValueError` já é exercitado.
- conflito: nenhum

### F-027 — `cli.py:_cmd_export` (416 linhas, CC 53): split em 5 fases nomeadas
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/cli.py:439-854`
- evidência: fases já sinalizadas por comentários: `_export_resolve_and_probe`, `_export_vectorize`, `_export_build_document`, `_export_provenance` (~140 linhas de montagem pura), resto = DXF tmp → promote + escritas atômicas.
- ação sugerida: extrair as 5 fases.
- esforço: M · risco: médio (assinaturas intermediárias novas; `_beta_step_export` chama `_cmd_export` intacto)
- teste: `test_cli.py` compara stdout JSON + artefatos + fingerprint fim-a-fim.
- conflito: nenhum

### F-028 — `cli.py:_cmd_beta_run` (317 linhas): closure `process_proposals` de 98 linhas → função de módulo
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/cli.py:1768-1865` (+preflight `:1693-1747`)
- evidência: closure só usa `args`, `report`, `steps` → `_process_proposals(args, ctx, proposal_file, report, steps) -> bool` é move-only; ramos de erro (1801-1864) sem cobertura — caracterização parcial.
- ação sugerida: promover closure; opcionalmente extrair `_beta_preflight`.
- esforço: M · risco: médio
- teste: `test_beta_cli.py` cobre caminhos principais; ramos s/ cobertura pedem cuidado.
- conflito: nenhum

### F-029 — `provenance/sidecar.py:audit_sidecar` (238 linhas, **CC 129 — maior do repo**): split por seção
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/provenance/sidecar.py:151-388`
- evidência: `radon cc` CC=129; 4 seções independentes com nomes naturais (manifest_reference, source_artifacts, groups, mesh_artifacts+cross-check).
- ação sugerida: `_audit_manifest_ref`, `_audit_sources`, `_audit_groups`, `_audit_meshes`.
- esforço: M · risco: médio (plumbing dos estados intermediários; 84% cobertura `test_sidecar.py`)
- teste: equivalência — issues/states são o contrato verificado.
- conflito: nenhum

### F-030 — `validation/validator.py:_semantic` (209 linhas, CC 137): split por família de checks
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/validation/validator.py:279-487`
- evidência: `radon cc` CC=137; blocos independentes compartilham só `diagnostics` + índices → `_check_frames`, `_check_geometry`, `_check_materials`, `_check_cases`, `_check_artifacts`, `_check_capabilities`.
- ação sugerida: split por regra.
- esforço: M · risco: médio (6 assinaturas novas; ordem dos diagnostics estável pois só appends + sort em `:622`; 88% cobertura)
- teste: equivalência — cada diagnostic code é asserted em `test_validator.py`.
- conflito: nenhum

### F-031 — `physics.py`: dict de resultado do caso idêntico em pybullet/mujoco
- categoria: simplificação · severidade: baixa · confiança: evidência (duplicação); cobertura dos corpos dos engines = 0% → exige caracterização
- local: `backend/ocre_core/draw/physics.py:479-495` e `:546-562`
- evidência: `envelope_from_series` + `_impulse_per_step` + dict de 11 chaves duplicado, diferindo só `engine`/`version`/`headless` → `_case_result(...)`.
- ação sugerida: extrair helper; antes, teste de forma das chaves do dict (verificável sem engine instalada).
- esforço: M · risco: médio (blocos 441-483, 513-550 sem cobertura)
- teste: caracterização das chaves + `test_physics.py` verde.
- conflito: nenhum

### F-032 — `ocs.py:sync_document` (123 linhas): extrair indexação do documento e resolução de `expected_live`
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/ocs.py:670-715`
- evidência: prefixo de 46 linhas de validação pura → `_doc_stroke_index` + `_resolve_live_ids`; restante já é linear.
- ação sugerida: extrair os 2 helpers.
- esforço: M · risco: médio (**ressalva**: linhas 700-715 sem cobertura em `coverage.txt` — move mecânico, mas ramo pede caso de teste)
- teste: `test_ocs.py`; verificar cobertura do ramo `expected_live` via `test_beta_cli.py`.
- conflito: nenhum

### F-033 — `fea.py:run_axial_case` (214 linhas): extrair bloco de proveniência (sidecar+fingerprint)
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/fea.py:848-936`
- evidência: ~89 linhas finais de montagem (`summary_for_print`, digest, `export_parameters`, `derived`, `build_draw_sidecar`) independentes da física → `_attach_provenance(...)`.
- ação sugerida: extrair bloco de proveniência.
- esforço: M · risco: médio (~9 params; 91% cobertura `test_fea.py`)
- teste: equivalência — sidecar/fingerprint comparados no resultado.
- conflito: nenhum

### F-034 — `validator_draw.py` espelha ~90 linhas de helpers de `validation/validator.py`
- categoria: simplificação · severidade: baixa · confiança: evidência (duplicação quase idêntica) / inferência (separabilidade)
- local: `draw/validator_draw.py:68-93,:96-152,:210-236,:239-254,:283-291` × `validation/validator.py:36-54,:57-98,:123-153,:156-204`
- evidência: 16 blocos ≥8 linhas detectados (`duplicates.txt`): `StrictJSONError`, `_pairs_no_duplicates`, `_reject_constant`, `_walk_finite`, `_assert_local_refs`, `_add` são idênticos; `parse_json_strict`, `_format_checker`, `Diagnostic.as_dict`, `_pointer` têm deriva cosmética (~110 linhas espelhadas no total, >50% exatas). **Porém** docstring `validator_draw.py:4-6` declara mirroring deliberado ("a separate profile, not an extension of EIR v0.2").
- ação sugerida: **decisão do revisor** — se a separação é de contrato (perfis podem divergir), manter; senão extrair helpers comuns (`validation/_strict_json.py`) mantendo re-exports compatíveis (`document.py:30`, `cli.py:92` importam via `validator_draw`).
- esforço: M · risco: médio (acopla duas famílias de contrato)
- teste: `test_validator.py` + `test_validator_draw.py` + importadores de `parse_json_strict`.
- conflito: nenhum

### F-035 — `AdapterExecutionState` duplicado entre `contracts.py` e `jobs.py`
- categoria: simplificação · severidade: baixa · confiança: evidência (duplicação) / inferência (nenhum consumidor do de contracts)
- local: `backend/ocre_core/integrations/contracts.py:14-22` × `backend/ocre_core/jobs.py:25-35`
- evidência: mesmos 8 membros/valores, ordem diferente; `WORKER_ADAPTER_RUNTIME_CONTRACT.md:27` declara `jobs.py` como vocabulário de domínio; zero imports do de contracts.
- ação sugerida: se F-011 não remover o módulo, `contracts.py` deve importar/re-exportar `ocre_core.jobs.AdapterExecutionState` (elimina o homônimo e o risco `is`-compare de F-040).
- esforço: S · risco: médio (superfície de contrato)
- teste: suíte inalterada.
- conflito: nenhum (relaciona F-011, F-040)

### F-036 — `viz.py:_read_frd` (170 linhas): extrair montagem do mesh/point_data — **risco alto, condicionado a F-002**
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `backend/ocre_core/draw/viz.py:324-362`
- evidência: pós-parse (index_of, pts, mesh_cells, point_data ALL/DISP) separável de `_frd_mesh(...)`. `viz.py` tem **9% cobertura** — único teste aborta na coleta (F-002).
- ação sugerida: só após resolver F-002 e adicionar caracterização; sem rede de segurança executável hoje.
- esforço: M · risco: alto (bloqueado por F-002)
- teste: caracterização do parse (depende de `meshio` ou skip honesto).
- conflito: nenhum

### F-037 — `sha256_file` implementado duas vezes (streaming 1 MiB)
- categoria: simplificação · severidade: baixa · confiança: evidência
- local: `provenance/sidecar.py:20-26` × `draw/vectorize_runner.py:104-109`
- evidência: corpos idênticos; um terceiro existe em `step_worker.py:70` (worker noutro venv — fora de escopo). Ganho marginal (~7 linhas) vs. novo acoplamento provenance→draw.
- ação sugerida: menor — só fazer se já houver módulo utilitário natural; senão registrar e seguir.
- esforço: S · risco: baixo
- teste: equivalência (`test_sidecar.py`, `test_vectorize_runner.py`).
- conflito: nenhum

## Grupo 6 — Inferências e notas de higiene (falta prova para virar evidência, ou ação não-código)

### F-038 — `timed_out` pode marcar execução dentro do prazo (join dos drains usa deadline original)
- categoria: bug · severidade: baixa-média · confiança: **inferência** (janela exige término colado no deadline + drain atrasado)
- local: `backend/ocre_core/integrations/process_host.py:131-151` (join `:139`, flag `:140-141`)
- evidência parcial: leitura — após `wait(timeout=deadline)` retornar a tempo, `drain.join(timeout=max(0, deadline-now))` dá ≤ε aos drains; `is_alive()` → `timed_out=True` indevido.
- ação sugerida: medir/resetar deadline por drain ou reportar `drain_incomplete` separado de `timed_out`.
- esforço: M · risco: baixo
- teste (repro): processo que despeja saída grande e termina a ~95-99% do timeout → `timed_out` deveria ser `False`.
- conflito: nenhum

### F-039 — `submit_job` não mapeia `PersistenceConflict` → 409 (assimétrico com `cancel_job`)
- categoria: bug · severidade: baixa · confiança: **inferência** (caminho quase inalcançável)
- local: `backend/ocre_core/application/services.py:236-245` vs `:308-313`; origem `sqlite.py:159`
- evidência parcial: `create_execution` pode levantar `PersistenceConflict` em `IntegrityError`; `submit_job` só captura `IdempotencyConflict` → 500 em vez de 409; `cancel_job:312` prova o mapeamento esperado.
- ação sugerida: capturar `PersistenceConflict` → `sequence_conflict`/409 como no cancel.
- esforço: S · risco: baixo
- teste (repro): forçar `IntegrityError` (message_id pré-inserido) → hoje `PersistenceConflict` cru.
- conflito: nenhum

### F-040 — Persistência compara enums com `is`: duplicação de `AdapterExecutionState` é latente
- categoria: bug · severidade: baixa · confiança: **inferência** (hoje inalcançável — o store só recebe `jobs.AdapterExecutionState`)
- local: `persistence/sqlite.py:93,:100,:103,:112` × `integrations/contracts.py:14-22`
- evidência parcial: `event.from_state is not old.state` falharia se uma transição usasse o enum homônimo de `contracts.py` — a desserialização é via codec JSON (`codec.py:116-119`, `_ENUMS` + `cls(value)`), não pickle; a classe errada entraria por uso direto do módulo. Liga-se a F-011/F-035.
- ação sugerida: dedup do enum (F-035) ou comparar por valor.
- esforço: S · risco: baixo
- teste (repro): `AdapterExecutionTransition` com `contracts.AdapterExecutionState` em `commit_transition` → `ValueError("event from_state differs…")` com valores iguais.
- conflito: nenhum

### F-041 — `Proposal.content` shallow-copied compartilha dict entre registros (`frozen` sugere imutável)
- categoria: bug · severidade: baixa · confiança: **inferência** (nenhum caller muta hoje)
- local: `backend/ocre_core/draw/ai/proposals.py:103,:121,:132`
- evidência parcial: `confirm(p).content is p.content` — mutar um registro altera o outro; nested mutables do caller também compartilhados. Resto do codebase faz deep-copy defensivo (`document.py`, `v01_to_v02.py`).
- ação sugerida: deep-copy (`copy.deepcopy`) ou mapping imutável na construção/transições.
- esforço: S · risco: baixo
- teste (repro): `p2 = confirm(p1); p2.content["x"]=1; assert "x" not in p1.content` → falha hoje.
- conflito: nenhum

### F-042 — Vars de unpack não usadas: `param` (`fea.py:429`), `dot` (`approval_fingerprint.py:69`)
- categoria: morto (trivial) · severidade: baixa · confiança: **inferência** (vulture-60; renomear para `_` perde documentação do formato)
- local: `backend/ocre_core/draw/fea.py:429` (`_, _, param, count = …` header `$Nodes` .msh); `backend/ocre_core/validation/approval_fingerprint.py:69` (`whole, dot, fraction = mantissa.partition(".")`)
- ação sugerida: manter como documentação ou `_`-prefixar — menor, opcional.
- esforço: S · risco: trivial
- teste: suíte inalterada.
- conflito: nenhum

### F-043 — 7 pins de dependências possivelmente redundantes
- categoria: morto (deps) · severidade: baixa · confiança: **inferência** (pins rígidos upstream; remover só despina versão)
- local: `backend/requirements-viz.txt` (`pillow`, `pooch`, `scooby`); `backend/requirements-step-worker.txt` (`ocp_gordon`, `ocpsvg`, `cadquery-ocp-proxy`); + 1 listado em `2b-deps.txt`
- evidência parcial: zero imports diretos no código OCRE; todos são `Requires-Dist` rígidos de pyvista/build123d — pins explícitos redundantes, mas podem ser intencionais p/ reprodutibilidade (`.tmp/audit/2b-deps.txt`).
- ação sugerida: **reportar** — regra do plano proíbe alterar `requirements*.txt` sem aprovação.
- esforço: S · risco: baixo-médio
- teste: —
- conflito: nenhum

### F-044 — `.worktrees/draw-core`: repo estrangeiro dentro da raiz do OCRE
- categoria: morto (higiene de workspace) · severidade: baixa · confiança: evidência
- local: `.worktrees/draw-core/` — `git remote -v` = `github.com/santleme/draw-core` (projeto separado, branch `feat/vectorize-paper-mm`); `.worktrees/arquivo-prs-1-2/` = snapshots soltos; `.worktrees/runs/` = artefatos com binários gmsh/ccx.
- evidência: `git worktree list` não registra draw-core/arquivo-prs-1-2; `AGENTS.md` proíbe misturar projetos na pasta — já existe, gitignored, pré-datando a auditoria.
- ação sugerida: nenhuma ação de código; registrar para o dono decidir o destino (projeto vivo de outra pessoa — **não remover**).
- esforço: — · risco: —
- teste: —
- conflito: nenhum

---

## Descartados (investigados e rejeitados — para o revisor não refazer)

**Fase 1/estrutura**
- `jobs.py` × `persistence/jobs.py` e `sync.py` × `persistence/sync.py` — layering intencional (domínio × portas/adapters), não duplicação. Confirmado 2× (Fases 1 e 2C).
- 46 "módulos sem importador" — todos falsos positivos (imports relativos não resolvidos pelo script) ou entry points de subprocesso (`render_worker`, `step_worker`, `physics.__main__`, `fake_vectorize.py`, `make_canvas.py`). Tabela: `no-importers-verification.md`.
- `step_worker`/`render_worker` 0% cobertura — fronteira de processo (coverage in-process não enxerga subprocesso), não código morto.

**Bugs**
- `app.py` B008 `Depends` default — idiom FastAPI, não estado mutável.
- `services.py:263` B904 raise sem `from` — normalização deliberada p/ `ApplicationError`.
- B905 `zip()` sem `strict=` — comprimentos iguais por construção/checagem prévia.
- PLW1510 subprocess sem `check=` — `returncode` conferido manualmente em todos os casos.
- PLW0603 `global _sys_executable_has_build123d` — cache de processo documentado.
- `assert process_host.py:126` — invariante garantida por `stdout/stderr=PIPE`.
- Pyflakes F401 (`physics.py` pybullet/mujoco/pinocchio; `fmu_gate.py` fmpy) — probes `# noqa` + uso real.
- TODO/FIXME/XXX/HACK — 5 hits, todos falsos positivos.
- 72 skips/xfails — todos com motivo documentado.
- `observer.py`/`viz.py:719`/`cli.py:1856` `except Exception` — honestos (status/report), não engolem falha.
- TOCTOU `SQLiteSyncStore.evaluate` — pré-checagem advisory; `commit` re-avalia sob `BEGIN IMMEDIATE`.
- `sync.py:57-60` CAS com `revision NULL` — `rowcount=0` → `PersistenceConflict` → rollback (fail-closed).
- `sqlite.py:98` "terminal snapshot is immutable" — defesa em profundidade intencional.
- Rotas/códigos ausentes na API (`unauthenticated`, `forbidden`, `/events/stream`, …) — propostas condicionais do draft CLOUD_API_CONTRACT; `result_available:false` é honesto (sem rota de resultado).
- `cancel` reutilizando `cancellation_unsupported_in_phase` fora de `collecting` — extensão não especificada, não violação.
- `AdapterExecutionState` duplicado — descartado como divergência doc×impl (camadas prescritas); mantido como finding de código (F-035/F-040).
- `_validate_groups` exigindo um caso — consistente com a assinatura `validate()`.
- Replay de cancel retornando snapshot do commit — é o "recibo original" que o doc pede.
- Re-confirmação de proposta — no-op idempotente conforme doc.

**Código morto**
- `model_config`, handlers `application_error`/`get_job`, `valid_datetime`, `do_GET`/`do_POST` — decorators FastAPI/jsonschema/stdlib.
- `contracts.py:128,130` `immutable_*` (vulture-80) — nomes de parâmetro de Protocol.
- `models.py` demais campos/enums — superfície de contrato (`contract_version` via `getattr`; membros de enum estreitariam contrato — mantidos como inferência em F-043/nota).
- `catalog.match`, `MATERIALS`, `new_proposal`, `observe_conversion`, `build_context`, `register_model`, `require_model`, `confirm`, `reject`, `is_pending`, `check` (nome) — callers reais em produção/teste.
- Deps: `vtk` (probe `importlib.metadata`), `httpx2` (TestClient), `pin`/pinocchio, `build123d`, `cadquery-ocp-novtk`, `meshio` (viz) — usados.
- npm (`three`, `@types/three`, `typescript`, `vite`) — todos com uso; zero exports TS sem uso.
- Código comentado em bloco — 6 hits, todos prosa/fórmulas.
- Arquivos `.bak/.orig/~`/`old/legacy/backup` — `git ls-files` zero hits; `solver.*.log` tracked são evidência referenciada por manifests.
- `make_canvas.py` — ferramenta manual documentada no README da fixture.

**Simplificação**
- `transition_job` × `transition_adapter_execution` — kwargs de dataclasses distintas (superficial); já fatorados via helpers comuns.
- idiom `subprocess.run(...)` ×4 gates — receita padrão com tratamento diferente; wrapper seria repasse.
- `_commit` re-validando domínio — defense-in-depth exigida pelo contrato do porto.
- `services.py:318` re-checagem de cursor/limit — aplicação framework-independent defende o próprio contrato.
- `max_body_bytes`, `include_pending`, `emit_circles`, `observe_conversions`, `revision_snapshot`/`get_revision` — exercitados por testes (último: contrato do store sem consumidor de produção — nota 2B).
- `_safe_local_file` × `_uri_path` — semânticas deliberadamente distintas (auditoria vs rejeição).
- `validator._semantic` × `validator_draw._semantic` — regras diferentes por família; só helpers duplicam (F-034).
- Escrita atômica inline `step_export.py:908-918` × `_atomic_write_bytes` — compartilhar cruzaria direção de dependência; marginal.
- `codec._reject_constant` × homônimos — helpers de 2 linhas com tipos de erro diferentes.
- `OcsClient.get_stroke_id` — único consumidor, já plano.
- `SyntheticWorker.run` ramos cancel — state machine sem split óbvio.
- `_strict_body` dupla verificação — header mentiroso/chunked, intencional.
- `ValidatorPreflight.evaluate` — necessário (porto real não pode afirmar readiness).
- `app.py` `openapi()` wrapper — resolve `$defs` (comentário no código).
- Funções >80 linhas não propostas (`validator_draw._semantic/_check_previous`, `step_worker._job_export`, `ocs.sync_strokes`, `fingerprint._json_payload`, `migration._pending`, `ai/semantics.analyze`) — pipelines de checks independentes ou contratos por braço; split geraria só pass-throughs.
