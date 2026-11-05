# Simulador interno leve — tradutor visual "em traços" (SÓ DESENHO, sem código)

Data: 2026-11-05. Status: **desenho registrado; NÃO implementar
agora**. Este documento não cria código, não muda nenhuma dependência
e não é importado por nada da Fase 0.

> Garantia explícita: **nada da Fase 0 importa ou depende disso.**
> É pós-F4. O escritor DXF, o runner do vectorize e o gate LibreCAD
> funcionam sem este simulador, não o referenciam e continuam
> funcionando se ele nunca existir.

> Veredito do Senado: **em avaliação — ver
> RELATORIO-SENADO-VISUAL.md na corrida do Senado**. O Senado está
> avaliando o tema em paralelo (complemento 2); nenhum veredito é
> registrado aqui e nenhum é inventado.

## 1. O que é

Um simulador interno próprio, **leve, só de tradução visual — não um
motor de física** (ideia do usuário, 2026-11-05 ~15:52 -03, ainda não
decidida/implementada).

Ele pega todos os arquivos que os softwares fizeram (arquivos de
simulação, de teste etc.) e joga dentro desse simulador interno, "em
traços": ele transcreve a parte de documento/arquivo (o que é cada
coisa) e transforma tudo numa imagem, numa simulação — **só TRADUZ os
arquivos para uma simulação visual**. Não calcula física; os números
continuam vindo dos solvers.

A parte visual é fácil justamente porque ele não pega a simulação ao
vivo da ferramenta externa — só os arquivos de saída dela.

## 2. Onde pluga

Depois dos resultados da F4, antes do viewer:

```
documento draw.paper.v1 → F4 simulação sob demanda (solver externo)
        → arquivos de resultado (.vtu, .frd, .ex2, CSV, envelopes)
        → [SIMULADOR INTERNO LEVE: traduz arquivos → imagem/animação]
        → viewer (apresentação ao usuário)
```

Ele é um passo de **apresentação**, não de análise: entra quando o
resultado já existe e já foi validado, e sai antes do usuário ver. Não
pluga no loop do Draw, não pluga no solver, não pluga no documento —
pluga só nos arquivos de resultado.

## 3. Formatos de entrada e saída

Entrada — arquivos de resultado que os softwares já produzem
(a lista exata depende do que a F4 adotar; estes são os formatos
citados na pesquisa):

- `.vtu` (VTK Unstructured Grid — ex.: saída de solvers FEA);
- `.frd` (CalculiX — resultado de análise);
- `.ex2` (Exodus II — ex.: saída de solvers de elementos finitos);
- séries CSV (séries temporais / tabelas de resultado);
- envelopes (envoltórias de resultado — máx/mín por ponto).

Saída — imagem e/ou animação:

- imagem estática (PNG) do resultado traduzido;
- animação (sequência de imagens / vídeo curto) quando a entrada é
  uma série temporal ou um envelope animado.

A tradução é **formato → traços → imagem**: ler o arquivo, transcrever
o que cada coisa é (malha, campo, valor por ponto/célula), desenhar
"em traços" e renderizar. Nenhum número é recalculado no caminho.

## 4. Relação com o pesquisado — PyVista e viewer Three.js

Registrado como **recomendação, não decisão** (o Senado avalia em
paralelo):

- **PyVista (job batch `.vtu` → `.png`)**: o simulador interno
  **complementa** o PyVista, não o substitui — pelo menos no começo.
  O PyVista já faz batch `.vtu` → `.png` com um motor testado; o
  simulador interno faria a mesma tradução, mas "em traços"
  (linguagem visual do Draw) e lendo também os outros formatos
  (`.frd`, `.ex2`, CSV, envelopes) num só lugar. Recomendação: usar
  o PyVista primeiro (é o caminho que já existe e já foi pesquisado);
  o simulador interno entra depois, como camada de apresentação
  própria por cima — ou no lugar do PyVista só se provar que traduz
  os mesmos arquivos com a mesma fidelidade e menos custo.
- **Viewer Three.js (`.glb` + `COLOR_0`)**: o simulador interno
  **complementa** o viewer, não o substitui. O viewer Three.js é o
  lugar onde o usuário **explora** o resultado (orbita, zoom,
  inspeciona); o simulador é o lugar onde o resultado vira **imagem
  ou animação em traços** (apresentação fixa, compartilhável). Um não
  faz o trabalho do outro: o viewer precisa de `.glb` interativo, o
  simulador produz imagem/animação. Se um dia o simulador gerar o
  `.glb` que o viewer lê, isso é uma integração futura — não é o
  desenho de agora.

Em resumo: o simulador não substitui nenhuma peça pesquisada; ele
acrescenta um passo de apresentação depois delas.

## 5. O estilo "em traços" como requisito de apresentação

A linguagem visual do Draw é o traço — e o simulador fala essa
língua. Requisito de apresentação (não de cálculo):

- O resultado aparece desenhado "em traços", como se tivesse sido
  traçado no papel: contornos, hachuras e setas no estilo do Draw,
  não um render fotorrealista.
- A escala visual é legível: o que é grande no resultado é grande no
  desenho; o que é detalhe fica como detalhe.
- Cores e espessuras codificam o campo (ex.: tensão, temperatura)
  com legenda explícita — nunca só "vermelho = ruim" sem dizer a
  unidade e o valor.
- A imagem carrega proveniência visível ou anexa: qual arquivo de
  resultado a originou, qual solver, qual revisão do documento.

O estilo é requisito porque é ele que faz o resultado ser legível
para quem desenhou no papel — mas estilo nenhum muda o número.

## 6. Regra dura — render nunca é resultado de análise

**O número vem do solver; o simulador só mostra.**

- O simulador nunca calcula, interpola, extrapola ou "suaviza"
  valores: ele desenha exatamente os valores que o arquivo de
  resultado traz.
- Nenhuma imagem ou animação do simulador é apresentada como
  resultado de análise: ela é apresentada como **visualização de um
  resultado**, com a fonte (solver, arquivo, revisão) identificada.
- Se o arquivo de resultado não existe, está incompleto ou falhou na
  validação, o simulador não desenha nada — recusa fail-closed, nunca
  inventa um desenho plausível.
- A validação do resultado (gates G4.1–G4.4 da Fase 4) acontece
  **antes** do simulador; o simulador não valida e não aprova nada.

## 7. Ordem de adoção da visualização — muda ou só acrescenta?

**Só acrescenta um passo depois delas; não muda a ordem.**

A ordem pesquisada continua valendo:

1. **PyVista primeiro** — job batch `.vtu` → `.png`; é o caminho
   mais curto para ter imagem de resultado.
2. **Viewer Three.js** — `.glb` + `COLOR_0` para exploração
   interativa do resultado.
3. **Blender** — render de apresentação quando for preciso (depois
   das duas anteriores, não antes).
4. **Simulador interno leve** — depois de todas: traduz os arquivos
   de resultado (`.vtu`, `.frd`, `.ex2`, CSV, envelopes) para
   imagem/animação "em traços", por cima do que as etapas anteriores
   já provaram.

O simulador não pula a fila: ele só existe depois que a F4 produz
resultados reais e que a visualização básica (PyVista/viewer) já
funciona. Se a F4 não existir, o simulador não tem entrada — e não
há nada para traduzir.

## 8. O que NÃO é este simulador

- Não é um motor de física: não resolve equações, não calcula
  tensão, temperatura ou deslocamento.
- Não é um substituto do solver: os números continuam vindo dos
  solvers externos (CalculiX, Code_Aster, MuJoCo, Newton — conforme
  a F4 decidir).
- Não é um substituto do PyVista, do viewer Three.js ou do Blender.
- Não é validação: não aprova resultado, não roda gate, não libera
  execução.
- Não é parte da Fase 0 nem da Fase 4: é pós-F4, e nenhum arquivo
  dessas fases o importa, referencia ou depende dele.

## 9. Próximos passos (quando — e só quando — a F4 existir)

1. Aguardar o veredito do Senado (RELATORIO-SENADO-VISUAL.md).
2. Decisão do usuário: construir o simulador interno ou ficar só com
   PyVista + viewer + Blender.
3. Se construir: fechar a lista de formatos de entrada que a F4
   realmente produz (`.vtu` primeiro; `.frd`/`.ex2`/CSV/envelopes
   conforme o solver adotado).
4. Definir o contrato de tradução (arquivo de resultado → traços →
   imagem) com proveniência obrigatória em cada saída.
5. Só então: implementação, em branch própria, com aprovação
   específica — depois da F4, nunca antes.
