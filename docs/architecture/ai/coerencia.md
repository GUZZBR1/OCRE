# Agente de coerência — contrato

Papel: `CoherenceChecker` (`backend/ocre_core/draw/ai/interfaces.py`).
Status: **contrato apenas** — nenhum modelo implementa este papel hoje.

## O que faz

Recebe um `AIContext` e devolve uma lista de `Incoherence` (`kind`,
`detail`, `stroke_id` opcional, `evidence` opcional) — os desvios de
coerência encontrados. **Reporta, nunca corrige.**

Exemplo canônico: um arquivo deve enviar só JPEG, mas envia PDF por
engano — o checker flagra `kind: "wrong-format"`, com a evidência.

## Entrada

- Documento validado, na revisão que o contexto carrega, + sidecar e
  decisões registradas.

## Saída

- Lista de incoerências encontradas. Lista vazia quando está coerente.
- Cada incoerência vira, no máximo, uma proposta `pending` do tipo
  `coherence-correction` (ver `proposals.py`).

## O que pode

- Ler o documento, o sidecar e as decisões.
- Apontar incoerências com evidência.

## O que NÃO pode

- Corrigir nada: nenhum arquivo é alterado, nenhum documento é
  mutado, nenhuma proposta é aplicada por este agente.
- Decidir sozinho que algo "está certo" — coerência é um achado, não
  um veredicto.

## Trava de confirmação humana (G3.2)

Correção automática sem confirmação é **proibida** até existir um
protocolo de correção (porta G3.2). A correção de **formato** passa
pelo ciclo padrão (validação determinística + confirmação humana
quando for semântica); a correção de **significado** sempre espera o
humano. Uma proposta de correção só se aplica depois de confirmada
(`apply_proposal`, em `proposals.py`), e só contra a revisão que a
originou.
