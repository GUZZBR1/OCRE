# Pacote inicial de trabalho: frentes 1–5

**Estado:** pacote atualizado em 28/10/2026. Além da prova sintética Gmsh→CalculiX em três malhas, há agora um codificador local para OCRE-AF/1, um migrador explícito v0.1→rascunho v0.2, um worker sintético local para exercitar o ciclo de jobs, um sidecar experimental de proveniência `.geo`→malha com métrica de qualidade sem limiar de aceitação, e um visualizador local TypeScript/Three.js com fixture fictícia. O fingerprint ainda não está integrado aos insumos do pré-flight: a API/Core continua retornando `blocked`, e rascunhos migrados nunca liberam aprovação ou execução. Cloud, IAM, broker, serviços externos, motores reais, importação STEP/OCCT e validação geral do OCRE seguem pendentes.

Este documento transforma as frentes aprovadas em entregáveis ordenados. A arquitetura geral está em [INTEGRATION_ARCHITECTURE.md](INTEGRATION_ARCHITECTURE.md); os documentos abaixo descem para os contratos e a prova mínimos.

## Frentes e entregáveis

| Frente | Entregável revisável | Estado inicial | Dependência |
| --- | --- | --- | --- |
| 1. Caso de referência estrutural | [STRUCTURAL_REFERENCE_CASE.md](STRUCTURAL_REFERENCE_CASE.md): parâmetros e métricas analíticas; [STRUCTURAL_VALIDATION_ARCHITECTURE.md](STRUCTURAL_VALIDATION_ARCHITECTURE.md): gates e evidências V&V | Caso executado em três refinamentos C3D10; critérios analíticos e convergência passaram para a geometria `.geo` testada | Expandir evidência para CAD importado e fechar critérios de qualidade/proveniência |
| 2. EIR mínimo | [EIR_MINIMUM.md](EIR_MINIMUM.md): EIR v0.1 legado; [EIR_V02_CONTRACT.md](EIR_V02_CONTRACT.md): contrato e schemas v0.2 congelados; [EIR_VALIDATION_ARCHITECTURE.md](EIR_VALIDATION_ARCHITECTURE.md): pré-flight no Core | Validação local e codificador puro OCRE-AF/1 existem; o pré-flight ainda não recebe o conjunto completo de entradas do fingerprint e permanece bloqueado. Migrador explícito produz draft v0.2 e relatório com pendências | Integrar entradas verificadas do fingerprint e compartilhar vetores interoperáveis; completar gates antes de qualquer liberação de execução |
| 3. Contrato de adaptador | [ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md): manifestos, entrada, estados, resultado e colocação cloud | Contrato batch proposto; worker sintético local exercita estados de job sem iniciar executor nem chamar solver/serviço externo | Implementar adaptadores e workers reais no ambiente futuro, sem exigir motores no cliente |
| 4. Prova técnica | Rodar geometria → Gmsh → CalculiX; preservar inputs, logs e resultados | Gmsh 4.15.2 gerou malhas C3D10 a partir de uma primitiva `.geo`; o harness acrescentou decks CalculiX separados e CCX 2.23 executou três refinamentos. Critérios analíticos e convergência passaram neste caso. Importação CAD e limiar de qualidade geral seguem pendentes | Validar CAD importado/proveniência e demais gates antes de ampliar o escopo |
| 5. Decisões arquiteturais | [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md): decisões iniciais e questões em aberto | Direções propostas registradas | Evidência da prova técnica fecha algumas questões |

## Execução e próximos passos

**Concluído para o benchmark local:** versões Gmsh 4.15.2 e CalculiX CCX 2.23 identificadas; três decks C3D10 gerados e executados; entradas, logs, saídas nativas, hashes e métricas preservados; critérios analíticos e comparação entre as duas malhas mais finas aprovados. A geometria testada foi uma primitiva `.geo`, não CAD importado.

**Implementado localmente desde a sequência inicial:**

- Codificador determinístico de framing/fingerprint OCRE-AF/1 e migrador explícito v0.1→v0.2 com saída `draft`, preservação dos bytes de origem e relatório de resoluções/pendências. A migração não aprova nem habilita execução.
- Worker sintético com estados de sucesso/falha/cancelamento rotulados como simulação; testes locais cobrem lifecycle sintético, idempotência, replay, entrega pelo menos uma vez e deduplicação local. Não há despacho real.
- Sidecar que liga definições `.geo` sintéticas, malhas e manifestos existentes por hashes e registra mean ratio sem gate de qualidade.
- Visualizador TypeScript/Three.js que carrega apenas fixture fictícia embutida localmente; não usa backend, solver ou resultado científico.

**Pendente:**

1. Ligar ao pré-flight o conjunto completo de entradas verificadas para OCRE-AF/1, produzir/compartilhar vetores interoperáveis e manter bloqueio até todas as entradas exigidas estarem presentes.
2. Ampliar testes locais de falha/replay, timeout/cancelamento e recuperação; a evidência atual não demonstra recuperação com worker real.
3. Definir um limiar de qualidade apropriado ao uso pretendido; o sidecar registra mean ratio medido, mas não define gate nem limite universal.
4. Validar importação CAD→Gmsh, identidade e proveniência desde arquivo CAD; a origem demonstrada segue sendo `.geo` sintético.
5. Ligar visualizador a resultados reais depois de definir formatos, correspondência de nós/elementos e referenciais; a fixture atual é fictícia e local.
6. Evoluir a API FastAPI local para operação Core Cloud, com IAM real, banco de produção, fila/broker, serviços externos e Scientific Workers reais; SQLite/outbox/sync são provas locais.
7. Completar capacidades de adapter e validação física representativa para um uso de engenharia definido; os checks atuais não validam o OCRE em geral.

## Chats e subagentes

Chats e subagentes coordenados e estado atual:

- `researcher`: caso de referência e fontes oficiais — concluído.
- `executor`: baseline estruturado C3D8 — concluído; decks de referência preparados separadamente, sem execução C3D8 nesta prova.
- `verifier`: revisão de mecânica, EIR e contrato — concluído; coordenadas dos pinos e extração determinística agora especificadas.
- `researcher`: exportação Gmsh INP e cards CalculiX — concluído; identificou limites documentais do mapeamento.
- Chat de execução da prova: Gmsh/C3D10 e decks separados executados com CCX 2.23; três malhas, saídas nativas e métricas preservadas em `experiments/fea-reference/runs/`.
- Auditoria independente: confirmou os hashes e métricas das tentativas selecionadas; ressalvou que a evidência é para uma primitiva `.geo`, não CAD importado, e que gates de entrada/proveniência/falhas ainda não estão completos.

Chats compartilham este branch com ownership delimitado. As frentes 2, 3 e 5 permanecem com o líder para evitar contratos divergentes. A revisão independente read-only já conferiu decks selecionados, logs, hashes e resultados concretos; os limites remanescentes estão listados abaixo.

## Lacuna explícita da frente 4

O baseline `prepare` gera decks C3D8 completos diretamente. A rota `prepare-gmsh` foi executada com Gmsh 4.15.2: exportou malhas C3D10, resolveu os pinos por coordenadas no harness, gerou decks de análise separados e executou CCX 2.23. O manifesto `experiments/fea-reference/runs/solver_runs_manifest.json` registra três execuções concluídas, hashes, resultados e critérios numéricos aprovados para o benchmark. A geometria de origem é uma primitiva `Box` OpenCASCADE definida em `.geo`, não um arquivo CAD importado; identidade/proveniência CAD→malha e sua validação seguem como lacunas. A qualidade geométrica foi medida no sidecar, sem limiar geral de aceitação. EIR v0.2 está congelado. O codificador OCRE-AF/1 existe localmente, mas seus insumos ainda não entram no pré-flight; o Core mantém execução bloqueada. O worker sintético e as provas de persistência/replay não são workers científicos nem integração de serviços. Validação física geral também permanece pendente. A arquitetura dos gates está em `STRUCTURAL_VALIDATION_ARCHITECTURE.md`.


## Condição de saída desta etapa

**Satisfeito nesta etapa:** (a) o caso sintético foi executado de ponta a ponta e comparado à referência analítica; (c) versões do toolchain e limites observados foram registrados. A prova numérica também incluiu três refinamentos e comparação entre as duas malhas mais finas.

**Ainda pendente para fechar o pacote:** ampliar a cobertura automatizada/interoperável de schemas e manifestos (já há testes locais de schemas e validador); proveniência de CAD importado; testes integrados de falha/replay; definição de limiar de qualidade geral; e decisões de escopo resolvidas ou adiadas explicitamente. Essas lacunas não invalidam os resultados numéricos do caso testado, mas impedem declarar o conjunto de gates ou o contrato implementável como concluídos.

## Evidência externa consultada

- [Manual oficial do Gmsh 4.15.2](https://gmsh.info/doc/texinfo/): grupos físicos, elementos, geração de malha e formatos.
- [Site oficial do CalculiX](https://calculix.de/): descreve o pacote e aponta versão 2.23 como disponível.
- [Visão oficial de capacidades do CalculiX 2.23](https://www.dhondt.de/ov_calcu.htm): confirma materiais elásticos isotrópicos, elementos tetraédricos de 10 nós e saídas de tensão/deslocamento; confirmar detalhes no manual da versão escolhida.
- [Manual oficial do CalculiX 2.22](https://www.dhondt.de/ccx_2.22.pdf): contém exemplos estruturais e de refinamento; é uma versão anterior e não substitui a documentação correspondente ao binário escolhido.

As referências acima fundamentam a escolha inicial, não substituem uma execução local nem uma avaliação de licença/distribuição por versão.

**Verificação local do ambiente:** Gmsh 4.15.2, CalculiX CCX 2.23 e runtime Fortran foram usados a partir de `experiments/fea-reference/.local-tools/`, sem instalação global. As execuções CCX e o probe de versão ocorreram em diretórios isolados sob `experiments/fea-reference/runs/`.
