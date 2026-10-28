# Registro inicial de decisões arquiteturais

**Estado:** registro atualizado em 28/10/2026. A prova axial sintética local produziu evidência para Gmsh 4.15.2 e CalculiX CCX 2.23; decisões sobre EIR, adaptadores, backend e escopo continuam sujeitas às ressalvas registradas aqui.

## ADR-001 — OCRE coordena ferramentas por adaptadores isolados

- **Status:** adotada como direção arquitetural.
- **Decisão:** OCRE controla projeto, revisões, execução e resultados; ferramentas mantêm seus formatos e ambientes. O primeiro harness de desenvolvimento usa processo batch local isolado; no produto, a integração batch executa em Scientific Workers cloud.
- **Motivo:** CAD, solver, sessões GPU e protocolos têm ciclos e requisitos incompatíveis com um único processo.
- **Consequência:** integração suportada é sempre combinação explícita de adaptador, versão, plataforma e modalidade.

## ADR-002 — EIR mínimo referencia artefatos nativos e fixa revisão imutável

- **Status:** proposta para a primeira prova.
- **Decisão:** EIR normaliza identidade, unidades, referencial e metadados de caso; arquivos STEP, malha, INP e resultados ficam em artefatos hashados.
- **Motivo:** preservar dados nativos e permitir proveniência sem inventar um formato universal.
- **Consequência:** EIR não representa ainda histórico paramétrico completo ou semântica específica de solver.

## ADR-003 — Primeira referência é barra sólida 3D em tração

- **Status:** referência sintética executada localmente; escopo limitado.
- **Decisão:** usar a barra axial definida em [STRUCTURAL_REFERENCE_CASE.md](STRUCTURAL_REFERENCE_CASE.md) como primeiro benchmark estrutural observado. A execução local usa Gmsh 4.15.2 e CCX 2.23, com origem em uma primitiva `.geo`, malhas C3D10 e três refinamentos; não cobre CAD importado nem implementa adaptadores conformes ao contrato.
- **Motivo:** reação e tensão axial têm referência analítica simples e evitam singularidade de carga pontual em flexão. Grupos, elemento, seleções de pinos e restrições antirrígidas ainda requerem verificação no arquivo gerado e na execução.
- **Consequência:** os critérios numéricos da configuração registrada passaram; gates de entrada/revisão, qualidade geral, proveniência/falhas e validação física não estão concluídos. A evidência permanece limitada ao caso e ambiente testados.

## ADR-004 — Primeiro adaptador usa job batch; produto executa em worker cloud

- **Status:** proposta para o contrato do primeiro adaptador; benchmark local já executado apenas como prova de desenvolvimento.
- **Decisão proposta:** contrato batch com preflight síncrono (validação/preparação) e ciclo persistido iniciado na submissão: status, cancelamento, coleta e limpeza. O harness pode rodar localmente sem provisionar cloud; no produto, validação/pré-flight autoritativos ocorrem no Core Cloud e Gmsh/CalculiX executam em Scientific Workers cloud. IA fica fora deste primeiro adaptador.
- **Motivo:** o primeiro fluxo não requer co-simulação ou telemetria ao vivo.
- **Consequência:** estados/eventos não são promessa de implementação até validação do contrato. Nenhum motor pesado é requisito de instalação para o usuário; execução local avançada, se adicionada, será opcional. Sessões e streams receberão contratos próprios quando houver caso que os exija.

## ADR-005 — Não escolher backend central antes do primeiro fluxo reproduzível

- **Status:** substituída por ADR-007.
- **Decisão histórica:** deixar Go versus Python em aberto até saber quais bibliotecas, adaptadores, validações e necessidades de concorrência realmente compõem o MVP.
- **Motivo:** a prova proposta rodava como ferramentas externas; escolher backend naquele momento não provaria benefício nem compatibilidade.
- **Consequência histórica:** a automação mínima da prova não consolidou uma API de produção. A decisão inicial atual está registrada em ADR-007 e continua sem validação de produção.

## ADR-006 — Adiar canonicalização do hash de revisão

- **Status:** proposta para revisão do líder.
- **Decisão proposta:** manter SHA-256 de bytes exatos para artefatos; não definir algoritmo/versionamento de canonicalização para manifesto/revisão até existir requisito de serialização e teste de conformidade.
- **Motivo:** um digest do manifesto depende de uma representação determinística, ainda não especificada nem validada.
- **Consequência:** hash de revisão permanece pendente/não interoperável; exemplos com placeholder não podem ser usados como schema congelado. Nenhum padrão de canonicalização é selecionado nesta proposta.

## ADR-007 — Baseline inicial de linguagens por camada

- **Status:** adotada como decisão inicial de implementação; revisável mediante evidência. Nenhuma linguagem ou aplicação de produto está implementada por este ADR.
- **Decisão:** usar **TypeScript** no cliente para UI, Three.js e interação CAD leve, com HTML/CSS como marcação e estilo da interface; **Python** no Core Cloud para API/controle, validação EIR e pré-flight, gestão de jobs e orquestração; **Python** nos adaptadores de controle que atendam aos contratos, incluindo invocação segura de motores externos; e **SQL** para esquema, consultas e migrações relacionais quando PostgreSQL for implementado. O cliente TypeScript é entregue como JavaScript para execução no browser.
- **Fronteiras:** motores científicos e CAD pesado permanecem em Scientific Workers isolados e não são reescritos nestas linguagens. O baseline não compromete o OCRE com código próprio em C++ nem adota Go. C++ continua disponível para um adaptador OCCT restrito se uma prova mostrar que APIs ou wrappers existentes não atendem aos requisitos; Go pode ser reconsiderado mediante necessidade operacional ou gargalo medido no Core.
- **Motivo:** a separação aprovada atribui ao cliente interação leve e visualização, ao Core validação determinística e coordenação, e aos workers execução pesada. A evidência disponível não demonstra uma necessidade de segundo runtime no Core ou de código C++ próprio; também não prova desempenho ou prontidão de produção de Python.
- **Limites e gates:** TypeScript não aprova OpenCascade.js/WASM nem paridade geométrica local/cloud. Antes de fechar a implementação de importação CAD, uma prova STEP/OCCT deve validar fidelidade geométrica, assemblies e proveniência, incluindo a possibilidade de usar Gmsh/OpenCASCADE via Python. Se essa via falhar, avaliar um adaptador C++ isolado. Antes de produção, demonstrar concorrência, cancelamento da árvore de processos, isolamento e limites de recursos, recuperação de worker, observabilidade e implantação cloud.
- **Formatos de contrato:** JSON, JSON Schema, OpenAPI e YAML são formatos/linguagens de contrato ou configuração, não alteram a seleção das linguagens de aplicação. O EIR v0.2 permanece independente da linguagem e do local de execução.
- **Revisão:** comparar alternativas com requisitos e cargas reais; não adicionar Go ou C++ por preferência abstrata nem inferir que a ausência de benchmark favorece qualquer runtime.

## Questões que a prova deve resolver

1. A combinação local Gmsh 4.15.2/CalculiX CCX 2.23 foi executada para este caso; quais plataformas e geometrias adicionais precisam ser suportadas?
2. Para a geometria sintética testada, C3D10, grupos necessários e deck separado foram inspecionados e resolvidos. Como validar importação CAD e identidade/proveniência até grupos e malha?
3. Saídas de reação e campos de integração foram extraídos para o benchmark; como validar interoperabilidade, unidades e URI/hash em um `ResultManifest` conforme ao contrato?
4. Os critérios de erro e refinamento passaram neste caso; qual limiar de qualidade de malha e quais ensaios físicos são necessários para os próximos usos?
5. Qual plataforma e fronteira de isolamento o MVP precisa suportar primeiro?
6. Qual estratégia de persistência/canonicalização dá hashes estáveis para a revisão e artefatos?

Licenciamento e distribuição devem ser analisados por pacote, versão e modo de execução antes de empacotar ou oferecer serviço; esta nota não é parecer jurídico.
