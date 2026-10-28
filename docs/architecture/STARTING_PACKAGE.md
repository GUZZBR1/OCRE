# Pacote inicial de trabalho: frentes 1–5

**Estado:** pacote atualizado em 28/10/2026. A prova sintética Gmsh→CalculiX foi executada para três malhas e EIR v0.2 permanece congelado. Há validação estrutural/semântica local e API FastAPI local implementada/testada (73 testes backend + 16 subtests); OCRE-AF/1 indisponível mantém o pré-flight bloqueado. Operação cloud segue em desenvolvimento, sem IAM real, broker ou worker real. CAD importado e cobertura física permanecem pendentes.

Este documento transforma as frentes aprovadas em entregáveis ordenados. A arquitetura geral está em [INTEGRATION_ARCHITECTURE.md](INTEGRATION_ARCHITECTURE.md); os documentos abaixo descem para os contratos e a prova mínimos.

## Frentes e entregáveis

| Frente | Entregável revisável | Estado inicial | Dependência |
| --- | --- | --- | --- |
| 1. Caso de referência estrutural | [STRUCTURAL_REFERENCE_CASE.md](STRUCTURAL_REFERENCE_CASE.md): parâmetros e métricas analíticas; [STRUCTURAL_VALIDATION_ARCHITECTURE.md](STRUCTURAL_VALIDATION_ARCHITECTURE.md): gates e evidências V&V | Caso executado em três refinamentos C3D10; critérios analíticos e convergência passaram para a geometria `.geo` testada | Expandir evidência para CAD importado e fechar critérios de qualidade/proveniência |
| 2. EIR mínimo | [EIR_MINIMUM.md](EIR_MINIMUM.md): EIR v0.1 legado; [EIR_V02_CONTRACT.md](EIR_V02_CONTRACT.md): contrato e schemas v0.2 congelados; [EIR_VALIDATION_ARCHITECTURE.md](EIR_VALIDATION_ARCHITECTURE.md): pré-flight no Core | Contrato e schemas congelados; implementação local estrutural/semântica existe, mas OCRE-AF/1 indisponível bloqueia execução | Implementar fingerprint OCRE-AF/1 e completar gates antes de liberar execução no Core Cloud |
| 3. Contrato de adaptador | [ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md): manifestos, entrada, estados, resultado e colocação cloud | Contrato de domínio batch proposto; execução local do harness é apenas experimental | Implementar adaptadores em Scientific Workers, sem exigir motores no cliente |
| 4. Prova técnica | Rodar geometria → Gmsh → CalculiX; preservar inputs, logs e resultados | Gmsh 4.15.2 gerou malhas C3D10 a partir de uma primitiva `.geo`; o harness acrescentou decks CalculiX separados e CCX 2.23 executou três refinamentos. Critérios analíticos e convergência passaram neste caso. Importação CAD e limiar de qualidade geral seguem pendentes | Validar CAD importado/proveniência e demais gates antes de ampliar o escopo |
| 5. Decisões arquiteturais | [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md): decisões iniciais e questões em aberto | Direções propostas registradas | Evidência da prova técnica fecha algumas questões |

## Execução e próximos passos

**Concluído para o benchmark local:** versões Gmsh 4.15.2 e CalculiX CCX 2.23 identificadas; três decks C3D10 gerados e executados; entradas, logs, saídas nativas, hashes e métricas preservados; critérios analíticos e comparação entre as duas malhas mais finas aprovados. A geometria testada foi uma primitiva `.geo`, não CAD importado.

**Pendente:**

1. Ampliar fault-injection, timeout/cancelamento e recuperação de execução; replay e persistência local já têm cobertura, mas cenários de falha integrados e recuperação com worker real continuam pendentes.
2. Definir limiar de qualidade de forma e validar qualidade geral da malha para o uso pretendido.
3. Validar importação CAD→Gmsh, identidade e proveniência da geometria original até a malha.
4. Completar OCRE-AF/1 e seus vetores interoperáveis; ampliar capacidades completas do adapter e cobertura física. A validação estrutural/semântica EIR v0.2, referências, regras UCUM/perfil axial e checagem de bundle já têm implementação local; isso não equivale a serviço cloud nem libera pré-flight.
5. Ligar resultados e seleções à visualização, conferindo correspondência de nós/elementos e referencial.
6. Evoluir a API FastAPI local implementada/testada para operação Core Cloud, com IAM real, banco de produção, broker e Scientific Workers reais; SQLite/outbox/sync são provas locais.
7. Implementar migração v0.1→v0.2 e ampliar validação física representativa para um uso de engenharia definido.
8. Fechar proveniência CAD→malha e limiares de qualidade física/geométrica.

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

O baseline `prepare` gera decks C3D8 completos diretamente. A rota `prepare-gmsh` foi executada com Gmsh 4.15.2: exportou malhas C3D10, resolveu os pinos por coordenadas no harness, gerou decks de análise separados e executou CCX 2.23. O manifesto `experiments/fea-reference/runs/solver_runs_manifest.json` registra três execuções concluídas, hashes, resultados e critérios numéricos aprovados para o benchmark. A geometria de origem é uma primitiva `Box` OpenCASCADE definida em `.geo`, não um arquivo CAD importado; identidade/proveniência CAD→malha e sua validação seguem como lacunas. A qualidade geométrica foi medida, mas não há limiar geral de aceitação. EIR v0.2 está congelado. Há validador estrutural/semântico e pré-flight implementados localmente no Core, mas OCRE-AF/1 ainda indisponível bloqueia execução; falhas/replay completos e validação física também permanecem pendentes. A arquitetura dos gates está em `STRUCTURAL_VALIDATION_ARCHITECTURE.md`; motores pesados ficam nos Scientific Workers, sem exigir sua instalação pelo usuário.


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
