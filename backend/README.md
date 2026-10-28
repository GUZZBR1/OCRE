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
- `ocre_core.validation.approval_fingerprint`: codificador puro do fingerprint
  OCRE-AF/1 com canonicalização/framing e validação dos dez campos normativos.
  Ele ainda não está integrado ao contexto de validação ou aprovação: o
  pré-flight não dispõe dos insumos verificados e mantém
  `execution_readiness: blocked` (`approval_fingerprint_unavailable`). Não
  libera aprovação ou execução.
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

- Integrar o codificador OCRE-AF/1 ao fluxo de aprovação usando todos os
  insumos normativos verificados. Até que isso e os demais gates estejam
  completos, o pré-flight permanece bloqueado; a migração v0.1→v0.2 nunca pode
  liberar aprovação ou execução.
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
