# Contrato mínimo de adaptador — execução batch

**Estado:** contrato conceitual; sua implementação e conformidade seguem pendentes. O harness experimental executou uma combinação local Gmsh 4.15.2→C3D10→deck CalculiX separado→CCX 2.23 para a barra sintética, com grupos, métricas numéricas e proveniência registrados em `experiments/fea-reference/runs/solver_runs_manifest.json`. Essa execução não equivale a conformidade com este contrato, schema EIR, importação CAD ou suporte geral de adaptador.

Este documento especializa o contrato comum de [INTEGRATION_ARCHITECTURE.md](INTEGRATION_ARCHITECTURE.md) para um job batch reproduzível. Sessões e streams continuam com ciclo próprio.

## Interface lógica

```text
describe_capabilities() -> CapabilityManifest
validate(request, immutable_input_manifest) -> ValidationReport
prepare(request, immutable_input_manifest) -> PreparedBundle + ConversionReport
submit(PreparedBundle, idempotency_key) -> ExecutionRef
get_status(ExecutionRef) -> ExecutionStatus + Progress + Events
cancel(ExecutionRef) -> CancellationReport
collect(ExecutionRef) -> ResultManifest + NativeArtifacts + Logs
cleanup(ExecutionRef) -> CleanupReport
```

`validate` e `prepare` são preflight síncrono, sem `ExecutionRef` ou eventos. Validação falha sem preparar; preparação falha sem submeter. `submit` aceita somente bundle preparado e imutável cujo manifesto/hash foi conferido, cria ou recupera uma execução e seu `execution_id`; estados/eventos começam então em `queued`. `collect` começa após o processo terminar e a execução entrar em `collecting`; sua conclusão define `succeeded` ou `collection_failed`. Em falha/cancelamento, saídas parciais são identificadas como diagnósticas. `cleanup` só remove temporários após coleta ou retenção diagnóstica registrada. Chamadas retornam relatórios tipados ou erros estruturados `{code, message, retryable, details}`; mensagens não substituem códigos estáveis. `idempotency_key` repetida com o mesmo hash de entrada retorna a execução existente; a mesma chave com entrada diferente falha. `cancel` e `cleanup` devem ser seguros para chamadas repetidas. Não repetir automaticamente falha de validação, licença, convergência ou erro determinístico do solver.

## Colocação no produto OCRE

O produto segue a arquitetura híbrida aprovada. O cliente leve concentra UI, visualização Three.js, interação CAD, operações locais leves, cache e sincronização. O Core Cloud é autoridade para validação semântica EIR e pré-flight, persistência/orquestração de jobs, armazenamento de artefatos e pós-processamento. Os Scientific Workers executam OCCT complexo/importação CAD, Gmsh, CalculiX, OpenFOAM, Isaac Sim/Newton e outros motores científicos habilitados.

`validate` e `prepare` autoritativos ocorrem no Core Cloud antes do despacho. O pacote de entrada é imutável, identificado por hashes e enviado ao worker; o cliente não precisa instalar solver, Gmsh, OCCT avançado ou outro motor pesado. Checagens locais futuras podem antecipar erros de forma offline, mas não substituem o pré-flight cloud nem liberam execução. Execução local avançada pode ser adicionada como opção explícita no futuro, sem alterar o contrato do job nem tornar motores obrigatórios no cliente.

O uso local de Gmsh/CalculiX em `experiments/fea-reference/` é somente evidência de desenvolvimento. Não representa modo de execução local suportado pelo produto nem dependência de instalação do usuário.

## Manifesto de capacidade

Campos obrigatórios:

```json
{
  "adapter_id": "calculix.batch",
  "adapter_version": "0.1.0",
  "contract_version": "0.1.0",
  "software": {"name": "CalculiX CrunchiX", "proposed_target_version": "2.23", "binary_sha256": "<sha256-observado-na-execucao>"},
  "mode": "batch",
  "supported_analyses": ["static_linear"],
  "input_formats": ["calculix_inp"],
  "result_formats": ["frd", "dat", "sta"],
  "platforms": ["linux-x86_64"],
  "units_policy": "explicit_si_or_declared_consistent_units",
  "license_status": "review_required",
  "maturity": "experimental",
  "limitations": ["only the pinned test case is evaluated"]
}
```

O manifesto de capacidade registra a evidência observada para a combinação exata e sua maturidade; exemplos e fixtures do harness não demonstram conformidade ao contrato. A execução da barra fornece evidência local da combinação identificada, sem validar a implementação de adaptadores descrita aqui. Binário/hash só se preenche após identificar o executável usado. Não extrapolar capacidade do solver para capacidade geral do adaptador OCRE.

## Fronteira entre adaptadores

- **Gmsh adapter:** recebe geometria e seleções semânticas; gera a malha e manifesto de grupos, registra tipos/conectividade/contagens e valida a resolução das entidades geométricas para entidades da malha. Não cria cards de análise CalculiX nem afirma que o solver aceita a exportação.
- **CalculiX adapter:** consome malha e manifesto já preparados; valida o mapeamento que usa, gera/confere o deck de análise, executa o binário, coleta outputs nativos e extrai resultados. Não redefine a seleção geométrica de origem.
- **Orquestração batch:** liga artefatos imutáveis e relatórios entre adapters, confere hashes/URI confinada e mantém identidade de caso/revisão. Cada adapter declara separadamente a evidência que verificou.

Essa separação é o contrato proposto. O harness executou a integração sequencial para um caso sintético, mas não implementa nem valida a interoperabilidade entre adaptadores independentes segundo este contrato.

## Pacote de entrada

O `InputBundle` é imutável e contém:

- `project_id`, `revision_id`, `case_id` e hash da revisão/caso;
- manifesto de artefatos com URI relativa, papel, formato/versão e SHA-256;
- arquivo de solver gerado e input original de geometria/malha;
- sistema de unidades, referencial, grupos, materiais, cargas e restrições;
- versões/hash do adaptador, Gmsh, solver e ambiente de execução;
- relatório de preparação: entidades/grupos mapeados, conversões, warnings e dados descartados.
- manifesto de grupos da malha: ID semântico → entidade geométrica e seletor/coordenada de origem → entidade/dimensão/tags Gmsh quando aplicável → entidade da malha e vínculo ao hash/revisão da malha → nome/tipo do conjunto de destino, contagens esperada/observada e hash do artefato. Para pinos, registrar a resolução da seleção até os nós efetivos e conferência de coordenadas/tolerância. IDs `PIN_A`/`PIN_B` não são nomes de node set por si só; tags numéricas não são identidade persistente.

URIs locais são relativas à raiz do bundle; rejeitar `..`, caminhos absolutos e qualquer resolução que escape a raiz, inclusive por symlink. Manifesto e exemplos aqui são propostas conceituais, sem schema executável ou validação automática correspondente.

`validate` falha antes de executar em caso de arquivo ausente/hash divergente, grupo vazio/desconhecido, tipo de análise não suportado, unidade implícita, combinação de versão não permitida ou material/condição inválida.

## Estado e eventos

O ciclo persistido começa quando `submit` retorna `ExecutionRef`:

```text
queued → running → collecting → succeeded
   ↘ execution_failed       ↘ collection_failed
queued/running → cancel_requested → cancelled
```

Falhas de `validate` e `prepare` são relatórios síncronos e não estados de execução. O processo termina antes da coleta; `collecting` representa validação e empacotamento dos resultados. Cada transição após submissão tem timestamp monotônico e evento com `execution_id`, sequência estritamente crescente, etapa, progresso opcional e código de falha. `execution_failed`, `collection_failed`, `succeeded` e `cancelled` são terminais. `succeeded` só após processo terminar com código zero, arquivos obrigatórios presentes e manifesto de resultados íntegro. Saídas parciais de estados falhos/cancelados são diagnósticas, não resultados válidos. Persistência após reinício, recuperação de processo interrompido e ordenação concorrente de eventos ficam pendentes de implementação.

`cancel` é idempotente. Em `queued`, impede início do processo e conclui como `cancelled`. Em `running`, solicita interrupção, aguarda o processo terminar e só então marca `cancelled`; se o processo concluir primeiro, retorna o estado terminal efetivo e não o reescreve. Durante `collecting`, retorna `cancellation_unsupported_in_phase` até haver política segura. Chamadas sobre estado terminal devolvem o estado existente. Polling não cria transição.

## Pacote de resultado

O `ResultManifest` lista outputs nativos, hashes, tamanho, tipo, variáveis/unidades quando aplicável, solver/adapter, hash de entrada, logs, status e relatórios de conversão. Para a prova estrutural, declarar as fontes/unidades/referencial de deslocamento, tensão e reações axiais requeridos pelas métricas canônicas. A extração experimental desses campos foi exercitada nos pontos de integração do `.dat`, mas a validação do schema interoperável deste `ResultManifest` continua pendente; não prometer campo normalizado antes de validar a implementação contratual.

## Isolamento e ciclo de vida

Cada execução usa diretório próprio e entrada somente leitura. Limites de CPU/memória/tempo/armazenamento e execução sem privilégios são requisitos a registrar, não capacidades comprovadas pelo harness atual. `cleanup` remove temporários conforme política e mantém artefatos listados no manifesto. Credenciais ou caminhos absolutos da máquina não entram no EIR nem no resultado publicado.

## Critério de aceitação do primeiro adaptador

Para o caso em [STRUCTURAL_REFERENCE_CASE.md](STRUCTURAL_REFERENCE_CASE.md), o harness experimental já executou exportação de malha C3D10, montagem de decks de análise separados, execução nas versões identificadas e comparação das métricas com as definições e metas daquela fonte. A aceitação deste contrato ainda exige validação de pacote/schema, comportamento de estado/cancelamento, coleta interoperável e testes de falha. A evidência numérica vale somente para o caso sintético e ambiente registrados; qualquer novo tipo de elemento, análise ou solver exige extensão de capacidade e evidência.
