# Núcleo inicial do backend OCRE

Este backend contém provas locais em desenvolvimento do Core: domínio de jobs
e sync, validação EIR, serviços de aplicação, persistência SQLite, transporte
FastAPI local, codificação pura de OCRE-AF/1, migração explícita v0.1→draft
v0.2 e um worker sintético. Não representa um Core Cloud implantado nem promete
compatibilidade com infraestrutura de produção.

## Implementado localmente

- `ocre_core.validation.validator`: parsing JSON estrito, validação estrutural
  dos schemas EIR v0.2 sob Draft 2020-12, verificações semânticas do perfil
  axial, referências/manifesto de grupos e verificação confinada de URI/hash
  dos artefatos quando há raiz de bundle. Os contratos e schemas EIR v0.2
  permanecem congelados.
- `ocre_core.validation.approval_fingerprint` + `validator`: codificador puro
  do fingerprint OCRE-AF/1 integrado ao pré-flight. Com `approval_context`
  (identidade do validador, registros de adapters e política de preparação —
  insumos que só a plataforma pode declarar), bytes exatos do manifesto
  staged (`bundle_manifest_uri`), raiz de bundle e capacidades verificadas,
  o validador monta os dez campos normativos, calcula o digest e só então
  reporta `execution_readiness: ready`; a porta real também exige que
  adapter/revisão/projeto da submissão correspondam ao aprovado. Sem esses
  insumos a porta (`ValidatorPreflight` sem `PreflightInputsProvider`)
  permanece bloqueada com `approval_fingerprint_unavailable`, e qualquer
  divergência de case/adapter/capacidade/submissão vira diagnóstico de
  bloqueio. Ainda não há provider real de insumos nem pacote imutável de
  validador: a execução local real continua indisponível.
- `ocre_core.migration.v01_to_v02`: migrador explícito, somente em memória e
  sem alterar bytes de origem, que produz documento `draft` v0.2 e relatório
  versionado com resoluções, pendências e validação de autoria. A migração não
  aprova documentos nem libera execução; referências não inferíveis exigem
  resolução explícita e o draft continua bloqueado.
- `ocre_core.integrations.synthetic_worker`: harness local que avança jobs
  enfileirados por estados sintéticos de sucesso, falha ou cancelamento e grava
  eventos/outbox rotulados como simulação. Não inicia processo, solver,
  broker ou serviço externo e não produz resultado científico.
- `ocre_core.persistence.sqlite.SQLiteJobStore` e
  `ocre_core.persistence.sync.SQLiteSyncStore`: prova local de transações,
  retomada e sync concorrente em SQLite. `FakeOutboxDispatcher` e
  `FakeMessageConsumer` são fakes locais para demonstrar entrega pelo menos uma
  vez e deduplicação. Testes também exercitam crash entre publicação e ACK,
  redelivery duplicada, replay idempotente e concorrência/falhas em transições.
  Não são integração com PostgreSQL, cloud, broker ou workers reais.
- `ocre_core.api.create_app`: API FastAPI local implementada e testada, com
  rotas para sync, submissão/status/cancelamento de jobs e replay de eventos.
  A operação cloud continua pendente; não há IAM real, broker, worker ou cloud,
  e a API exige dependência de identidade injetada.

`backend/requirements.txt` declara `jsonschema==4.26.0`. Essa é a versão
declarada, não uma afirmação sobre o ambiente instalado. No ambiente Python 3
consultado para esta atualização, a versão instalada é `4.10.3`.

## Limites e próxima sequência

- O codificador OCRE-AF/1 já está integrado ao pré-flight: com os dez insumos
  normativos verificados o digest é calculado e a prontidão pode ser
  reportada. Falta um `PreflightInputsProvider` real — bundle staged
  imutável, capacidades autoritativas, pacote imutável do validador e
  binários de adapter — sem o qual a porta real segue bloqueada. A migração
  v0.1→v0.2 nunca pode liberar aprovação ou execução.
- Completar capacidades autoritativas de adapters, cobertura física e
  proveniência CAD/geometria/malha. O sidecar experimental atual registra
  proveniência de geometria sintética `.geo` para malhas e mede uma métrica de
  qualidade sem gate/limiar geral; não cobre STEP/OCCT nem validação física.
  Os checks do perfil axial atual também não constituem validação física geral.
- Evoluir a API local implementada/testada para operação Core Cloud, com
  autenticação/autorização reais e implantação; integrar banco de produção,
  broker e Scientific Workers reais. A prova SQLite/outbox/sync permanece
  estritamente local.
- Evoluir e verificar o migrador explícito EIR v0.1→draft v0.2; a conversão
  local já existe, mas permanece não aprovativa e requer resoluções explícitas.

No produto, a arquitetura prevista é Core Cloud + Scientific Workers. IAM,
fila/serviços externos, workers e motores científicos reais ainda estão
pendentes. Esta implementação local não faz despacho real para workers.

## Draw AI to CAD beta

The optional human-gated AI workflow, provider injection seam, proposal
ledger, CAD-only limits, and current integration status are documented in
[`docs/draw-ai-cad-pipeline.md`](../docs/draw-ai-cad-pipeline.md). The AI layer
contracts and architecture history are in
[`docs/architecture/DRAW_AI_LAYER.md`](../docs/architecture/DRAW_AI_LAYER.md).

Verificação local de imports: `.venv/bin/python -m pyflakes ocre_core tests`.
