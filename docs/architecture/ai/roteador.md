# Agente roteador — contrato

Papel: `route()` (`backend/ocre_core/draw/ai/router.py`).
Status: **implementado como mecânica determinística** — sem modelo,
sem rede, sem chamada externa.

## O que faz

Decide para onde vai um item de conversão, numa ordem fixa:

1. Conversores determinísticos tentam, na ordem dada (regra, tabela,
   conversão de formato — ex.: PDF → JPEG por software).
2. Se nenhum resolve, e há modelo configurado e registrado, o item
   escala para a IA (`escalated-to-ai`).
3. Se nenhum resolve e não há modelo, o item fica `unresolved`
   ("não resolvido") — nunca inventado, nunca resolvido por palpite.

## Entrada

- `item`: dict opaco descrevendo a conversão pedida.
- `deterministic_converters`: sequência de callables `fn(item) ->
  resultado | None` — resultado significa "resolvido", `None`
  significa "não consigo".
- `config` (`AIConfig`) e `registry` (`ModelRegistry`) opcionais;
  por padrão usam os compartilhados (vazios hoje).

## Saída

- `RouteResult(outcome, detail, deterministic_result)`:
  - `deterministic` — resolvido por software; a IA nem é consultada.
  - `escalated-to-ai` — escalado; quem chama é que invoca o modelo
    via registro e transforma a resposta em `Proposal`.
  - `unresolved` — sem conversor e sem modelo; o item fica pendente.

## O que pode

- Tentar conversores determinísticos na ordem.
- Dizer para onde o item vai, com o motivo em `detail`.

## O que NÃO pode

- Chamar modelo nenhum (o roteador não invoca a IA; ele só decide).
- Resolver por palpite quando não há conversor nem modelo.
- Furar a ordem: a IA nunca é o primeiro degrau — se o software
  resolve, a IA não entra.

## Trava de confirmação humana (G3.2)

O roteador não cria proposta nem aplica nada. O que escala para a
IA volta como `Proposal` `pending`, e só se aplica depois de
confirmação humana (`proposals.py`).
