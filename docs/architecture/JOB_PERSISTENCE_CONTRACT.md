# Contrato de lifecycle e persistência de jobs

**Estado:** ports e adaptador SQLite local implementados para provar atomicidade e retomada após reinício. SQLite não implica compatibilidade PostgreSQL, cloud, broker ou worker.

## Vocabulários e mapeamento

O ciclo genérico de job da plataforma preserva `INTEGRATION_ARCHITECTURE.md`: `created → validated → queued → preparing → running → collecting → succeeded|failed|cancelled`. Esses estados representam o job sob autoridade do Core Cloud. `cancel_requested` é um estado intermediário explícito entre `queued|running` e o resultado do cancelamento. Falhas síncronas de `validate` e `prepare` retornam relatórios e não criam eventos de execução.

O primeiro adapter batch especializa a execução conforme `ADAPTER_CONTRACT.md`: `queued → running → collecting → succeeded|execution_failed|collection_failed`; pedido de cancelamento só parte de `queued|running` e passa por `cancel_requested`. O tipo `AdapterExecutionState` mantém os nomes especializados. O mapeamento para a projeção genérica é: `execution_failed` e `collection_failed` → `failed`; os demais estados compartilham o nome. `preparing` é responsabilidade da plataforma e não é estado de execução do adapter. A diferença entre os ciclos genérico e especializado permanece uma pendência de reconciliação editorial em `INTEGRATION_ARCHITECTURE.md`; este documento não altera o contrato de outra frente.

## Invariantes de lifecycle e corrida

- Cada evento de job tem `job_id`; evento de execução tem `execution_id`; ambos têm `event_id` único no agregado, sequência estritamente crescente e timestamp com fuso. O timestamp não arbitra corridas.
- Cada transição exige sequência esperada (CAS). Escrita concorrente obsoleta falha sem mudar snapshot/evento.
- Em `queued`, o cancelamento primeiro transita para `cancel_requested`, impede o início do worker e só então transita para `cancelled` com confirmação autoritativa de que o worker não iniciou.
- Em `running`, cancelar primeiro registra `cancel_requested`. A confirmação de cancelamento requer observação de que o processo terminou cancelado. Se o processo terminou com resultado antes do pedido ser observado, a conclusão observada vence: sucesso segue por `collecting`; falha segue a estado falho. Nunca se transforma conclusão efetiva em `cancelled`.
- A transição que entra em `cancel_requested` grava `cancellation_origin` no evento e no snapshot (`queued` ou `running`). Toda transição subsequente preserva essa origem; confirmação carrega `CancellationEvidence` vinculada ao job/execução e deve igualar a origem persistida. `WORKER_START_PREVENTED` só é válido para origem `queued`; `PROCESS_TERMINATED_BY_CANCELLATION` só para `running`. A evidência integra o evento append-only de confirmação; não é estado volátil do chamador.
- Se pedido e conclusão competirem, o coordenador/adaptador consulta evidência autoritativa de término antes de confirmar cancelamento. CAS define a ordem persistida; quem perde recarrega o estado e retorna o efetivo, sem sobrescrita. Horário de parede não prova precedência física. A porta de execução do adapter deve fornecer essa observação; ela ainda não está implementada.
- Em `collecting`, cancel retorna `cancellation_unsupported_in_phase`. Repetição para estado terminal devolve o estado atual sem nova transição. Polling não gera eventos.

## Fronteira transacional e idempotência

`JobPersistencePort.create_execution` cria submissão em `queued` como uma unidade atômica: snapshots do job e da execução, evento `ExecutionSubmitted`, recibo de idempotência e mensagem de outbox. Isso define o primeiro registro persistido após `submit`; não cria evento de execução para falha de validação/preparação. `commit_transition` compara `expected_sequence`, grava o snapshot novo, acrescenta exatamente um evento, registra o recibo/chave de idempotência quando fornecidos e grava a mensagem de outbox obrigatória. Cada evento persistido tem uma mensagem correspondente na outbox. Tudo confirma junto ou nada confirma. Nenhuma chamada de broker, worker, storage externo ou código de adapter ocorre dentro dessa transação.

A implementação valida que snapshot e evento identificam o mesmo agregado, que `event.sequence == expected_sequence + 1`, que `snapshot.sequence/state` correspondem ao destino do evento, e que `event.from_state` corresponde ao snapshot carregado. `cancellation_origin` é copiado do snapshot anterior ao sair de `cancel_requested` e só pode ser criado pela transição `queued|running → cancel_requested`; uma confirmação rejeita evidência cuja origem não corresponda ao snapshot. Criação confere IDs/job/execution e começa ambos os snapshots em `queued`. Alocação de sequência e unicidade do `event_id` são verificadas na escrita persistente.

A chave de idempotência é associada a tenant, operação, chave, impressão digital canônica da requisição e recibo original. Mesma tupla de escopo/chave e mesma impressão digital reproduzem o recibo sem novo evento/outbox; mesma tupla com impressão digital diferente é conflito. Após resultado de commit incerto, retry consulta a chave antes de submeter novo commit. A impressão digital é calculada pelo chamador a partir do conteúdo semântico da requisição; formato canônico fica por definir junto ao API.

O port permite recarregar snapshots de job e execução após restart ou perda de CAS. O dispatcher publica a outbox fora da transação. Entrega é pelo menos uma vez; `message_id` é único e estável, e consumidores deduplicam. Não há promessa de entrega exatamente uma vez. Retry de CAS recarrega snapshot/eventos e reavalia a transição; retry de chave idempotente segue as regras acima. Backoff, lease, retenção, recuperação após reinício e política de dead-letter ficam para uma implementação concreta.

Os ports são independentes de PostgreSQL, broker, framework e driver. PostgreSQL segue apenas como candidato. Não há integração funcional Core Cloud ↔ Scientific Worker nesta entrega; o solver permanece isolado no Scientific Worker e EIR v0.2/schemas permanecem inalterados.

## Prova local SQLite

O adaptador `SQLiteJobStore` implementa as unidades atômicas locais e usa `BEGIN IMMEDIATE`, constraints e CAS por sequência. Snapshots/eventos são armazenados em JSON versionado com codificação allowlisted para os dataclasses/enums suportados, timestamps, bytes e tuplas; nenhum pickle é carregado. O formato e o banco servem apenas a esta prova local e não são contrato de intercâmbio nem promessa de compatibilidade futura.

`read_events` pagina por cursor inteiro monotônico alocado no append, ordenado pelo cursor e filtrado simultaneamente por tenant e job. O cursor não usa timestamp. `FakeOutboxDispatcher` publica fora da transação e marca entrega depois do retorno do publisher; uma falha deixa a mensagem pendente para reentrega. `FakeMessageConsumer` persiste deduplicação local por `message_id`, com estados `processing`/`processed`; após parar o consumer anterior, `recover_abandoned` libera reservas interrompidas para nova entrega. Não se deve chamar a recuperação enquanto consumidores antigos ainda estão ativos. O modelo é pelo menos uma vez, sem exatamente uma vez.
