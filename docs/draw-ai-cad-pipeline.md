# AI assistido no pipeline CAD beta

O caminho de IA é opcional e recebe um `ModelAdapter` através de
`--ai-adapter-factory modulo:funcao`. A função deve devolver o adapter já
implementado pelo provider; o CLI não escolhe modelo, não lê credenciais e não
faz chamadas de rede por conta própria. O adapter pode classificar traços,
aconselhar material e observar conversões DXF/STEP. Classificações e materiais
são propostas, nunca alterações no documento mestre.

## Duas passagens

1. Execute `beta-run` com `--cad-only`, `--ai-adapter-factory` e um
   `--proposal-file`. A primeira passagem deve criar `export/document.json`,
   registrar cada proposta como `pending` no ledger e terminar bloqueada para
   confirmação humana.
2. Revise o documento e o ledger. Confirme apenas os IDs aprovados com
   `--document export/document.json --proposal-file propostas.json
   --confirm-proposal ID --confirmed-by NOME`. A confirmação se refere à mesma
   revisão e SHA-256; uma divergência exige gerar novas propostas. O material
   passado por `--material` sempre prevalece sobre qualquer sugestão.

Um ledger confirmado deve permitir o segundo passo sem carregar o provider:
o CLI consome a decisão humana persistida, verifica documento/revisão/hash e
continua até CAD. `--reject-proposal ID` registra rejeição humana. Repetir uma
confirmação já registrada deve ser idempotente.

## Observação e publicação

O observador recebe artefatos derivados e devolve `clear`, `observed`,
`unavailable` ou `confirmed-error`. Advisories e indisponibilidade do provider
não equivalem a erro confirmado. Apenas `confirmed-error`, sustentado por
checagem estrutural independente, pode interromper publicação/uso do DXF ou
STEP. Os arquivos PNG e `draw.paper.v1` originais permanecem imutáveis.

`--cad-only` termina após a etapa de STEP; não executa FEA, física ou
visualização. Só deve anunciar e deixar no caminho de publicação os artefatos
verificados desta execução. A geometria suportada pelo beta permanece limitada
a perfis fechados e estáveis extrudados; não há inferência de geometria de
conjunto, restrições paramétricas ou semântica de engenharia.

## Exemplo de uso

O exemplo abaixo usa um adapter local `my_provider:create_adapter`. O SHA do
vectorizer deve ser calculado a partir do arquivo que será executado.

```bash
export PYTHONPATH=backend
VECTORIZE_SHA=$(sha256sum /caminho/draw/src/draw/exporters/vectorize.py | cut -d' ' -f1)
python -m ocre_core.draw.cli beta-run \
  --png canvas.png \
  --vectorize-script /caminho/draw/src/draw/exporters/vectorize.py \
  --vectorize-sha256 "$VECTORIZE_SHA" \
  --out-dir build/cad \
  --origin upstream-canvas-png \
  --draw-repo /caminho/draw \
  --draw-commit 0123456789abcdef0123456789abcdef01234567 \
  --thickness-mm 2 \
  --ai-adapter-factory my_provider:create_adapter \
  --proposal-file build/cad/proposals.json \
  --cad-only

python -m ocre_core.draw.cli beta-run \
  --png canvas.png \
  --vectorize-script /caminho/draw/src/draw/exporters/vectorize.py \
  --vectorize-sha256 "$VECTORIZE_SHA" \
  --out-dir build/cad \
  --origin upstream-canvas-png \
  --draw-repo /caminho/draw \
  --draw-commit 0123456789abcdef0123456789abcdef01234567 \
  --thickness-mm 2 \
  --document build/cad/export/document.json \
  --proposal-file build/cad/proposals.json \
  --confirm-proposal ID_DA_PROPOSTA \
  --confirmed-by "Nome da pessoa" \
  --cad-only
```

O adapter é necessário somente na primeira passagem; o ledger confirmado
permite a segunda execução sem provider. O relatório da primeira execução
registra `observed` quando o adapter retorna advisory. Sem provider, DXF/STEP
podem registrar `unavailable`; checagem estrutural determinística continua
ativa e erro confirmado nela bloqueia publicação.

O STEP inclui a designação de material como atributo descritivo XCAF.
O leitor STEP do OCCT recupera o nome. Propriedades físicas, como densidade,
ficam ausentes. O manifesto vincula material, IDs de traços e geometria ao
SHA-256 do STEP. Use `load_step_material_manifest` para ler o manifesto e
validar o vínculo em integrações que usam esse arquivo.
