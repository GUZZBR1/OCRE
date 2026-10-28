# Contrato de sincronização de revisões de projeto

**Estado:** primitiva de domínio local com adaptador SQLite de prova para persistência/transação. SQLite não implica compatibilidade PostgreSQL/cloud/transporte. Este documento não altera o EIR v0.2 congelado.

## Envelope de batch

Cada tentativa contém `project_id`, `batch_id`, `actor_id`,
`base_revision_id` (que pode ser `null` para a base inicial) e uma sequência
ordenada de operações. Cada operação tem `operation_id` e `payload` em bytes
opacos. O protocolo não interpreta ator como prova de identidade/autorização,
nem define tipos, schema ou semântica CAD para as operações. A ordem da
sequência e os bytes fazem parte da igualdade exata do envelope.

Revisões e IDs são tratados como identificadores opacos. O protocolo não
calcula hash de revisão, não canonicaliza payload, não define cursor de eventos
e não converte bytes de operação em snapshots. Snapshot e revisão aceitos
continuam imutáveis.

## Decisão e confirmação

`evaluate_sync_batch` classifica a solicitação:

- sem recibo anterior e com `base_revision_id == current_head_revision_id`,
  retorna `SyncReady`: intenção para o adaptador tentar o append;
- base divergente retorna `SyncConflict`, com base esperada e cabeça observada;
- recibo para a mesma chave e envelope idêntico retorna `SyncReplay` com o
  recibo original;
- mesma chave com qualquer diferença no envelope retorna
  `SyncBatchIdentityConflict`.

A chave de idempotência é `(project_id, batch_id)`. A igualdade do envelope é
estrutural e exata; não depende de hash ou serialização canônica. A busca do
recibo deve preceder a rejeição por base obsoleta, para que uma confirmação
perdida possa ser recuperada mesmo depois de a cabeça avançar. Um replay não
cria revisão nem duplicação no log.

`SyncReady` não significa commit nem confirmação. Entre a observação da cabeça
e a gravação, outra sincronização pode vencer. O port de persistência deve
implementar em uma única transação/unidade atômica: unicidade/reserva da chave
de idempotência, verificação do envelope repetido, compare-and-swap da cabeça
contra `base_revision_id`, append do log, gravação do snapshot/revisão imutável,
avanço da cabeça e recibo de confirmação. Se o CAS falhar, não pode haver
gravação parcial; o perdedor recebe conflito explícito. Sem esse port, o código
de domínio não oferece atomicidade, durabilidade ou idempotência distribuída.

## Retry, replay e conflito

- Timeout ou perda de confirmação: repetir exatamente o mesmo batch. Se houve
  commit, obter o recibo original; se a mesma chave vier com envelope diferente,
  reportar conflito de identidade.
- Conflito de base: manter o batch e suas operações locais pendentes, sem
  descartá-las, mesclá-las, rebaseá-las ou sobrescrever a cabeça. Repetir o
  mesmo batch/base pode retornar o mesmo conflito enquanto a cabeça não mudar.
- Uma tentativa alterada exige decisão explícita acima desta primitiva e uma
  nova identidade de batch; este contrato não determina como conciliar conteúdo.
- Requisições concorrentes do mesmo batch devem resultar em no máximo um
  append; a outra deve aguardar/recuperar o recibo ou receber estado transitório
  definido pelo adaptador. Batches distintos na mesma base competem pelo CAS:
  no máximo um avança a cabeça e os demais recebem conflito sem escrita parcial.

## Limites explícitos

Não há storage, cloud, broker, API, cursor incremental, retenção, semântica de
operação CAD, merge/rebase, resolução automática, geração de ID de revisão ou
hash/canonicalização de revisão nesta entrega. A comparação local é consultiva;
somente o port pode confirmar atomicamente uma revisão. EIR v0.2 e seus schemas
permanecem inalterados.

## Prova local SQLite

`SQLiteSyncStore` executa lookup do recibo antes da cabeça e realiza comparação
estrutural do envelope, CAS, append da revisão/snapshot e recibo sob uma única
transação local. `tenant_id` é namespace obrigatório do adaptador e não integra
o envelope nem a identidade de batch descrita acima; a chave persistida é
`(tenant_id, project_id, batch_id)`. Snapshots e envelopes usam JSON versionado
com tipos explicitamente allowlisted, sem desserialização pickle. A prova não
define cursor incremental nem altera o protocolo ou sua semântica.
