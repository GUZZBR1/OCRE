# Agente de material — contrato

Papel: `MaterialAdvisor` (`backend/ocre_core/draw/ai/interfaces.py`).
Status: **contrato apenas** — nenhum modelo implementa este papel hoje.

## O que faz

Recebe um `AIContext` e devolve uma lista de `MaterialAdvice`
(`stroke_id`, `material`, `rationale`) — um material **proposto** por
traço estável, a partir do contexto do projeto (o que o desenho é,
o que as decisões registradas dizem, o que o documento carrega).

## Entrada

- Documento validado, na revisão que o contexto carrega, + sidecar e
  decisões registradas.

## Saída

- Lista de propostas de material. Lista vazia quando não há base
  para propor — nunca inventar material.

## O que pode

- Propor material com justificativa (`rationale`) por traço estável.
- Passar o contexto adiante: o material proposto é informação para
  as plataformas de teste de material, não uma ordem.

## O que NÃO pode

- Escrever material no documento, num job ou numa simulação.
- Decidir material sozinho: "aquele pilar tem de ser metal ou
  concreto" é uma proposta, não uma decisão.
- Usar base de conhecimento que não existe: **não há base de
  conhecimento codificada** (parede→tijolo, pilar→concreto etc. é
  decisão pendente do usuário). Sem base versionada, o advisor não
  tem sobre o que propor com confiança — e propõe vazio em vez de
  inventar.

## Trava de confirmação humana (G3.2)

Toda `MaterialAdvice` nasce como proposta `pending` do tipo
`material-advice` (ver `proposals.py`). Nenhum material entra em
documento, job ou simulação sem confirmação humana explícita.
