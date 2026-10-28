# Contexto do projeto OCRE

**Data de referência:** 28/10/2026
**Estado:** arquitetura conceitual consolidada e primeira prova local implementada. O backend Python inclui validador EIR, pré-flight que bloqueia prontidão por falta de OCRE-AF/1, jobs, persistência/sync SQLite, outbox fake e API FastAPI. A suíte passou com 73 testes e 16 subtests. Cloud, IAM real, broker, workers, CAD importado e validação física geral continuam pendentes.

Este documento é a memória canônica para continuidade do projeto. Ele consolida os contextos fornecidos sem tratar tecnologias candidatas como decisões de implementação ou integrações concluídas.

O detalhamento da arquitetura de integração está em [docs/architecture/INTEGRATION_ARCHITECTURE.md](architecture/INTEGRATION_ARCHITECTURE.md); ele especifica modalidades de adaptador, contratos e limites sem declarar as integrações como implementadas.
Os gates da primeira prova estrutural estão em [docs/architecture/STRUCTURAL_VALIDATION_ARCHITECTURE.md](architecture/STRUCTURAL_VALIDATION_ARCHITECTURE.md), separando verificação da integração/solução de validação física futura.

O pacote inicial para as frentes de especificação e prova está em [docs/architecture/STARTING_PACKAGE.md](architecture/STARTING_PACKAGE.md), incluindo o caso de referência, EIR mínimo, contrato batch e decisões propostas.
Os JSON Schemas experimentais v0.1 do EIR e do manifesto de grupos estão em [schemas/eir](../schemas/eir); eles validam apenas estrutura e tipos, não canonicalização, relações entre IDs, confinamento URI nem semântica UCUM.
A arquitetura e o estado do validador determinístico estão em [EIR_VALIDATION_ARCHITECTURE.md](architecture/EIR_VALIDATION_ARCHITECTURE.md). Há implementação local estrutural/semântica; o pré-flight ainda bloqueia execução até OCRE-AF/1 e outros gates completos. O baseline inicial de linguagens por camada está em ADR-007; adoção na interface e validação de produção permanecem pendentes.

O contrato EIR v0.2, com quaternion, perfil UCUM, fingerprint `OCRE-AF/1` e migração explícita, foi congelado após revisão independente em [EIR_V02_CONTRACT.md](architecture/EIR_V02_CONTRACT.md). Os três schemas Draft 2020-12 têm hashes fixados em [`schemas/eir`](../schemas/eir/README.md). O validador local está implementado; cálculo completo de OCRE-AF/1 e pré-flight cloud continuam pendentes.

## 1. Visão e princípios

OCRE propõe uma plataforma universal e modular de engenharia que reúne CAD paramétrico, simulação física e científica, robótica, eletrônica, controle, simulação industrial e gêmeos digitais.

O usuário poderá criar e editar modelos manualmente ou com auxílio opcional de IA, associar propriedades físicas, executar análises, visualizar resultados, comparar versões, otimizar projetos e, futuramente, integrar sistemas reais.

Princípios orientadores:

1. A plataforma deve funcionar sem IA.
2. Modelos criados por IA devem ser dados CAD estruturados, editáveis e paramétricos, usando o mesmo modelo de operações da edição manual.
3. IA interpreta, planeja, sugere e explica; código determinístico executa operações, sincroniza dados, chama solvers e aplica validações.
4. A validação científica não será delegada à IA.
5. Solvers e kernels existentes serão integrados, respeitando capacidade, versão e licença.
6. Dados, entradas, conversões, versões de ferramentas e resultados devem ser rastreáveis e reproduzíveis.

## 2. Ambientes e responsabilidades

### Cliente local

Interface, CAD interativo leve, visualização 3D, previews, operações geométricas leves, cache, histórico e edição offline parcial. Não instala nem executa motores científicos pesados no fluxo padrão. Deve manter resposta imediata quando a operação puder ser local; esses resultados ficam como rascunho até confirmação cloud.

### Core Cloud

Gerenciamento de projetos e revisões canônicas, sincronização, armazenamento central, APIs, autenticação, validação EIR/pré-flight autoritativos, integração de ferramentas, operações CAD avançadas, IA opcional e orquestração de tarefas.

### Scientific Workers

Workers cloud isolados e escaláveis para OCCT complexo, Gmsh, CalculiX, FEM/FEA, CFD, robótica, dinâmica, térmica, eletrônica, controle, co-simulação e treinamento de políticas. Nenhum motor pesado é uma instalação obrigatória no computador do usuário. Execução local avançada pode ser adicionada futuramente como opção.

O gerenciador determinístico escolhe worker cloud/capacidade compatível considerando complexidade, recursos, latência, custo, volume de dados, licença e disponibilidade de motores. O padrão do produto não encaminha motores pesados ao computador do usuário. A prova Gmsh/CalculiX local em `experiments/fea-reference/` é somente experimento de desenvolvimento.

## 3. Engineering Core e EIR

O **Engineering Core** coordena, mas não substitui kernels geométricos nem motores físicos. Componentes conceituais:

- Project Manager e Version Control.
- Parametric Model Manager e Geometry Manager.
- Assembly Manager e Material Database.
- Execution Orchestrator e Integration Manager.
- Simulation Manager e Result Manager.
- Validation & Provenance.
- Unit & Coordinate Service.

A representação intermediária interna, provisoriamente denominada **Engineering Intermediate Representation (EIR)**, deverá representar Project, Geometry, ParametricGraph, Assembly, Material, SimulationCase, Execution, Result e Coupling. Ela será o contrato interno dos adaptadores, sem substituir STEP, USD, URDF, STL, FMI/FMU ou formatos nativos.

Cada execução deverá apontar para uma versão imutável do modelo e registrar materiais, malhas, condições de contorno, parâmetros, versões/configurações dos solvers e proveniência dos resultados.

## 4. CAD e geometria

**Direção proposta:** Open CASCADE Technology (OCCT) como kernel geométrico. OpenCascade.js/WebAssembly é candidato para operações locais; o acesso a OCCT nos workers cloud ainda precisa de uma prova STEP. A linguagem do cliente é TypeScript; isso não aprova OpenCascade.js/WASM nem paridade local/cloud. A decisão inicial não compromete o OCRE com código próprio C++; um adaptador C++ isolado poderá ser escolhido se wrappers/APIs existentes não satisfizerem fidelidade, assemblies e proveniência. Compatibilidade de versões, tolerâncias e referências topológicas ainda requer validação.

O histórico paramétrico interno armazenará operações, parâmetros e dependências. Formatos de intercâmbio como STEP e STL podem não preservar o histórico paramétrico original.

FreeCAD é referência de CAD paramétrico/automação; Gmsh é candidato para malhas; OpenUSD/URDF são possíveis formatos de cena/robótica; Three.js é candidato para visualização; Blender pode apoiar modelagem visual/animação. Nenhum deles está declarado integrado.

## 5. Contrato de integração

Fluxo geral:

**Interface → Engineering Core → Integration & Simulation Orchestrator → adaptadores → motores científicos → resultados → interface.**

Contrato conceitual de adaptador:

- `describe_capabilities()`
- `validate_input()`
- `prepare_model()`
- `execute()`
- `get_status()`
- `collect_results()`
- `cancel()`
- `cleanup()`

Adaptadores devem declarar capacidades reais, formatos aceitos, versões e limitações. Co-simuladores poderão expor inicialização, avanço temporal, troca de variáveis e checkpoint.

## 6. Integrações científicas candidatas

| Domínio | Candidatos | Observações |
| --- | --- | --- |
| Estrutural | CalculiX, Code_Aster, Elmer FEM; ANSYS/Abaqus futuros | Exige malha, materiais, condições de contorno, convergência e validação de referência. |
| Malha e pós-processamento | Gmsh, ParaView | Fidelidade geométrica e qualidade da malha devem ser verificadas. |
| Robótica e dinâmica | Isaac Sim, Newton, MuJoCo, Gazebo, ROS 2 | Compatibilidade de versões, formatos e backend físico precisa ser confirmada. |
| Treinamento robótico | Isaac Lab, PyTorch, RL Games, RSL-RL, Stable-Baselines3 | Preferencialmente cloud; considerar sim-to-real, calibração e segurança. |
| CFD e térmica | OpenFOAM, SU2, Elmer | Resultados remotos devem retornar em formatos otimizados para visualização. |
| Eletrônica e controle | OpenModelica, ngspice, KiCad, FMI/FMU | FMI/FMU pode apoiar intercâmbio e co-simulação, não substitui todos os adaptadores. |
| Simulação industrial | SimPy, SUMO, AnyLogic, Factory I/O, Tecnomatix | Verificar interfaces e licenças de cada integração. |
| Gêmeos digitais | OPC UA, MQTT, REST; PLC/SCADA/MES/ERP | Um gêmeo digital pode existir sem uma representação 3D. |

## 7. Pipeline robótico e estrutural

Fluxo multidisciplinar proposto:

1. CAD e Engineering Core registram peças, juntas e propriedades.
2. Model Preparation Service gera representações como USD e/ou URDF.
3. Simulador robótico configura colisores, massa, inércia, articulações, sensores e atuadores.
4. Isaac Sim/Newton ou outro backend calcula posições, velocidades, torques, contatos e forças.
5. O sistema seleciona posições/condições críticas e mapeia cargas, pontos de aplicação, referências e unidades.
6. Gmsh prepara a malha estrutural e CalculiX (ou outro solver) calcula tensões/deformações.
7. Validação verifica qualidade de malha, convergência e critérios de engenharia.
8. Resultados versionados são associados e visualizados no modelo CAD.

Newton poderá atuar como backend físico do Isaac Sim apenas se versões e compatibilidade confirmarem esse uso. Não se deve calcular a mesma dinâmica em duplicidade.

## 8. Sincronização, latência e cache

Sincronização CAD local/cloud determinística baseada em log de operações, IDs únicos, dependências, versões, snapshots, confirmação, sincronização incremental, detecção explícita de conflitos e recuperação. Alterações concorrentes nunca devem sobrescrever silenciosamente umas às outras; IA pode sugerir resolução, mas não é autoridade de consistência.

CAD para simuladores passa por preparação de modelo, adaptadores, conversão, mapeamento de materiais/referências, geração de malha e validação. Simulador para interface usa progresso/eventos, WebSocket quando adequado, armazenamento de objetos, resultados versionados e visualização progressiva. IA usa APIs tipadas, permissões, validação de operações e logs.

Cache conceitual:

- **L1/RAM:** geometria ativa, seleção, câmera e estado do editor.
- **L2/SSD local:** projetos recentes, histórico, malhas, previews e operações não sincronizadas.
- **L3/cloud storage:** projetos, versões, arquivos científicos, resultados e relatórios.
- **L4/cache científico:** resultados reutilizáveis associados a entradas e configuração exatas.

Cache científico depende de hashes de geometria, materiais, malha, condições de contorno, parâmetros e versões/configuração de solver. Mudanças incompatíveis invalidam ou marcam resultados como desatualizados.

## 9. Co-simulação

O **Co-Simulation Coordinator** deverá suportar inicialmente:

- **Sequencial:** a saída de um motor alimenta o próximo.
- **Acoplada:** motores trocam variáveis durante execução.

Responsabilidades: dependências, tempo/passos, troca de variáveis/campos, unidade e coordenadas, interpolação, convergência, erros e checkpoints quando disponíveis. FMI/FMU é opção importante, mas não universal. A coordenação será determinística.

## 10. IA opcional

Um orquestrador poderá interpretar solicitações, explicitar requisitos ausentes, planejar, selecionar ferramentas, convocar especialistas sob demanda e consolidar explicações. Especialidades potenciais incluem CAD, mecânica, materiais, estrutural, CFD, robótica, eletrônica, controle, otimização, verificação e relatórios.

Agentes recebem objetivos, ferramentas, permissões e limites definidos. Não poderão executar operações críticas ou aprovar resultados fora dessas permissões. Execução real passa por APIs e motores determinísticos.

## 11. Validação e confiabilidade

Uma integração deve ser avaliada por compatibilidade de formatos e versões, fidelidade geométrica, propriedades físicas, unidades, coordenadas, qualidade de malha, convergência, comparação com referências, rastreabilidade, reprodutibilidade, recuperação de falhas, licenciamento e segurança.

Uma execução concluída não prova que a simulação está validada; um resultado numérico isolado também não comprova segurança estrutural.

## 12. Baseline inicial de linguagens e tecnologias ainda abertas

**Linguagens escolhidas como baseline inicial:** TypeScript no cliente para UI, Three.js e interação CAD leve, com HTML/CSS para marcação e estilo; Python no Core Cloud para API/controle, validação EIR/pré-flight, jobs e orquestração, e nos adaptadores de controle que atendam aos contratos; SQL para o modelo relacional quando PostgreSQL for implementado. TypeScript será entregue como JavaScript para o browser. A decisão está registrada em [ADR-007](architecture/ARCHITECTURE_DECISIONS.md#adr-007--baseline-inicial-de-linguagens-por-camada). Ela não significa que essas partes já estejam implementadas nem que Python esteja validado para produção.

**Fora do baseline de código próprio inicial:** Go não será adotado em paralelo sem requisito operacional ou gargalo medido; C++ não será compromisso de implementação do OCRE antes de prova. OCCT continua sendo um motor nativo possível nos workers, mas a interface de importação STEP ainda requer uma prova de fidelidade geométrica, assemblies e proveniência. Se APIs ou wrappers existentes não satisfizerem os requisitos, avaliar um adaptador C++ isolado.

**Não decididos por esta escolha de linguagens:** framework de UI/API, OpenCascade.js/WASM, implementação do acesso a OCCT nos workers, PostgreSQL, SQLite/IndexedDB/OPFS, object storage, REST/WebSocket/gRPC, filas, containers e infraestrutura cloud. JSON, JSON Schema, OpenAPI e YAML são formatos de contrato/configuração. Motores científicos pesados permanecem isolados em Scientific Workers cloud; nenhum deles se torna instalação obrigatória para o usuário.

## 13. Roadmap de integração

1. **CAD e análise estrutural:** OCCT → Gmsh → CalculiX → resultados validados.
2. **CAD e robótica:** OCCT → USD/URDF → Isaac Sim/Newton ou motor compatível.
3. **Integração multidisciplinar:** cargas dinâmicas da robótica → análise estrutural.
4. **CFD e térmica:** CAD → OpenFOAM/SU2/Elmer.
5. **Co-simulação:** OpenModelica, FMI e outros motores.
6. **Expansão industrial:** ROS 2, OPC UA, MQTT, KiCad, SimPy, SUMO e integrações comerciais.

## 14. Decisões e pendências

### Direções consolidadas

- Plataforma modular com processamento local/cloud distribuído.
- CAD manual e assistido por IA, sem dependência funcional da IA.
- OCCT como direção de kernel geométrico.
- Engineering Core como coordenador, EIR como representação interna proposta.
- Adaptadores independentes e execução científica em workers especializados.
- Sincronização/versionamento determinísticos e resultados vinculados a entradas imutáveis.
- Validação científica independente da IA e reutilização de motores existentes.

### Candidatos e integrações ainda não validados

- OCCT local via OpenCascade.js e interface de importação STEP/OCCT nos workers cloud; não está decidido se exigirá código próprio C++.
- Gmsh/CalculiX para primeiro pipeline estrutural.
- Isaac Sim/Newton para robótica; Newton como backend depende de compatibilidade.
- Frameworks de backend, armazenamento, comunicação, filas e execução listados na seção anterior.
- Demais motores listados neste documento.

### Decisões abertas

- Especificação EIR e modelo paramétrico; estabilidade de referências topológicas.
- Contratos completos dos adaptadores e formato de resultados.
- Prioridade exata do primeiro marco: CAD híbrido como fundação versus CAD → malha → análise estrutural como primeira demonstração científica.
- Framework e implantação do Core Cloud; baseline de linguagem já selecionado em ADR-007, ainda sem implementação/benchmark de produção.
- Resolução de conflitos local/cloud e seleção de framework desktop.
- Infraestrutura cloud, segurança/isolation dos workers, autenticação, custos e atualização/distribuição dos kernels.
- Versões compatíveis dos motores e licenças.
- Estratégia de co-simulação, benchmarks, tolerâncias e critérios de aceitação científicos.

## 15. Próximas ações recomendadas

1. Especificar um recorte mínimo do Engineering Core e os limites entre seus componentes.
2. Definir o primeiro esquema EIR versionado, incluindo unidades, coordenadas, proveniência e referências geométricas.
3. Descrever contrato de adaptador com schemas de entrada/saída e ciclo de execução.
4. Escolher o primeiro pipeline e um caso de referência com resultados esperados e tolerâncias.
5. Verificar documentação oficial, versões, licenças, distribuição e requisitos de execução dos componentes escolhidos.
6. Só então implementar um mini-MVP e reportar validação com evidências de execução.
