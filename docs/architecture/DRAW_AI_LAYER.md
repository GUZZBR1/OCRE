# Camada de IA — registro arquitetural (SÓ DESENHO, sem código)

Data: 2026-11-05. Status: **desenho aprovado para registro; NÃO
implementar agora**. Este documento não cria código, não muda nenhuma
dependência e não é importado por nada da Fase 0.

> Garantia explícita: **nada da Fase 0 importa ou depende desta
> camada.** O escritor DXF (`dxf_export.py`), o runner do vectorize
> (`vectorize_runner.py`) e o gate LibreCAD (`librecad_gate.py`)
> funcionam sem ela, não a referenciam e continuam funcionando se ela
> nunca existir. A camada de IA é Fase 3 (semântica), depois do
> documento `draw.paper.v1` (Fase 1) — e só existe sobre o documento
> já validado.

## 1. O que é

Uma camada de inteligência artificial **por cima de todas as
integrações** (adendo do usuário, 2026-11-05 ~15:46 -03), que gerencia
as integrações e faz as conversões que o software determinístico não
consegue. É a camada semântica da Fase 3 do plano
(`PLANO-INTEGRACAO-OCRE-DRAW.md` §3/§4).

O trabalho concreto dela, refinado pelo usuário em 2026-11-05 ~15:50
-03, é **checagem de coerência**: quando um arquivo muda, a IA olha
para ele e verifica se ele continua coerente; se não estiver, roda o
ciclo padrão até ficar coerente — ou a própria IA o altera para
torná-lo coerente. É assim que a alucinação se reduz: a IA não decide
sozinha o que o desenho significa; ela verifica coerência e propõe
correção, e a correção passa pelo ciclo padrão (validação
determinística + confirmação humana quando for semântica).

Exemplo canônico do usuário: um arquivo deve enviar só JPEG, mas envia
PDF por engano. Com o software determinístico fazendo a conversão, a
chance disso acontecer já é muito menor; a IA entra como a checagem de
coerência que flagra o desvio quando ele ocorre.

## 2. Onde pluga

Entre o **documento `draw.paper.v1` validado** (Fase 1) e os
**adapters** (DXF, STEP, FMU, CAD ao vivo, simulação). Nunca antes do
documento existir e nunca no lugar do validador.

```
PNG camada confirmada → vectorize → draw.paper.v1 (validado, mestre)
        → [CAMADA DE IA: propõe semântica / checa coerência]
        → adapters (DXF R2000, STEP, FMU, CAD ao vivo, simulação)
```

Consome **eventos do documento**, nunca o PNG cru nem o estado
interno do Draw:

- `stroke.stable` — traço confirmado; candidato a classificação
  semântica (parede, porta, janela, cota, texto… — domínio ainda
  aberto, decisão do usuário pendente).
- `stroke.erased` — tombstone; a IA nunca ressuscita um traço apagado
  nem propõe classe para ele.
- revisões do documento (`revision` +1 por mutação) — a IA reage a
  revisões, não reescreve o passado; cada proposta carrega a revisão
  que a originou.

Enquanto a pessoa desenha, a IA entende semântica ao vivo (pilar vs.
viga; parede → diz ao programa qual tijolo e cimento usar; passa o
contexto de material para as plataformas de teste de material — ex.:
aquele pilar tem de ser metal ou concreto). Essa compreensão ao vivo é
**proposta**, nunca decisão.

## 3. Regra determinístico-primeiro

Conversões **primeiro por software determinístico** (mudança de
unidade, correção de formato, conversão de arquivo). O que o software
não consegue fazer escala para a IA. A ordem é fixa:

1. Software determinístico tenta (regra, tabela, conversão de formato).
2. Se não consegue, escala para a IA.
3. A IA propõe (semântica ou correção de coerência).
4. A proposta passa pelo ciclo padrão: validação determinística
   (schema, unidades, limites) + confirmação humana quando for
   semântica (G3.2).

A IA nunca substitui uma conversão que o software faz — ela é o
segundo degrau, não o primeiro. Se o arquivo X pode virar o arquivo Y
via software, vira via software.

## 4. Regra de confirmação humana (G3.2)

**Inferência propõe, nunca decide. Zero classes sem confirmação
humana explícita.** Concretamente:

- Toda classe semântica proposta pela IA fica como `proposed`
  (nunca `confirmed`) até um humano confirmar.
- Nada é auto-promovido: não existe caminho de código que leve
  `proposed → confirmed` sem um ato humano registrado (quem, quando,
  origem — G3.3).
- A confirmação é por classe e por traço; confirmar um desenho não
  confirma o próximo.
- A IA pode corrigir coerência de **formato** (ex.: o arquivo devia
  ser JPEG e veio PDF) pelo ciclo padrão, mas a correção de
  **significado** (isto é uma parede) sempre espera o humano.

## 5. Modelo próprio vs. LLM pronto — decisão aberta

Duas rotas, ainda sem decisão (o Senado avalia; o usuário decide):

| | Modelo próprio (treinar/construir) | LLM pronto (off-the-shelf) |
|---|---|---|
| Controle | total sobre dados e comportamento | depende do fornecedor |
| Custo | treino + infra; previsível após pronto | por uso/chamada; custo por desenho em tempo real é risco aberto |
| Latência | local, sob nosso controle | rede; depende de conta e nuvem |
| Conta/nuvem | pode rodar local, sem conta externa | exige conta, possivelmente nuvem; dados do desenho saem da máquina |
| Escala (10 mil usuários) | distribuir modelo é um problema de licença + peso | distribuir é um problema de custo por usuário + conta |

Questões que a decisão precisa responder antes de qualquer
implementação: custo por desenho em tempo real, latência aceitável
enquanto a pessoa desenha, se os dados do desenho podem sair da
máquina (conta/nuvem), e se a licença permite distribuir para 10 mil
usuários sem armadilha de per-seat/conta/nuvem.

Nenhuma das duas rotas é assumida aqui; nenhuma é implementada aqui.

## 6. Base de conhecimento necessária

A IA precisa de um **banco de elementos e materiais** para propor
semântica com sentido (o que é uma parede, o que é um pilar, que
materiais existem, o que cada um exige). Em aberto, a decidir:

- Quem alimenta o banco (curadoria humana? extração de normas?
  contribuição dos usuários?).
- Qual a fonte de verdade (normas? catálogos de fabricante? dados do
  próprio uso?).
- Como o banco é versionado e auditado (uma classe proposta a partir
  de um banco desatualizado é uma proposta errada com aparência de
  certa).

Sem essa base, a camada não tem sobre o que propor — por isso ela é
Fase 3, não Fase 0.

## 7. Riscos

- **Alucinação semântica:** a IA classificar um traço como parede
  quando é uma cota, ou inventar um material. Mitigação: checagem de
  coerência + confirmação humana obrigatória (G3.2) + o ciclo padrão
  como rede de segurança. Nenhuma classe entra no documento sem
  humano.
- **Custo por desenho em tempo real:** se cada traço custar uma
  chamada de LLM, o custo escala com o desenho, não com o usuário.
  Mitigação: decidir o modelo antes de implementar; medir custo por
  desenho na Fase 3, nunca assumir que é desprezível.
- **Dependência de conta/nuvem:** se a rota for LLM pronto, o desenho
  ao vivo depende de rede e de conta externa. Mitigação: modo
  degradado definido — sem IA, o documento continua válido e os
  adapters continuam funcionando; a IA é acréscimo, não pré-requisito.
- **Deriva do banco de conhecimento:** banco desatualizado gera
  propostas sistematicamente erradas. Mitigação: versionamento do
  banco e registro de qual versão originou cada proposta.
- **Falsa sensação de decisão:** uma proposta bem formatada parece
  uma decisão. Mitigação: o estado `proposed` é visível e distinto no
  documento e no sidecar; nada que a IA propõe se parece com algo
  confirmado.

## 8. O que NÃO é esta camada

- Não é um substituto do validador do documento (o validador é
  determinístico e continua sendo a autoridade sobre a forma).
- Não é um substituto do software determinístico (conversões de
  formato e unidade continuam em software).
- Não é um decisor: não confirma classes, não apaga traços, não muda
  revisão, não libera execução.
- Não é parte da Fase 0: nenhum arquivo da Fase 0 a importa, a
  referencia ou depende dela.

## 9. Próximos passos (quando — e só quando — a Fase 3 chegar)

1. Decisão do usuário: modelo próprio vs. LLM pronto (com custo,
   latência, conta/nuvem e escala avaliados).
2. Decisão do usuário: domínio semântico (quais classes).
3. Definição de quem alimenta o banco de elementos/materiais e como
   ele é versionado.
4. Desenho do protocolo de proposta → confirmação humana (estados
   `proposed|confirmed|rejected`, com quem/quando/origem no sidecar).
5. Só então: implementação, em branch própria, com aprovação
   específica.
