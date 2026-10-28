# Núcleo inicial do backend OCRE

Este backend contém uma implementação local em desenvolvimento do Core: domínio
de jobs e sync, validação EIR, serviços de aplicação, persistência SQLite de
prova e transporte FastAPI local. Não representa um Core Cloud implantado nem
promete compatibilidade com infraestrutura de produção.

## Implementado localmente

- `ocre_core.validation.validator`: parsing JSON estrito, validação estrutural
  dos schemas EIR v0.2 sob Draft 2020-12, verificações semânticas do perfil
  axial, referências/manifesto de grupos e verificação confinada de URI/hash
  dos artefatos quando há raiz de bundle. Os contratos e schemas EIR v0.2
  permanecem congelados.
- `ocre_core.application.ValidatorPreflight`: chama o validador antes de
  submeter jobs. A implementação atual mantém `execution_readiness: blocked`
  porque a implementação/entradas de aprovação do fingerprint EIR `OCRE-AF/1`
  ainda não estão disponíveis no fluxo; não libera execução.
- `ocre_core.persistence.sqlite.SQLiteJobStore` e
  `ocre_core.persistence.sync.SQLiteSyncStore`: prova local de transações,
  retomada e sync concorrente em SQLite. `FakeOutboxDispatcher` e
  `FakeMessageConsumer` são fakes locais para demonstrar entrega pelo menos uma
  vez e deduplicação. Não são integração com PostgreSQL, cloud, broker ou
  workers reais.
- `ocre_core.api.create_app`: API FastAPI local implementada e testada, com
  rotas para sync, submissão/status/cancelamento de jobs e replay de eventos.
  A suíte backend passou com 73 testes e 16 subtests. A operação cloud continua
  pendente; não há IAM real, broker, worker ou cloud, e a API exige dependência
  de identidade injetada.

`backend/requirements.txt` declara `jsonschema==4.26.0`. Essa é a versão
declarada, não uma afirmação sobre o ambiente instalado. No ambiente Python 3
consultado para esta atualização, a versão instalada é `4.10.3`.

## Limites e próxima sequência

- Implementar OCRE-AF/1 no fluxo de aprovação e completar as entradas exigidas
  para que o pré-flight possa liberar execução.
- Completar capacidades autoritativas de adapters, cobertura física e
  proveniência CAD/geometria/malha; os checks do perfil axial atual não
  constituem validação física geral.
- Evoluir a API local implementada/testada para operação Core Cloud, com
  autenticação/autorização reais e implantação; integrar banco de produção,
  broker e Scientific Workers reais. A prova SQLite/outbox/sync permanece
  estritamente local.
- Implementar e testar a migração explícita EIR v0.1→v0.2.

No produto, a arquitetura é Core Cloud + Scientific Workers. Motores científicos
pesados executam nos workers; o usuário não precisa instalar esses motores no
cliente. Esta implementação local ainda não faz despacho real para workers.
