# OCRE

**OCRE** é uma proposta de plataforma modular de engenharia para integrar CAD paramétrico, simulação científica, robótica, eletrônica e sistemas industriais em uma experiência híbrida local/cloud. O cliente no computador será leve; operações CAD complexas e motores científicos serão executados em infraestrutura cloud.

O projeto está na etapa de **concepção e definição arquitetural**. Este repositório ainda não declara implementação, integração ou validação científica concluída. Tecnologias citadas são candidatas até que compatibilidade, licença e comportamento sejam verificados.

## Princípios

- A plataforma básica deve funcionar sem inteligência artificial.
- A IA será opcional e auxiliará criação, planejamento e automação; operações e validações serão executadas por componentes determinísticos.
- O cliente local concentra interface, visualização 3D, edição CAD interativa leve, pré-visualizações, cache, histórico e sincronização. OpenCascade.js/WebAssembly poderá executar operações geométricas leves quando adequado.
- O Core Cloud mantém projetos e revisões canônicas, valida EIR e pré-flight, armazena artefatos e orquestra execuções.
- Scientific Workers isolados provisionam OCCT completo para operações complexas e motores como Gmsh, CalculiX, OpenFOAM e Isaac Sim/Newton, conforme capacidade, versão e licença.
- O usuário não precisa instalar nem abrir individualmente motores CAD ou científicos para usar o fluxo padrão. Execução local avançada poderá ser uma opção futura, nunca um requisito.
- O cliente mostra previews imediatamente e continua responsivo durante jobs remotos; resultados locais pendentes só se tornam parte da revisão canônica após confirmação cloud.
- Motores existentes serão integrados por adaptadores com contratos explícitos, sem recriar seus solvers.
- Projetos, entradas, versões de ferramentas e resultados precisam ser rastreáveis e reproduzíveis.

## Arquitetura em uma linha

**Cliente local → Engineering Core → orquestrador de integrações e simulações → adaptadores → motores científicos → resultados versionados → interface.**

O Engineering Core coordena projetos, representação paramétrica, geometria, montagens, materiais, versões, execução, resultados, unidades e proveniência. O formato interno provisoriamente chamado **Engineering Intermediate Representation (EIR)** deverá relacionar esses dados sem substituir formatos externos como STEP, USD, URDF ou FMI/FMU.

## Primeiro marco proposto

Demonstrar um fluxo reproduzível de **CAD paramétrico leve no cliente → sincronização → validação EIR/pré-flight no Core Cloud → geração de malha com Gmsh e análise CalculiX em Scientific Workers → validação → visualização de resultados no cliente → edição e nova execução**. Gmsh e CalculiX são candidatos para esse primeiro pipeline; o fluxo de produto ainda não está implementado.

O harness local de Gmsh/CalculiX, quando usado em desenvolvimento, é experimental e não define os requisitos de instalação do produto.

Uma etapa multidisciplinar posterior poderá transferir cargas dinâmicas de robótica para análise estrutural: **CAD → Isaac Sim/Newton → cargas → CalculiX → tensões associadas ao modelo**. A compatibilidade entre versões e o papel de Newton no Isaac Sim precisam ser verificados antes de assumir essa integração.

## Tecnologias candidatas

OCCT/OpenCascade.js, Three.js, Gmsh, CalculiX, Isaac Sim, Newton, MuJoCo, Gazebo/ROS 2, OpenFOAM, SU2, Elmer, OpenModelica, ngspice, FMI/FMU, PostgreSQL, SQLite/IndexedDB, REST/WebSocket/gRPC e infraestrutura baseada em containers aparecem como opções de integração. A stack definitiva ainda não foi selecionada.

## Estado e próximos passos

**Estado:** arquitetura conceitual híbrida definida; implementação e validação técnica do fluxo de produto pendentes. A seleção definitiva de tecnologias, compatibilidade de versões, provisionamento dos workers, licenciamento, custos, segurança e recuperação de falhas ainda precisam ser validados.

Próximos artefatos recomendados: detalhar o Engineering Core e o EIR, definir os contratos de adaptadores e escolher um pipeline científico mínimo com critérios de aceitação e referências de validação.

## Documentação

- [Contexto e memória do projeto](docs/PROJECT_CONTEXT.md): arquitetura, integrações, roadmap, decisões e pendências.
- [Regras do repositório](AGENTS.md): instruções de trabalho específicas do projeto.
