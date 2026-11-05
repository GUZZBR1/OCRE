# Agente classificador — contrato

Papel: `SemanticClassifier` (`backend/ocre_core/draw/ai/interfaces.py`).
Status: **contrato apenas** — nenhum modelo implementa este papel hoje.

## O que faz

Recebe um `AIContext` (documento `draw.paper.v1` validado + sidecar +
decisões registradas) e devolve uma lista de `Classification`
(`stroke_id`, `label`, `confidence`, `rationale`) — uma **proposta**
de classe semântica por traço estável.

## Entrada

- Documento validado, na revisão que o contexto carrega.
- Só traços com `status: "stable"` são classificáveis (o contexto já
  filtra: `AIContext.stable_strokes()`).

## Saída

- Lista de propostas. Cada proposta carrega a revisão do documento.
- Lista vazia quando nada pode ser classificado — nunca inventar.

## O que pode

- Classificar traços estáveis a partir do documento e do contexto.
- Dar um `confidence` e um `rationale` por proposta.

## O que NÃO pode

- Classificar traço apagado (tombstone é final) nem traço `pending`.
- Escrever no documento, mudar revisão, apagar traço, confirmar nada.
- Inventar classe fora do domínio semântico — o domínio (parede,
  porta, janela, cota, texto...) é decisão pendente do usuário (D4);
  sem domínio fechado, o classificador só propõe rótulos livres e a
  confirmação humana é quem valida.

## Trava de confirmação humana (G3.2)

Toda `Classification` nasce como proposta `pending` (ver
`proposals.py`). Nenhuma classe entra no documento sem confirmação
humana explícita, por classe e por traço. Confirmar um desenho não
confirma o próximo.
