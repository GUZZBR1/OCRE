# Proveniência local CAD→geometria→malha (experimental)

## Estado e limites

Este documento descreve um manifesto lateral experimental para evidência
reproduzível local. Ele não altera EIR v0.2, schemas, contratos nem os gates
normais. A única origem presente no benchmark atual é `synthetic_geo`: cada
`.geo` define uma primitiva `Box` do Gmsh. Isso não é CAD importado e não
demonstra leitura de STEP, OCCT, identidade persistente de faces ou arestas,
nem proveniência CAD→geometria importada.

O manifesto não transforma evidência parcial em prontidão. Referências locais
quebradas, hashes inválidos ou vínculos incompatíveis ficam incompletos e não
podem sustentar um gate normal aprovado. Não há conexão cloud, IAM real,
broker, banco de produção, importação STEP ou execução de software científico
para produzir este manifesto.

## Modelo da evidência

O sidecar versionado fica ao lado dos artefatos do benchmark em
`experiments/fea-reference/runs/cad_mesh_provenance.json`. Ele registra o ID e
SHA-256 do manifesto-fonte, revisões identificadas, a origem explicitamente
sintética, os arquivos `.geo`, as malhas e decks existentes com seus hashes,
versões de ferramenta disponíveis e IDs semânticos para grupos conhecidos.
URIs são caminhos POSIX relativos ao evidence root; caminhos absolutos,
travessia de diretório, symlinks que resolvem para fora da raiz e hashes que
não correspondem são rejeitados. Um symlink cujo alvo permanece dentro da
raiz pode ser lido, e seu destino resolvido é o arquivo cujo hash é conferido.

IDs como `volume`, `fixed_end` e `displaced_end` são rótulos semânticos
estáveis do caso. Tags numéricas ou índices topológicos são locais a uma
revisão e não são tratados como identidade entre revisões. Um grupo ausente,
duplicado ou sem artefato correspondente aparece como pendente/incompleto; o
manifesto não inventa um vínculo a partir de proximidade de tags.

## Métricas de malha registradas

O sidecar apenas copia métricas já presentes no manifesto preparado; não
executa Gmsh, CalculiX ou qualquer medição nova. Para cada malha existente,
registra contagem de nós/elementos, volume geométrico em mm³ e mean ratio do
tetraedro de canto (adimensional; 1 corresponde ao tetraedro equilateral),
com mínimo e média. O método implementado pelo harness usa os quatro nós de
canto, os seis comprimentos de aresta ao quadrado e o volume orientado:
`12 * (3V)^(2/3) / soma(l_aresta^2)`. O método e unidade são parte do registro.

Esse valor é uma métrica específica da representação C3D10 deste benchmark;
não é uma medida geral de qualidade de malha, nem define um limiar de
aceitação. O campo de limiar continua ausente. A evidência do harness já
versionada é reutilizada, sem regenerar geometria, malha, deck, manifesto do
solver ou saídas medidas.

## Verificação local

O auditor de evidência aceita o formato do sidecar como modo separado, usa
uma raiz local explícita e confere os hashes do manifesto, fontes e malhas,
além de comparar seus IDs, grupos e métricas copiadas com o manifesto
preparado. Referências ausentes ficam pendentes; dados contraditórios ficam
inválidos. Testes unitários cobrem sidecar válido, hash/URI inválida,
divergência entre fonte e malha, grupos ausentes/duplicados e origem
`synthetic_geo`. Esta verificação local não integra o sidecar aos gates EIR
nem remove as condições de evidência real exigidas pela prontidão normal.

## Próxima evidência necessária

Para fechar proveniência de CAD importado, ainda é necessário um artefato STEP
real e uma importação OCCT real, com identificador/hash da fonte, revisão,
transformação e versões registradas; identidade semântica rastreável para
geometria derivada; vínculos verificados entre a geometria importada, grupos e
malhas; e testes de revisão/alterações topológicas. Essa etapa continua
pendente. Nenhum resultado neste experimento pode ser descrito como importação
CAD comprovada ou como prontidão normal aprovada.
