# OCRE

**OCRE** é uma plataforma modular de engenharia em desenvolvimento para integrar CAD paramétrico, simulação científica, robótica, eletrônica e sistemas industriais em uma experiência híbrida local/cloud. O cliente será leve; operações CAD complexas e motores científicos serão executados em infraestrutura cloud.

O repositório contém contratos de arquitetura, um Core local de prova e um benchmark estrutural Gmsh/CalculiX executado em geometria primitiva. Isso não equivale a produto cloud, integração CAD importado ou validação científica geral. Tecnologias seguem candidatas até que compatibilidade, licença e comportamento sejam verificados.

## Princípios

- A plataforma básica deve funcionar sem inteligência artificial.
- A IA será opcional e auxiliará criação, planejamento e automação; operações e validações serão executadas por componentes determinísticos.
- O cliente local concentra interface, visualização 3D, edição CAD interativa leve, pré-visualizações, cache, histórico e sincronização. OpenCascade.js/WebAssembly poderá executar operações geométricas leves quando adequado.
- O Core Cloud mantém projetos e revisões canônicas, valida EIR/pré-flight, armazena artefatos e orquestra execuções.
- Scientific Workers isolados provisionam OCCT completo para operações complexas e motores como Gmsh, CalculiX, OpenFOAM e Isaac Sim/Newton, conforme capacidade, versão e licença.
- O usuário não precisa instalar nem abrir motores CAD ou científicos para usar o fluxo padrão. Execução local avançada poderá ser uma opção futura, nunca um requisito. O harness local atual é somente experimento de desenvolvimento.
- O cliente mostra previews imediatamente e continua responsivo durante jobs remotos; resultados locais pendentes só se tornam parte da revisão canônica após confirmação cloud.
- Motores existentes serão integrados por adaptadores com contratos explícitos, sem recriar seus solvers.
- Projetos, entradas, versões de ferramentas e resultados precisam ser rastreáveis e reproduzíveis.

## Arquitetura em uma linha

**Cliente local → Engineering Core → orquestrador de integrações e simulações → adaptadores → motores científicos → resultados versionados → interface.**

O Engineering Core coordena projetos, representação paramétrica, geometria, montagens, materiais, versões, execução, resultados, unidades e proveniência. O formato interno provisoriamente chamado **Engineering Intermediate Representation (EIR)** deverá relacionar esses dados sem substituir formatos externos como STEP, USD, URDF ou FMI/FMU.

## Primeiro marco proposto

Demonstrar um fluxo reproduzível de **CAD paramétrico leve no cliente → sincronização → validação EIR/pré-flight no Core Cloud → geração de malha com Gmsh e análise CalculiX em Scientific Workers → validação → visualização de resultados no cliente → edição e nova execução**. Gmsh e CalculiX são candidatos para esse primeiro pipeline; o fluxo de produto ainda não está implementado.

A prova batch local documentada é experimental e não define a colocação de produção. No produto, a validação/pré-flight autoritativos rodam no Core Cloud e os motores pesados em Scientific Workers.

Uma etapa multidisciplinar posterior poderá transferir cargas dinâmicas de robótica para análise estrutural: **CAD → Isaac Sim/Newton → cargas → CalculiX → tensões associadas ao modelo**. A compatibilidade entre versões e o papel de Newton no Isaac Sim precisam ser verificados antes de assumir essa integração.

## Tecnologias candidatas

**Baseline inicial de linguagens decidido:** TypeScript (entregue como JavaScript no browser) para o cliente, com HTML/CSS para marcação e estilo; Python para o Core Cloud e adaptadores de controle/orquestração; SQL para persistência relacional quando PostgreSQL for implementado. Isso é uma decisão de arquitetura, não evidência de implementação ou prontidão para produção. Go não será adicionado sem requisito operacional ou gargalo medido; código C++ próprio fica fora do baseline inicial, sujeito a uma prova STEP/OCCT que pode justificar um adaptador isolado.

Continuam como opções de tecnologia/integração: Three.js, OpenCascade.js/WASM, OCCT, Gmsh, CalculiX, Isaac Sim, Newton, MuJoCo, Gazebo/ROS 2, OpenFOAM, SU2, Elmer, OpenModelica, ngspice, FMI/FMU, PostgreSQL, SQLite/IndexedDB/OPFS, REST/WebSocket/gRPC e infraestrutura baseada em containers. Frameworks, protocolos, armazenamento e compatibilidade dos motores ainda não estão selecionados ou validados. JSON, JSON Schema, OpenAPI e YAML são formatos de contrato/configuração, não linguagens de aplicação. Ver [ADR-007](docs/architecture/ARCHITECTURE_DECISIONS.md#adr-007--baseline-inicial-de-linguagens-por-camada).

## Estado e próximos passos

**Estado:** há provas locais em Python para validação EIR v0.2, codificação pura do fingerprint OCRE-AF/1, migração explícita v0.1→draft v0.2, jobs/SQLite/outbox fake e worker sintético; há também um sidecar experimental de proveniência `.geo`/malha e um visualizador local TypeScript/Three.js com fixture fictícia. O codificador do fingerprint ainda não está integrado ao pré-flight: a prontidão continua bloqueada, e drafts migrados nunca liberam aprovação ou execução. Os testes locais incluem falhas de persistência, entrega duplicada/replay e estados sintéticos; não demonstram operação externa.

**Pendências:** Core Cloud, IAM, banco/fila/serviços externos, workers e motores reais, importação STEP/OCCT, limiar geral de qualidade da malha e validação física geral. A prova estrutural segue restrita ao benchmark sintético documentado; medições de qualidade e resultados numéricos não são critérios gerais nem demonstram validade física fora daquele caso.

Próxima ordem: integrar OCRE-AF/1 ao contexto de aprovação sem alterar o contrato congelado e mantendo o bloqueio até todos os insumos/gates existirem; ampliar e validar falhas/replay no harness local; qualificar proveniência CAD importado e critérios de qualidade para um uso definido; depois planejar infraestrutura cloud e engines reais. O visualizador continua isolado na fixture local.

## Documentação

- [Contexto e memória do projeto](docs/PROJECT_CONTEXT.md): arquitetura, integrações, roadmap, decisões e pendências.
- [Arquitetura de integração](docs/architecture/INTEGRATION_ARCHITECTURE.md): viabilidade, tipos de adaptadores, contratos, limites e fases.
- [Verificação e validação estrutural](docs/architecture/STRUCTURAL_VALIDATION_ARCHITECTURE.md): gates e evidências para a prova Gmsh/CalculiX.
- [Pacote inicial das frentes 1–5](docs/architecture/STARTING_PACKAGE.md): caso de referência, EIR, contrato do adaptador, decisões e sequência de validação.
- [Schemas EIR](schemas/eir/README.md): schemas v0.1 experimentais e v0.2 congelados; relações semânticas e prontidão dependem do validador da aplicação.
- [Arquitetura de validação do EIR](docs/architecture/EIR_VALIDATION_ARCHITECTURE.md): pipeline determinístico, relatórios, segurança de bundle, readiness e decisões ainda abertas.
- [Contrato EIR v0.2](docs/architecture/EIR_V02_CONTRACT.md): contrato congelado, fingerprint `OCRE-AF/1`, migração explícita e três schemas travados por digest; a validação semântica local existe, mas a prontidão permanece bloqueada enquanto OCRE-AF/1 não estiver ligado.
- [Regras do repositório](AGENTS.md): instruções de trabalho específicas do projeto.
