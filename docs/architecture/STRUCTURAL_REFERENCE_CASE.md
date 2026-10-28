# Caso de referência estrutural: barra em tração uniaxial

**Estado:** executado como benchmark sintético local em 28/10/2026: Gmsh 4.15.2 gerou C3D10 a partir de uma primitiva `Box` em `.geo`, e CalculiX CCX 2.23 resolveu três malhas. Os critérios numéricos registrados passaram nesta configuração. Não houve importação de arquivo CAD, validação física nem aprovação geral do pipeline OCRE.

## Objetivo

Verificar a cadeia mínima de uma análise estrutural linear elástica: geometria e grupos → malha volumétrica → entrada CalculiX → execução → reação e tensão → comparação com solução analítica. A barra em tração foi escolhida por permitir métricas globais limpas sem tensão singular de carga pontual ou engaste em flexão. Não é uma certificação de projeto.

## Definição do caso

Prisma retangular, eixo `x` longitudinal, com deslocamento axial imposto entre as faces de extremidade. As faces laterais ficam livres para contrair por Poisson.

| Parâmetro | Valor candidato | Unidade |
| --- | ---: | --- |
| Comprimento `L` | 100 | mm |
| Largura `b` | 10 | mm |
| Altura `h` | 10 | mm |
| Área `A = b h` | 100 | mm² |
| Módulo de Young `E` | 210000 | N/mm² |
| Poisson `ν` | 0.3 | 1 |
| Deslocamento imposto `δ` | 0.01 | mm |
| Deformação axial `ε = δ/L` | 0.0001 | 1 |

Geometria ocupa `0 ≤ x ≤ L`, `-b/2 ≤ y ≤ b/2`, `-h/2 ≤ z ≤ h/2`. Impor `Ux=0` na face `x=0` e `Ux=δ` em `x=L`. Para remover modos rígidos sem impedir a contração lateral, usar os vértices reproduzíveis `PIN_A=(0,-b/2,-h/2)` e `PIN_B=(0,+b/2,-h/2)`: em `PIN_A`, fixar `Uy=Uz=0`; em `PIN_B`, fixar apenas `Uz=0`. Essas restrições são compatíveis com o campo afim de Poisson até translações rígidas laterais. Registrar coordenadas e rótulos de nó efetivamente usados no manifesto; validar estabilidade e ausência de sobre-restrição na execução.

O benchmark executado usa elementos sólidos quadráticos C3D10. O Gmsh exporta um INP de malha; o harness deriva os pinos por coordenadas, acrescenta os cartões de análise em um deck separado e o CalculiX executa esse deck. Foram conferidos tipo, conectividade, conjuntos, volume, orientação dos tetraedros de canto e nós midside afins. Isso verifica a configuração sintética testada, mas não uma importação CAD→malha nem identidade persistente de entidades CAD. O harness mantém também um baseline estruturado C3D8 separado; sua preparação não é um resultado de solver.

Configuração Gmsh usada na geração das malhas deste benchmark:

```geo
Mesh.ElementOrder = 2;
Mesh.SaveGroupsOfNodes = 1;
Mesh.SaveGroupsOfElements = -1000; // restringe os grupos de elementos exportados à dimensão 3
```

Criar grupo físico 3D para o sólido e grupos físicos separados nas extremidades. O Gmsh 4.15.2 emitiu C3D10 e os grupos de malha esperados nesta execução; o harness verificou o INP real antes de montar o deck separado. Os pinos foram resolvidos por coordenadas e não são grupos físicos exportados pelo Gmsh. Esse resultado local não transforma o comportamento do exportador em garantia geral para outros formatos, geometrias ou versões.

## Referências analíticas

Para elasticidade linear isotrópica em tração uniaxial uniforme:

```text
ε_xx = δ/L = 0.0001
σ_xx = E ε_xx = 21 MPa
R_x = E A δ/L = 2100 N
ε_yy = ε_zz = -ν ε_xx = -0.00003
```

A tensão axial e reação são observáveis principais. Comparar tensão média no terço central do volume, longe dos nós usados para remover modos rígidos. A contração lateral é verificação secundária. As referências decorrem da solução linear de barra; a malha 3D tem efeitos locais nas extremidades, então não avaliar tensões nodais máximas junto às restrições.

## Matriz e critérios propostos

Rodaram-se três malhas com tamanho característico `h_e = 5, 2.5, 1.25 mm`, mantendo material, condições, algoritmo e elemento. A convergência observada foi estável nos observáveis registrados; contagens e métricas estão no manifesto de execuções. Repetir a avaliação se formulação, geometria, condições ou versão mudarem.

Definições determinísticas das métricas:

1. `R_x = |Σ RF_x|` sobre todos os nós de `X_FIXED`, em N. Também registrar a resultante em `X_DISPLACED` e o desequilíbrio `|ΣRF_x(fixa)+ΣRF_x(móvel)|/2100`.
2. `σ̄_xx` é média ponderada pelo volume das amostras `S11` nos pontos de integração dentro de `L/3 ≤ x ≤ 2L/3`, incluindo toda a seção transversal: `Σ(S11_i · w_i · detJ_i)/Σ(w_i · detJ_i)`. Registrar número/origem dos pontos; valores extrapolados para nós não entram nesta métrica.
3. `ε̄_xx`, `ε̄_yy` e `ε̄_zz` usam a mesma região e ponderação de volume, nas componentes de deformação nos pontos de integração. As referências são `0.0001`, `-0.00003` e `-0.00003`.
4. Erro relativo percentual: `100·|resultado−referência|/|referência|`. Mudança entre malhas consecutivas, relativa à malha mais fina: `100·|valor_fino−valor_anterior|/|valor_fino|`.
5. Conferir equilíbrio de força, conectividade, tipo/orientação dos elementos e grupos preservados; registrar campos e contagens usados na extração.

Critérios candidatos aplicados nesta execução (não são limites universais do OCRE):

- erro relativo de reação axial ≤ 1%;
- erro relativo da tensão média no terço central ≤ 1%;
- diferença entre as duas malhas mais refinadas para reação e tensão média ≤ 1%;
- contração transversal compatível com `-ν ε_xx`, com erro ≤ 5%;
- ausência de modos rígidos, singularidade numérica, grupo vazio ou condição aplicada à entidade errada.

Os limites de 1% e 5% são metas deste benchmark, não tolerâncias recomendadas por documentação. As três malhas atenderam às métricas analíticas e a variação entre as duas mais finas foi 0% para reação e tensão. Isso não valida outros casos ou configurações. Se não forem atingidos em execuções futuras, investigar formulação, critérios de extração e convergência antes de alterá-los; registrar qualquer revisão e sua justificativa.

## Grupos e mapeamento

O manifesto derivado precisa identificar `VOLUME`, `X_FIXED`, `X_DISPLACED`, `PIN_A` e `PIN_B`. Para cada conjunto, registrar ID semântico, dimensão/tag(s) Gmsh quando aplicável, tipo de entidade, nome/tipo do conjunto no `.inp`, contagens esperada e observada, coordenadas de referência e hash do artefato de malha. Tags Gmsh são locais à malha, não identidade global. Validar no `.inp` que volume e faces foram mapeados para conjuntos nodais/elementares corretos; falhar antes do solver se algum grupo estiver vazio ou divergente.

## Evidências a preservar

- geometria de origem, parâmetros, revisão e hash;
- script CAD/Gmsh, versões e parâmetros de malha;
- malhas e manifesto de grupos para cada refinamento;
- `.inp` gerado, versão/binário/ambiente CalculiX, logs e arquivos nativos de saída;
- script determinístico de extração, tabela de métricas e hashes de todos os artefatos;
- unidades e referencial registrados, mais relatório de conversões e limitações.

## Limitações e decisão seguinte

Este caso não cobre flexão, plasticidade, contato, montagem, CAD importado, sincronização, cloud ou validação de segurança. A qualidade de forma foi medida, mas não há limiar de aceitação definido; schema EIR, replay/falhas e proveniência de CAD importado também não foram validados. O [manual do Gmsh 4.15.2](https://gmsh.info/doc/texinfo/gmsh.html#Mesh-options) documenta opções de grupos físicos e formatos; o mapeamento exato deve ser verificado no arquivo exportado. O [manual do CalculiX 2.23](https://www.dhondt.de/ccx_2.23.pdf) documenta C3D10, `*BOUNDARY`, `*NODE PRINT` com `RF`/`TOTALS=ONLY` e `*EL PRINT` com tensões nos pontos de integração. Além dos recursos documentais, esta configuração foi executada localmente; isso não prova compatibilidade geral do pipeline.
