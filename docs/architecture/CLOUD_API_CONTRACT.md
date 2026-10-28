# Contrato draft da API Core Cloud

**Estado:** draft técnico para revisão; não define implementação, framework HTTP,
provedor cloud, banco, broker, formato de token nem transporte de streaming.
Este documento descreve a superfície externa que deverá conectar o domínio de
sync, o lifecycle de jobs e os ports conceituais de persistência. Nenhuma rota
é implementada por este contrato.

**Autoridade do produto:** Core Cloud é autoridade para identidade, projetos,
revisões aceitas, validação/preflight, lifecycle de jobs e resultados
publicados. O cliente local leve mantém cache e operações pendentes e sincroniza
com a nuvem; não executa motores científicos pesados. Scientific Workers
executam motores isolados e reportam observações ao Core Cloud. Processo host
isolado não é, por si só, sandbox.

**Compatibilidade:** EIR v0.2 e seus schemas congelados não são alterados nem
reinterpretados aqui. A API aceita referências/versões de EIR já definidas,
mas não cria semântica de operações CAD, conversão ou merge.

## 1. Convenções comuns

- Prefixo draft: `/v1`. O prefixo identifica a versão da API; a versão de
  schema de cada payload permanece explícita onde aplicável. Mudanças
  incompatíveis exigem nova versão de API ou negociação documentada; campos
  opcionais podem ser adicionados sem alterar a semântica existente.
- JSON em UTF-8 para envelopes de controle. Payloads binários de operações e
  artefatos são referenciados por upload/download opaco e autenticado; não se
  assume que bytes arbitrários sejam JSON ou EIR.
- IDs são opacos. Timestamps RFC 3339 UTC são informativos e não ordenam
  concorrência. Sequência do agregado é a ordem autoritativa.
- Toda resposta de erro usa `application/problem+json` conceitualmente:

```json
{
  "type": "https://ocre.example/problems/sync-conflict",
  "code": "sync_conflict",
  "title": "Base revision is no longer current",
  "status": 409,
  "detail": "The batch was not committed.",
  "request_id": "req_…",
  "retryable": false,
  "current_head_revision_id": "rev_…"
}
```

  `type`, `code`, `title`, `status`, `request_id` são comuns. `detail` é
  seguro para cliente; extensões são tipadas por `code`. Não expor stack,
  caminho interno, segredo, conteúdo de outro tenant ou detalhes de worker.
- Erros comuns: `unauthenticated` (401), `forbidden` (403),
  `resource_not_found` (404, inclusive para recurso fora do tenant quando
  necessário para evitar enumeração), `validation_failed` (422),
  `idempotency_key_reused` (409), `sync_conflict` (409),
  `sync_batch_identity_conflict` (409), `sequence_conflict` (409),
  `cancellation_unsupported_in_phase` (409), `rate_limited` (429, com
  `Retry-After`), `temporarily_unavailable` (503). Um conflito não é retry
  cego; `retryable` descreve a resposta específica.
- Limites são configuráveis e publicados como capacidades do serviço. Draft
  requer limites explícitos para bytes por request/operação, quantidade de
  operações por batch, frequência de sync, conexões de eventos, faixa máxima
  de replay e tamanho do buffer; os valores numéricos dependem de operação,
  storage e política de produto e não são inventados aqui. Exceder limite
  retorna `request_too_large` (413) ou `rate_limited` (429) sem commit parcial.

## 2. Identidade, tenancy e autorização

O mecanismo de autenticação fica em aberto. Todo request autenticado resolve
um `principal_id` e um único contexto `tenant_id` a partir de credenciais
verificadas pelo serviço; IDs enviados no corpo nunca concedem identidade.
`actor_id` em operação de sync é uma declaração/audit field e deve ser
derivado do principal ou rejeitado quando divergir dele, não tratado como
prova de identidade.

Todos os recursos pertencem a tenant. Projeto e recursos filhos (revisões,
jobs, execuções, eventos e artefatos) são autorizados por associação ao tenant
e pelas permissões do projeto, em cada request e reconexão. Nenhuma rota aceita
troca de tenant via query/body como substituto de autorização. Respostas e
cursor não podem permitir leitura cruzada. A política de membership, papéis,
delegação service-to-service, credenciais de worker, expiração/revogação e
auditoria de acesso permanece a definir.

Scopes draft de autorização: `projects:read`, `projects:write`, `sync:write`,
`jobs:submit`, `jobs:read`, `jobs:cancel`, `artifacts:read`, `events:read`;
além disso, exige-se permissão sobre o projeto indicado. A nomenclatura é
proposta, não decisão de IAM.

## 3. Projetos e revisões

| Método e rota draft | Uso |
| --- | --- |
| `POST /v1/projects` | Criar projeto no tenant autenticado. |
| `GET /v1/projects` | Listar somente projetos visíveis ao principal; paginação opaca. |
| `GET /v1/projects/{project_id}` | Ler metadados e `head_revision_id`. |
| `GET /v1/projects/{project_id}/revisions/{revision_id}` | Ler metadados imutáveis de revisão/manifesto. |

Criação retorna `201` e `{project_id, tenant_id, name, created_at}`. Revisão
aceita é imutável e expõe `{revision_id, project_id, parent_revision_id,
sequence, created_at, manifest_ref}`. `sequence` ordena revisões de um projeto,
sem impor que IDs ou hashes tenham forma específica. Conteúdo de projeto é
obtido por referência autorizada; não inserir bytes de arquivo arbitrário nos
envelopes de controle. Criação/rename de projeto e paginação são propostas de
API e não são suportadas por novos ports descritos neste draft.

## 4. Sync batch, replay e conflito

| Método e rota draft | Uso |
| --- | --- |
| `POST /v1/projects/{project_id}/sync-batches` | Enviar batch ordenado. |
| `GET /v1/projects/{project_id}/sync-batches/{batch_id}` | Recuperar recibo ou estado conhecido após resposta perdida. |
| `GET /v1/projects/{project_id}/sync` | Rota proposta, somente após novo read model durável; ler revisões aceitas (não eventos de domínio) desde cursor. |

Envelope de envio:

```json
{
  "schema_version": "ocre.sync-batch.v1",
  "batch_id": "batch_…",
  "actor_id": "principal_…",
  "base_revision_id": "rev_…",
  "operations": [
    {"operation_id": "op_…", "payload_ref": "upload_…"}
  ]
}
```

`base_revision_id` pode ser `null` apenas para projeto sem cabeça inicial. A
ordem das operações integra a identidade do envelope. `payload_ref` é uma
representação de transporte para os bytes opacos do domínio; sua associação,
integridade e autorização devem ser verificadas. Se o transporte usar inline
base64, isso requer perfil separado e mesmos limites.

Resposta commitada (`201`): `{batch_id, project_id, committed_revision_id,
head_revision_id, revision_sequence, replayed:false}`. Repetição exata da
mesma chave e envelope retorna recibo original (`200`, `replayed:true`) sem
criar revisão/evento. Mesmo `(project_id,batch_id)` com envelope diferente
retorna `409 sync_batch_identity_conflict`. Base divergente retorna `409
sync_conflict` com `expected_base_revision_id` e `current_head_revision_id`;
não grava nada. O cliente preserva operações pendentes; não faz merge, rebase,
descarte nem sobrescrita automática. Tentativa alterada requer nova identidade
de batch e decisão explícita do usuário/produto.

A identidade de replay do domínio é `(project_id, batch_id)`, mas isolamento
multi-tenant é requisito obrigatório para a API e o futuro port. Antes da
implementação, o lookup e a unicidade do recibo devem ser tenant-qualified
(`tenant_id` + identidade de batch), ou a API deve validar autorização do
projeto dentro do tenant antes de qualquer lookup que possa revelar recibo ou
colisão. Nunca consultar recibo por IDs não autorizados nem permitir replay
cross-tenant. A identidade efetiva deve alinhar-se ao padrão do port de jobs
`(tenant_id, operation, key)`; em sync, `operation` identifica a operação de
batch e a identidade do batch faz parte do escopo/fingerprint. Isto é requisito
pré-implementação, não alegação de vulnerabilidade explorável hoje.

Consulta de sync usa `{after_revision_sequence, limit}` ou cursor opaco apenas
se um read model persistente adicionar sequência/histórico de revisões; o
domínio `SyncBatch` atual não define sequência de revisão nem leitura histórica.
Essa rota retorna revisões aceitas (não eventos arbitrários) e é proposta
condicional, não disponível pelos ports atuais. O cursor é dica de posição
dentro do histórico do projeto e deve carregar escopo de tenant/projeto ou ser
validado contra ele. Resposta informa `head_revision`, itens ordenados,
`next_cursor` e `has_more`. Cursor expirado/fora da retenção retorna `410
cursor_expired` e exige snapshot/head completo seguido de sync; não simula
eventos ausentes.

A busca de batch anterior precede a rejeição de base obsoleta. O commit exige
atomicamente unicidade da identidade, igualdade exata do batch, CAS da cabeça,
append de operações/revisão imutável e recibo. `SyncReady` de leitura não é
aceitação. Com concorrência, no máximo uma gravação avança uma mesma cabeça;
perdedores recebem conflito, sem escrita parcial. Retenção de batches/recibos
deve cobrir a janela de retry offline publicada; duração e estratégia de
arquivamento dependem do port.

## 5. Jobs: submit, status, cancel e collect

| Método e rota draft | Uso |
| --- | --- |
| `POST /v1/projects/{project_id}/jobs` | Validar/preparar e submeter execução baseada em revisão imutável. |
| `GET /v1/projects/{project_id}/jobs/{job_id}` | Ler projeção autoritativa e sequência atual. |
| `POST /v1/projects/{project_id}/jobs/{job_id}/cancel` | Registrar pedido de cancelamento idempotente. |
| `POST /v1/projects/{project_id}/jobs/{job_id}/collect` | Comando proposto para iniciar/recuperar coleta conforme política do adapter. |
| `GET /v1/projects/{project_id}/jobs/{job_id}/result` | Ler artefato/manifesto publicado após sucesso. |

Submit draft:

```json
{
  "schema_version": "ocre.job-submit.v1",
  "design_revision_id": "rev_…",
  "case_ref": "case_…",
  "adapter_id": "adapter_…",
  "adapter_version": "…",
  "options_ref": "upload_…"
}
```

`Idempotency-Key` é obrigatório para submit e cancel. A associação de projeto e
revisão é validada no servidor; request fingerprint cobre campos semânticos
normalizados e referências imutáveis, não cabeçalhos voláteis. Escopo de
persistência de job: `(tenant_id, operation, key)` como o port define;
`operation` identifica a operação/versionamento sem colisão entre submit,
cancel e outras mutações. O projeto/job entra na fingerprint e autorização,
não substitui tenant no escopo da chave. Mesma chave/fingerprint reproduz o
recibo original; chave reaproveitada com fingerprint diferente é `409
idempotency_key_reused`. Janela de retenção de chave deve ser publicada e cobrir
retries do cliente; prazo concreto depende de storage.

Falhas de validação/preparação são relatórios síncronos tipados (`422
validation_failed` ou `422 preparation_failed`) e não criam execução nem evento
de lifecycle. Submissão aceita retorna `202` com `{job_id, state:"queued",
sequence, status_url, events_url, submitted_event_id, replayed}`. A criação
persistida inicial associa snapshot genérico e snapshot de execução do adapter,
evento `ExecutionSubmitted`, recibo e outbox como unidade atômica, sem chamada
de broker/worker dentro da transação.

Status inclui `{job_id, project_id, state, sequence, updated_at,
design_revision_id, adapter_execution:{state,sequence}, progress?,
failure?, result_available}`. A enumeração genérica permitida é
`created|validated|queued|preparing|running|cancel_requested|collecting|
succeeded|failed|cancelled`. Execução do adapter mantém seu vocabulário
especializado (`execution_failed`, `collection_failed`); ambos projetam para
`failed` no job. Resposta de status é snapshot consultivo atual, não fonte para
inferir eventos intermediários perdidos.

Cancel retorna `202` quando pedido registrado e `{job_id,state:
"cancel_requested",sequence}`; mesma chave/fingerprint reproduz o recibo
original do commit com `replayed:true`, sem nova transição. Um pedido sem
transição porque o job já está terminal retorna o snapshot atual e não grava
recibo de transição/outbox; a política de recibo para no-op depende de extensão
do port e não pode fingir um commit. A
conclusão observada antes do cancelamento vence; em `collecting`, responde `409
cancellation_unsupported_in_phase`. `cancelled` só é estado final com
evidência autoritativa vinculada à fase/origem (`queued` com worker impedido
de iniciar; `running` com processo terminado pelo cancelamento). Nunca inferir
cancelamento de timeout, pedido aceito ou ausência de heartbeat. Se estado já
terminal, não criar transição; retornar estado vigente. `POST /collect` é uma
proposta de comando idempotente e não autoriza executar um adapter no processo
da API. Solicita iniciar ou recuperar a etapa de coleta sob a política do
adapter; coleta ocorre durante `collecting`, antes do sucesso terminal. Só é
aceita quando lifecycle/adapter permitem coleta, persiste transição e outbox
segundo o port, e devolve `202` com status; chamada repetida com mesma chave
recupera o recibo. Se a plataforma faz coleta automaticamente ao observar
término, a rota pode somente recuperar acompanhamento/recibo da coleta em
andamento ou concluída, sem forçar nova execução. Falha de coleta mantém
`collection_failed` na projeção do adapter e `failed` no job. Separadamente,
`GET /result` apenas lê manifesto/artefato já publicado, disponível após
`succeeded`; enquanto coleta está em andamento ou não há publicação, retorna
`409 result_not_available`. GET nunca inicia nem recupera coleta. Erro de
execução/coleta não expõe artefato como resultado válido.

O submit referencia uma revisão aceita, não carrega uma interpretação própria
do EIR. Validação EIR/preflight, capacidades/licenças, quotas de recursos e
retentativas automáticas devem ter relatórios explícitos. API não promete
execução exactly-once nem retry automático de falhas de worker.

## 6. Eventos, polling e stream

| Método e rota draft | Uso |
| --- | --- |
| `GET /v1/projects/{project_id}/events?cursor=…` | Poll finito de eventos por projeto. |
| `GET /v1/projects/{project_id}/jobs/{job_id}/events?cursor=…` | Poll finito restrito ao agregado. |
| `GET /v1/projects/{project_id}/events/stream?cursor=…` | Stream reconectável; mecanismo HTTP (por exemplo, SSE) fica em aberto. |

Envelope público draft:

```json
{
  "event_id": "evt_…",
  "project_id": "proj_…",
  "aggregate_type": "job",
  "aggregate_id": "job_…",
  "sequence": 7,
  "event_type": "job.state_changed",
  "occurred_at": "2026-10-28T12:00:00Z",
  "data": {"from_state":"running","to_state":"collecting"},
  "cursor": "opaque-project-cursor"
}
```

Para revisão aceita, envelope inclui `aggregate_type:"project"`,
`aggregate_id:project_id`, `sequence:revision_sequence` e
`event_type:"project.revision_committed"`. Para submit, `job.submitted` liga
job e execution IDs. Outros eventos de job refletem somente transições
persistidas; erros pré-flight síncronos não viram evento de execução.

**Fonte autoritativa:** evento persistido junto ao snapshot e sequência do
agregado é autoritativo; `event_id` apenas identifica o evento. CAS e append no
port definem a ordem do lifecycle. Cada job e execução tem sequência própria;
uma sequência não ordena ambas. `ExecutionSubmitted` será exposto como evento
do agregado `job` com ligação ao `execution_id`; eventos posteriores de execução
podem usar agregado `execution` e sua própria sequência. Timestamp e ordem de
chegada do broker não são autoritativos. O contrato atual não fornece sequência
de projeto nem cursor/feed cross-aggregate. O cursor proposto é apenas token de continuação na ordenação durável do feed do projeto;
não é a autoridade do agregado nem prova de processamento pelo cliente. A API
precisa definir um high-water mark durável para evitar omissões. Sua geração,
total order cross-aggregate, consistência entre shards e persistência não estão
definidas pelos ports atuais; até isso existir, eventos podem ser consultados
por agregado usando `sequence` e o stream global não pode prometer ordem total.

Poll retorna eventos em ordem estável, `next_cursor`, `has_more` e
`head_cursor`. Stream pode enviar heartbeat sem evento, comentários/frames de
controle e indicação de limite; heartbeat nunca avança cursor. Cliente grava o
último cursor/event ID confirmado localmente e reconecta a partir dele; pode
receber duplicatas, que deduplica por `event_id` e valida por sequência. Não se
promete exatamente uma vez. Se cursor antigo saiu da retenção, servidor informa
`410 cursor_expired`, informa a cabeça atual e instrução de ressincronização;
cliente busca snapshots (`GET project`, `GET job`) e abre feed da cabeça.

Backpressure: servidor limita lote, bytes, conexões e buffer por cliente; ao
atingir limite, encerra stream com cursor de retomada ou exige polling. Não
descarta silenciosamente evento durável e não muda estado de job por lentidão
do consumidor. Política de heartbeat, limite numérico, timeout, batching,
compressão e transporte são configurações abertas. Polling não cria eventos.

Outbox fornece publicação pelo menos uma vez; consumidores/dispatcher devem
deduplicar por `message_id`/`event_id`. A API só pode afirmar disponibilidade
eventual após commit; atraso de publicação é possível. Não se promete latência
máxima nem exactly-once. Retenção do feed, cursor opaco, índice high-water,
backfill, compactação e snapshot de recuperação exigem desenho no port/outbox.

Estados terminais de job: `succeeded`, `failed`, `cancelled`; são imutáveis.
Um cliente reconectado pode obter o snapshot terminal via status mesmo se
eventos intermediários expiraram. Coleta e manifesto continuam sob autorização
do projeto.

## 7. Decisões abertas e limites de implementação

1. **Persistência de sync:** não há port de storage de sync identificado no
   repo; implementar append/CAS/recibo/revisão atômicos, comparação exata de
   envelope, retenção de recibos e lookup tenant-qualified ou autorização do
   projeto antes do lookup. A identidade efetiva deve alinhar tenant + operação
   ao escopo de idempotência do port de jobs. São requisitos prévios à
   implementação; não se afirma vulnerabilidade explorável no código atual.
2. **Outbox e feed:** port de job descreve commit atômico com outbox, mas não
   define API de leitura/retenção de eventos nem cursor cross-aggregate. Definir
   high-water durável, escopo de cursor, ordenação, backfill, deduplicação,
   poison messages, leases e política de retenção.
3. **Idempotência:** definir canonicalização/fingerprint para JSON, referências
   de upload e aliases; política de expiração e resposta ao replay; alinhar
   sync `(project_id,batch_id)` ao escopo sem reduzir isolamento tenant.
4. **Identidade/autorização:** selecionar mecanismo e lifecycle de credenciais,
   membership e permissões worker/service; fechar política anti-enumeração.
5. **Transporte e limites:** escolher polling/stream e limites/quotas por plano,
   disponibilidade, payload, conexões e retenção offline. Valores devem ser
   publicados antes de cliente depender deles.
6. **Uploads/artefatos:** definir URL/ref efêmera, checksum, varredura,
   autorização, tamanho, retenção e exclusão sem embutir storage concreto.
7. **Erro e versionamento:** registrar catálogo estável de `code`, campos
   extensíveis, compatibilidade, depreciação e schema negotiation.
8. **Lifecycle:** manter explícita a diferença entre estado genérico do job e
   estado especializado do adapter, incluindo a reconciliação editorial já
   pendente entre contratos de integração; este draft não altera essa decisão.

## 8. Critérios de aceitação do contrato

- Cada recurso sempre é autorizado por tenant e, quando aplicável, projeto;
  IDs de corpo não concedem acesso.
- Retry idêntico de sync recupera recibo original mesmo com cabeça avançada;
  envelope alterado sob mesma identidade dá conflito; conflito/CAS perdedor
  não persiste parcialmente e não propõe merge automático.
- Submit e transições de job preservam idempotência `(tenant, operation,
  key)`, fingerprint incompatível conflita, e snapshots/evento/outbox/recibo
  são documentados como uma unidade atômica do port.
- `cancel_requested` é intenção; terminal `cancelled` requer evidência da fase;
  conclusão observada vence; cancel durante `collecting` é rejeitado.
- Eventos distinguem `event_id`, sequência do agregado e cursor de feed; cursor
  expirado leva a resync explícito; duplicatas, reconexão e feed eventual são
  tratados sem garantia exactly-once nem ordem total não suportada.
- Todos os estados terminais e projeções de falha do adapter são representados;
  status snapshot não substitui replay de eventos.
- Limites, retenção, backpressure, códigos de erro, versionamento e dependências
  de banco/outbox são visíveis como contratos/decisões em aberto, sem escolha
  prematura de framework, banco, broker ou runtime cloud.
- Nenhuma rota redefine semântica de EIR v0.2, merge/rebase ou interpretação de
  operações sync.

## 9. Revisão independente e integração

Revisões independentes recebidas: desenho REST/recursos; replay e entrega de
eventos; segurança/tenancy; crítica adversarial.
Pontos convergentes: recibo original é fonte para replay de batch; sequência
por agregado e CAS, nunca timestamp, ordenam lifecycle; outbox/broker servem à
entrega eventual e podem duplicar; actor não prova identidade; cursor/feed,
retenção e recuperação precisam de suporte persistente ainda ausente. Ajustes
integrados: declarar `actor_id` derivado/validado; limitar garantia de cursor
global; explicitar escopo de acesso em reconexão; acrescentar `POST collect`
como proposta draft não suportada pelo domínio atual; tornar `/sync` condicional
a novo read model; distinguir sequências job/execução e recibo de cancelamento.
A crítica não encontrou contradição restante com a lógica de transições; apontou
que cursor global, sequência de projeto e read model não existem hoje. Divergência
de desenho registrada: collection pode ser rota de comando ou detalhe interno;
este draft propõe comando idempotente de recuperação condicionado à política do
adapter, sem afirmar suporte atual. A revisão também exigiu separar coleta
durante `collecting` da leitura de resultado somente após `succeeded`, e
definir lookup sync tenant-qualified ou autorização do projeto antes do lookup;
este é requisito de implementação alinhado ao escopo tenant + operação dos jobs,
não alegação de falha explorável atual. A revisão do coordenador verifica consistência com
`backend/ocre_core/jobs.py`, `backend/ocre_core/persistence/jobs.py`,
`backend/ocre_core/sync.py`, contratos de sync/jobs e fronteira de processo host.

## 10. Implementação local inicial

O backend local expõe somente estas seis operações, sob `/v1`:

| Método e rota | Observação |
| --- | --- |
| `POST /projects/{project_id}/sync-batches` | Recebe payloads de operação em base64; `actor_id` vem do principal injetado. |
| `GET /projects/{project_id}/sync-batches/{batch_id}` | Consulta recibo dentro do tenant do principal e após autorização de projeto. |
| `POST /projects/{project_id}/jobs` | Exige `Idempotency-Key`; pre-flight bloqueado retorna relatório sem persistir snapshot, evento, recibo ou outbox. |
| `GET /projects/{project_id}/jobs/{job_id}` | Retorna o snapshot local do job após conferir tenant e projeto. |
| `POST /projects/{project_id}/jobs/{job_id}/cancel` | Exige `Idempotency-Key`; registra apenas `cancel_requested`. |
| `GET /projects/{project_id}/jobs/{job_id}/events` | Replay paginado por tenant/job, usando cursor local. |

Esta implementação é transporte local FastAPI sobre SQLite e outbox fake. O
principal e a política de autorização são dependências obrigatórias ao montar o
app. O transporte ignora campos extras de identidade no corpo e não usa headers
de tenant como autoridade. Recursos ausentes e não autorizados retornam o mesmo
`404`. O validador atual mantém submit bloqueado enquanto `approval_fingerprint_unavailable`
e os demais requisitos do OCRE-AF/1 não puderem ser comprovados. Um fake de
prontidão existe somente nos testes para exercitar o caminho atômico de
persistência; não representa prontidão real. A API não despacha broker nem
executa solver; qualquer despacho do outbox é manual e externo à transação.

O backend fixa `fastapi==0.162.0` para tornar reproduzíveis a instalação e os
testes deste transporte local. O pin foi confirmado em 2026-10-28 como a
release mais recente, publicada em 2026-10-23; os metadados oficiais declaram
suporte a Python `>=3.10`, incluindo Python 3.12, usado pelo ambiente do
projeto. A versão exata permanece explícita para que mudanças futuras de
dependências transitivas não alterem silenciosamente este slice. Ver
[metadados do FastAPI no PyPI](https://pypi.org/project/fastapi/0.162.0/) e
[notas oficiais de release](https://fastapi.tiangolo.com/release-notes/).

Projetos e sua associação continuam dependendo da política de autorização
injetada, pois esta fatia não cria nem lista projetos. Upload/download,
artifacts/results, IAM/token, WebSocket, cloud, worker e solver permanecem fora
desta implementação.
