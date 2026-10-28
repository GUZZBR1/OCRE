# Contrato inicial de runtime de adapters e workers

**Estado:** fronteira implementável inicial; sem integração de solver, serviço de cloud, fila, banco ou credenciais. O host de processo abaixo é uma primitiva local de invocação e não é uma sandbox.

## 1. Fronteiras

O Core Cloud continua autoridade de validação EIR/pré-flight e coordena execuções. O Scientific Worker recebe um bundle imutável preparado e executa o adapter/motor habilitado. O produto não exige instalação de motores científicos no cliente. As estruturas Python em `backend/ocre_core/integrations/` definem capacidades e portas; transporte, persistência, storage e autenticação permanecem externos.

`CapabilityManifest` expõe identidade/versões do adapter e software, hash opcional do binário, modalidade, análises e formatos, versões de schema, requisitos de recursos/plataforma, status de licença, maturidade, limitações e evidência. Um manifesto experimental não significa integração suportada. O host genérico não declara capacidade científica: cada adapter fornece seu manifesto.

## 2. Contrato síncrono e contrato persistido

`Adapter` oferece `describe_capabilities`, `validate`, `prepare`, `submit`, `get_status`, `cancel`, `collect` e `cleanup`.

- `validate(request, immutable_input_manifest)` e `prepare(request, immutable_revision)` são chamadas síncronas de pré-flight. Devolvem relatório tipado; não criam `ExecutionRef`, estado persistido ou evento. Falha em validação encerra antes de preparação; falha em preparação encerra antes de submissão.
- `submit(PreparedBundle, idempotency_key)` é a fronteira de criação/recuperação de execução. O bundle deve ser imutável e seu hash conferido. A execução especializada começa em `queued`.
- `get_status` lê estado/eventos persistidos; polling não cria transições. `collect` só ocorre após término do processo e entrada em `collecting`. Em execução falha/cancelada, artefatos parciais são diagnósticos, não resultados válidos.
- `cancel` é idempotente. `queued` pode ser cancelado antes de iniciar; `running` aceita `cancel_requested`, aguarda o processo/grupo encerrar e termina `cancelled`. Durante `collecting`, retorna `cancellation_unsupported_in_phase`; estado terminal não é reescrito.
- `cleanup` é idempotente e só remove temporários depois de coleta ou retenção diagnóstica registrada.

`ExecutionPort` é o ponto de integração com jobs: `create_or_get` deve garantir idempotência por chave e hash; `get_status` lê a execução; `request_cancel` implementa corrida/terminalidade de cancelamento. A implementação persistente deve escrever snapshot e evento atomicamente, ordenar eventos e recuperar após reinício. Essas propriedades não são fornecidas pelo host de processo em memória.

## 3. Vocabulário especializado e reconciliação editorial pendente

Para o primeiro adapter batch, `ADAPTER_CONTRACT.md` é a especialização: `queued → running → collecting → succeeded`, falhas `execution_failed`/`collection_failed`, e `cancel_requested` apenas em `queued`/`running`. `INTEGRATION_ARCHITECTURE.md` também descreve o ciclo genérico de plataforma `created → validated → queued → preparing → running → collecting → succeeded|failed|cancelled`. Os dois vocabulários pertencem a camadas distintas e não devem ser renomeados silenciosamente.

O mapeamento em nível de domínio já está definido em `jobs.py` e `JOB_PERSISTENCE_CONTRACT.md`: `execution_failed` e `collection_failed` projetam para `failed`, os demais estados preservam o nome, `cancel_requested` tem evidência/origem persistida, e `preparing` pertence ao job da plataforma antes do `submit`. Permanecem pendentes nesta frente a integração durável do `ExecutionPort` com jobs, sua implementação persistente e a coordenação de eventos/idempotência/recovery no runtime. A diferença editorial entre o ciclo genérico descrito em `INTEGRATION_ARCHITECTURE.md` e o especializado em `ADAPTER_CONTRACT.md` segue registrada no contrato de persistência; integrations não altera esses documentos nem `jobs.py`.

## 4. Host de processo batch inicial

`ProcessBatchHost` executa apenas configuração confiável do adapter, nunca um comando livre vindo do request:

- `argv` é uma sequência de argumentos literal, sem shell; executável configurado deve ser caminho absoluto.
- Cada chamada recebe diretório novo sob o workspace do worker.
- O chamador escolhe explicitamente ambiente `clean` ou `inherit_allowlist`; ambiente limpo é o padrão seguro. Variáveis configuradas são explícitas.
- Timeout usa relógio monotônico; em POSIX o host termina o grupo da sessão com `SIGTERM`, aguarda graça curta e usa `SIGKILL` se necessário, então espera/recolhe o processo direto.
- `stdout` e `stderr` são drenados simultaneamente e limitados independentemente por stream; truncamento é informado.
- `verify_artifact` rejeita caminho absoluto, `..`, componentes symlink, arquivo ausente/não regular e hash divergente. Deve ser aplicado ao snapshot staged imediatamente antes do launch.

O host não estabelece sandbox ou imutabilidade. Sinalizar um grupo POSIX não garante conter processos que escapem desse grupo; um descendente escapado que mantenha os pipes abertos também pode prolongar os joins de captura. O supervisor/container externo continua responsável por execução sem privilégios, quotas de CPU/memória/armazenamento, isolamento de filesystem, filesystem de entrada read-only, rede negada por padrão, limites de processos e containment/recovery de descendentes. Uma verificação de hash não evita troca concorrente posterior (TOCTOU); a entrada deve estar em snapshot imutável do supervisor. A captura limitada reduz memória em logs, mas não limita o volume total escrito pelo processo nem consumo de disco. A API inicial executa bloqueando até término/timeout; cancelamento vivo, persistência de PID, restart recovery, retries e coleta/hash de outputs precisam do supervisor/adapter persistente. Não matar PID recuperado de estado antigo.

Arquivos e binários são não confiáveis. Adaptadores devem validar esquema e hashes, confinar toda entrada/saída, tratar logs/outputs como dados hostis e evitar publicar saída parcial como sucesso. Credenciais não devem ser herdadas implicitamente nem incluídas em logs. O host atual não invoca nem instala Gmsh, CalculiX, OCCT ou solver real.

## 5. Testes e evidência desta rodada

Os testes em `backend/tests/integrations/` usam processos Python curtos como fakes: argv literal, cwd distinto por execução, política de ambiente, timeout, saída não-zero, limite de captura e caminho/hash/symlink de artefatos. Não constituem evidência de sandbox, isolamento cloud, segurança de solver, idempotência durável, corrida de cancelamento, persistência, coleta real ou conformidade científica.

## 6. Próximas dependências

1. Conectar `ExecutionPort` à implementação persistente da frente jobs, preservando o mapeamento de domínio já definido e cobrindo idempotência, sequência, cancelamento concorrente e retomada.
2. Um adapter concreto define schemas de request/result, validação e preparação, política de artefatos obrigatórios e evidência de licença/versão.
3. O supervisor Scientific Worker fornece containment e quotas verificáveis antes de executar inputs não confiáveis em ambiente de produção.
4. A coleta valida arquivos confinados, hashes, manifesto e distinção entre resultado válido e diagnóstico parcial.
