# OCRE

**OCRE** é uma plataforma modular de engenharia em desenvolvimento para integrar CAD paramétrico, simulação científica, robótica, eletrônica e sistemas industriais em uma experiência híbrida local/cloud.

O repositório contém contratos de arquitetura, um Core local de prova e um benchmark estrutural Gmsh/CalculiX executado em geometria primitiva. Isso não equivale a produto cloud, integração CAD importado ou validação científica geral. Tecnologias seguem candidatas até que compatibilidade, licença e comportamento sejam verificados.

## Princípios

- A plataforma básica deve funcionar sem inteligência artificial.
- A IA será opcional e auxiliará criação, planejamento e automação; operações e validações serão executadas por componentes determinísticos.
- O cliente local priorizará interação CAD responsiva, visualização e operações geométricas leves.
- O Core Cloud manterá revisões canônicas, validará EIR/pré-flight e coordenará projetos e jobs; Scientific Workers executarão motores pesados isolados.
- O usuário não precisará instalar Gmsh, CalculiX ou outros motores científicos para usar o produto. O harness local existente é somente experimento de desenvolvimento; execução local avançada futura será opcional.
- Motores existentes serão integrados por adaptadores com contratos explícitos, sem recriar seus solvers.
- Projetos, entradas, versões de ferramentas e resultados precisam ser rastreáveis e reproduzíveis.

## Arquitetura em uma linha

**Cliente local → Engineering Core → orquestrador de integrações e simulações → adaptadores → motores científicos → resultados versionados → interface.**

O Engineering Core coordena projetos, representação paramétrica, geometria, montagens, materiais, versões, execução, resultados, unidades e proveniência. O formato interno provisoriamente chamado **Engineering Intermediate Representation (EIR)** deverá relacionar esses dados sem substituir formatos externos como STEP, USD, URDF ou FMI/FMU.

## Primeiro marco proposto

Demonstrar um fluxo reproduzível de **CAD paramétrico → sincronização → preparação de modelo e malha → análise estrutural → validação → visualização → edição e nova execução**. Gmsh e CalculiX são candidatos para o primeiro pipeline. A sequência e o escopo exato deste marco continuam sujeitos a decisão.

No produto, a validação/pré-flight autoritativos e a execução Gmsh/CalculiX ocorrem no Core Cloud e em Scientific Workers. A prova batch local documentada não define a colocação de produção.

Uma etapa multidisciplinar posterior poderá transferir cargas dinâmicas de robótica para análise estrutural: **CAD → Isaac Sim/Newton → cargas → CalculiX → tensões associadas ao modelo**. A compatibilidade entre versões e o papel de Newton no Isaac Sim precisam ser verificados antes de assumir essa integração.

## Tecnologias candidatas

**Baseline inicial de linguagens decidido:** TypeScript (entregue como JavaScript no browser) para o cliente, com HTML/CSS para marcação e estilo; Python para o Core Cloud e adaptadores de controle/orquestração; SQL para persistência relacional quando PostgreSQL for implementado. Isso é uma decisão de arquitetura, não evidência de implementação ou prontidão para produção. Go não será adicionado sem requisito operacional ou gargalo medido; código C++ próprio fica fora do baseline inicial, sujeito a uma prova STEP/OCCT que pode justificar um adaptador isolado.

Continuam como opções de tecnologia/integração: Three.js, OpenCascade.js/WASM, OCCT, Gmsh, CalculiX, Isaac Sim, Newton, MuJoCo, Gazebo/ROS 2, OpenFOAM, SU2, Elmer, OpenModelica, ngspice, FMI/FMU, PostgreSQL, SQLite/IndexedDB/OPFS, REST/WebSocket/gRPC e infraestrutura baseada em containers. Frameworks, protocolos, armazenamento e compatibilidade dos motores ainda não estão selecionados ou validados. JSON, JSON Schema, OpenAPI e YAML são formatos de contrato/configuração, não linguagens de aplicação. Ver [ADR-007](docs/architecture/ARCHITECTURE_DECISIONS.md#adr-007--baseline-inicial-de-linguagens-por-camada).

## Estado e próximos passos

**Estado:** há implementação local em Python para validação EIR, pré-flight bloqueado, jobs, persistência/sync SQLite com outbox fake e API FastAPI; a suíte backend passou com 73 testes e 16 subtests. A prova Gmsh/CalculiX cobre uma geometria `.geo` primitiva. OCRE-AF/1 ainda bloqueia prontidão, e cloud, autenticação real, broker, workers, CAD importado e validação física geral permanecem pendentes.

Próximos passos: completar OCRE-AF/1 e gates do EIR, ampliar testes de falha/replay, qualificar proveniência CAD e qualidade da malha, e evoluir a prova local para Core Cloud e Scientific Workers.

## Documentação

- [Contexto e memória do projeto](docs/PROJECT_CONTEXT.md): arquitetura, integrações, roadmap, decisões e pendências.
- [Arquitetura de integração](docs/architecture/INTEGRATION_ARCHITECTURE.md): viabilidade, tipos de adaptadores, contratos, limites e fases.
- [Verificação e validação estrutural](docs/architecture/STRUCTURAL_VALIDATION_ARCHITECTURE.md): gates e evidências para a prova Gmsh/CalculiX.
- [Pacote inicial das frentes 1–5](docs/architecture/STARTING_PACKAGE.md): caso de referência, EIR, contrato do adaptador, decisões e sequência de validação.
- [Schemas EIR](schemas/eir/README.md): schemas v0.1 experimentais e v0.2 congelados; relações semânticas e prontidão dependem do validador da aplicação.
- [Arquitetura de validação do EIR](docs/architecture/EIR_VALIDATION_ARCHITECTURE.md): pipeline determinístico, relatórios, segurança de bundle, readiness e decisões ainda abertas.
- [Contrato EIR v0.2](docs/architecture/EIR_V02_CONTRACT.md): contrato congelado, fingerprint `OCRE-AF/1`, migração explícita e três schemas travados por digest; a validação semântica local existe, mas a prontidão permanece bloqueada enquanto OCRE-AF/1 não estiver ligado.
- [Regras do repositório](AGENTS.md): instruções de trabalho específicas do projeto.
