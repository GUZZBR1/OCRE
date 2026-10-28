# Arquitetura de verificação e validação estrutural

**Estado:** arquitetura e gates propostos; benchmark sintético axial executado localmente em 28/10/2026. A configuração Gmsh/CalculiX passou os critérios numéricos registrados, mas os gates de entrada, qualidade geral, proveniência/falhas e validação física continuam parciais ou não avaliados.
**Escopo:** barra sólida 3D em tração uniaxial, caso linear estático. Não define critérios para certificar projetos reais.

Este plano acompanha o caso e as metas registrados em [STRUCTURAL_REFERENCE_CASE.md](STRUCTURAL_REFERENCE_CASE.md). O benchmark local atual também executa Gmsh 4.15.2→C3D10→deck CalculiX separado→CCX 2.23 em uma geometria `Box` definida por `.geo`. Isso não valida importação de CAD nem conversão CAD→malha. O baseline C3D8 continua sendo um caminho separado.

## 1. Objetivo e vocabulário

O OCRE deve produzir evidência por etapa, para que uma conversão bem-sucedida ou um solver que terminou não sejam confundidos com uma análise confiável.

- **Verificação da integração:** os dados foram traduzidos e entregues ao programa correto sem alterar silenciosamente geometria, grupos, unidades, materiais, condições de contorno ou resultados.
- **Verificação da formulação:** o problema entregue ao solver representa as hipóteses e condições escolhidas.
- **Verificação da solução numérica:** a solução calculada é consistente com referências matemáticas e está suficientemente estável para as grandezas de interesse, dentro dos critérios definidos para o caso.
- **Validação física:** comparar previsões do modelo com medições do sistema real sob condições relevantes ao uso pretendido.
- **Incerteza:** registrar como incertezas de geometria, material, carga, condições de contorno, medição e discretização podem influenciar as grandezas reportadas.

A comparação da barra com uma solução analítica é verificação, não validação experimental. Aprovação nesse benchmark significa apenas que o pipeline foi verificado para aquele caso e configuração; não prova que um componente real está seguro.

## 2. Caso de referência

O candidato atual é um prisma retangular de `L=100 mm`, `b=h=10 mm`, material linear elástico isotrópico (`E=210000 N/mm²`, `ν=0.3`), `Ux=0` na face `x=0` e `Ux=0.01 mm` na face `x=L`. As faces laterais ficam livres para contrair por Poisson; restrições transversais mínimas nos pinos removem movimentos rígidos. Usar coordenadas e seleção dos pinos registrados em [STRUCTURAL_REFERENCE_CASE.md](STRUCTURAL_REFERENCE_CASE.md).

Para a solução uniforme em tração:

- deformação axial: `εxx = δ/L = 0.0001`;
- tensão axial: `σxx = E εxx = 21 MPa`;
- reação axial: `Rx = E A δ/L = 2100 N`, com `A = b h = 100 mm²`;
- deformação lateral: `εyy = εzz = -ν εxx = -0.00003`.

Os valores servem de referência analítica para esta idealização. Comparar tensão média no terço central, longe dos pinos; confirmar também estabilidade e que as restrições não bloqueiam a contração lateral. Os parâmetros e critérios foram aplicados em três execuções CalculiX desta configuração; permanecem específicos do benchmark e não são limites gerais.

## 3. Gates independentes

Cada gate retorna relatório estruturado com `status` (`passed`, `warning`, `failed`, `not_assessed`), métricas observadas, critério aplicado, mensagens e referências aos artefatos. Se uma evidência exigida estiver ausente, o gate não pode retornar `passed`.

### Gate A — entrada e revisão

- Validar schema e versão do caso.
- Confirmar revisão imutável/hash do modelo e identidade dos parâmetros.
- Exigir unidades, eixo de coordenadas, referencial, material, carregamento e restrições explícitos.
- Rejeitar valores ausentes, dimensões não positivas, unidade desconhecida ou seleção geométrica ambígua.

**Evidência:** caso de análise validado, resumo dimensional e hash das entradas.

### Gate B — geometria e tradução CAD → malha

- Validar sólido de origem e conferir dimensões, bounding box e volume antes/depois da preparação.
- Conferir que o volume estrutural existe na malha, elementos são suportados pelo adaptador/solver e conectividade/orientação são válidas.
- Conferir que os grupos `X_FIXED`, `X_DISPLACED` e os pinos transversais existem, não estão vazios e apontam para as regiões/nós esperados.
- Registrar qualidade da malha, contagem de nós/elementos, tipo e ordem dos elementos, configurações e versão do Gmsh.
- Emitir relatório sobre grupos, elementos ou propriedades descartados.

**Evidência:** geometria original, malha, mapa de grupos, métricas e `ConversionReport`. A checagem é necessária porque o Gmsh pode exportar apenas elementos pertencentes a grupos físicos quando eles são definidos; veja o [manual do Gmsh](https://gmsh.info/doc/texinfo/).

### Gate C — formulação e arquivo do solver

- Conferir que o deck do CalculiX contém os elementos, material, espessura/seção quando aplicável, apoio, carga e tipo de análise esperados.
- Conferir unidade coerente entre comprimentos, força, tensão e módulo elástico; os programas não podem inferir unidades ausentes.
- Conferir direção e resultante das cargas e graus de liberdade restringidos.
- Depois da solução, comparar resultantes de reação com força e momento externos; os limites de equilíbrio serão definidos para o benchmark.
- Detectar restrição insuficiente, movimento de corpo rígido inesperado, warnings e término anormal do solver.

**Evidência:** deck gerado, resumo parseado das condições, stdout/stderr, código de saída e reações.

### Gate D — verificação da solução numérica

- A execução deve terminar corretamente e disponibilizar as grandezas de interesse definidas pelo caso.
- Comparar reação axial, tensão média axial no terço central e deformação axial/lateral com a referência do caso, usando regiões e extração fixadas antes da execução.
- Avaliar solução em três malhas progressivamente refinadas e registrar cada grandeza por nível. A comparação entre malhas usa a mesma definição de região, média e sinal.
- O caso de referência usa erro de até 1% para reação e tensão axial, mudança de até 1% entre as duas malhas mais refinadas e erro de até 5% para contração lateral. As três execuções registradas atenderam esses critérios; continuam sendo metas específicas deste benchmark, não limites universais do OCRE.
- Não mascarar divergência alterando pontos, médias ou tolerâncias depois de observar resultados. Registrar justificativa se os critérios forem revisados.
- Não usar máximo nodal perto dos pinos como único critério; usar as grandezas globais e a região central definidas no caso.

**Evidência:** outputs nativos do solver, métricas extraídas, resultados da referência, estudo de refinamento e relatório comparativo. O [manual do CalculiX 2.23](https://www.dhondt.de/ccx_2.23.pdf) descreve os formatos e recursos do solver; a comparação deste caso axial é feita com as relações analíticas acima.

### Gate E — resultado, proveniência e falhas

- Conferir que deslocamento e tensão importados correspondem aos nós/elementos e referencial da malha que gerou o resultado.
- Conferir unidade e convenção de componentes vetoriais/tensoriais; preservar também os arquivos nativos.
- Registrar hashes de revisão, deck, malha e resultados, além das versões/configurações do adaptador, Gmsh e CalculiX.
- Reexecução deve produzir resultados numericamente comparáveis conforme critério do caso; igualdade binária só se o ambiente a garantir.
- Testar caminhos de entrada inválida, grupo ausente, elemento não suportado, falha de solver, timeout e cancelamento.
- Execução falha/cancelada ou resultado parcial nunca recebe status de resultado aprovado. Preservar evidência suficiente para diagnóstico.

**Evidência:** `ResultManifest`, logs, artefatos nativos, relatório de estado e hashes.

### Estado observado do benchmark local (28/10/2026)

O manifesto [solver_runs_manifest.json](../../experiments/fea-reference/runs/solver_runs_manifest.json) registra três malhas C3D10 (433, 3.594 e 24.960 elementos), decks aceitos pelo CCX 2.23, saídas nativas e extração das métricas nos pontos de integração. Em todas, `Rx=2100 N`, `S11=21 MPa`, `E11=0.0001` e `E22=E33=-0.00003`; a mudança entre as duas malhas mais finas foi 0% para reação e tensão. Os critérios numéricos D passaram para esta configuração.

O status geral não é `passed`: A (pacote EIR/revisão usado para gerar a execução) permanece `not_assessed`. Os três schemas EIR v0.2 e fixtures passaram validação estrutural separada, mas o benchmark não foi submetido como `InputBundle` validado pelo Core Cloud. B tem `warning` porque a qualidade de forma foi medida sem limiar de aceitação e a geometria veio de uma primitiva `.geo`, não de CAD importado; C passou para os decks separados executados; D passou para as métricas deste benchmark; E tem `warning`, pois fault-injection/replay e validação externa não foram avaliados. A trilha não valida qualidade geral para outros modelos, importação CAD/OCCT, pré-flight integrado nem uso de engenharia real.

## 4. Validação física posterior

Se o OCRE for usado para prever o comportamento de uma barra real, preparar ensaio controlado com geometria, propriedades do material, apoios e deslocamento medidos. Comparar grandezas observáveis equivalentes às do modelo, registrar calibração e incerteza dos instrumentos e caracterizar variabilidade do material e montagem.

A faixa de uso validada é limitada às condições cobertas pelos dados e hipóteses. Alterar material, regime não linear, contato, geometria, escala ou condições de apoio pode exigir nova evidência. Uma comparação com um único ensaio ou com solução analítica não valida todas essas extrapolações.

Orientação de referência: [NASA-HDBK-5026 (2024)](https://standards.nasa.gov/sites/default/files/standards/NASA/Baseline/0/NASA-HDBK-5026-Final-8-12-2024.pdf) descreve documentação de hipóteses, cargas, propriedades, malha e correlação com ensaio em seu contexto aeroespacial; essa orientação não define por si só os limites do OCRE. A [ASME V&V 10](https://www.asme.org/codes-standards/find-codes-standards/standard-for-verification-and-validation-in-computational-solid-mechanics) é a norma específica para V&V em mecânica computacional de sólidos.

## 5. Responsabilidades arquiteturais

- **Adapter Gmsh:** conversão, grupos, tipos/conectividade e métricas de malha.
- **Adapter CalculiX:** geração/validação do deck, execução, logs, estado e extração de resultado nativo.
- **V&V evaluator determinístico:** executa asserts e métricas especificados no caso; não delega aprovação a IA.
- **Engineering Core:** associa relatório a revisão imutável, armazena proveniência e invalida resultados incompatíveis quando os inputs mudam.
- **Interface:** apresenta estados, métricas, warnings e critérios aplicados; não rotula a execução como segura com base somente em um benchmark.
- **IA opcional:** pode explicar um relatório já produzido ou sugerir diagnósticos, sem alterar status de gate.

## 6. Ordem de implementação

O benchmark sintético já cobriu a especificação do caso, montagem dos decks, execução do solver, extração, comparação analítica e convergência de três malhas. A sequência restante é:

1. Exercitar falhas, replay, timeout, cancelamento e recuperação, preservando os estados e artefatos diagnósticos.
2. Definir e aplicar um gate quantitativo de qualidade de malha, além das verificações geométricas já medidas.
3. Validar importação CAD→Gmsh, identidade/proveniência da geometria e rastreio até grupos e malha.
4. Implementar no Core Cloud a validação semântica/pre-flight dos schemas EIR v0.2 e manifestos de entrada/resultado; os schemas estruturais e fixtures já têm verificação isolada. Manter fingerprint e URI dentro dos critérios de interoperabilidade.
5. Ligar resultados e seleções à visualização, conferindo correspondência de nós/elementos e referencial.
6. Planejar validação física representativa para um uso de engenharia definido.

Esses passos ampliam a evidência além do benchmark de barra. A execução numérica aprovada não valida o OCRE para outros modelos, CAD importado ou uso de engenharia real.

## Referências metodológicas

- [ASME VVUQ: conceitos e normas](https://www.asme.org/codes-standards/publications-information/verification-validation-uncertainty)
- [NASA: critérios de verificação/validação estrutural e convergência](https://lmse.larc.nasa.gov/admin/public_docs/LPR_1710-15K_Final.pdf)
- [NASA-HDBK-5026 (2024)](https://standards.nasa.gov/sites/default/files/standards/NASA/Baseline/0/NASA-HDBK-5026-Final-8-12-2024.pdf)
