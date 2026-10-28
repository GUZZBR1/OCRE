# Schemas do EIR

O diretório mantém contratos estruturais versionados. Os schemas v0.1 continuam experimentais e imutáveis. O contrato e os schemas v0.2.0 foram aprovados após revisão independente e estão congelados; seus digests estão em [`schema-lock-0.2.0.json`](schema-lock-0.2.0.json) e cobertos pelos testes. Mudanças incompatíveis exigem nova versão.

## EIR v0.1 — experimental, imutável

- [`0.1.0.schema.json`](0.1.0.schema.json): forma mínima do caso axial inicial.
- [`mesh-group-manifest-0.1.0.schema.json`](mesh-group-manifest-0.1.0.schema.json): manifesto de grupos experimental.
- [Exemplo axial v0.1.0](examples/0.1.0/eir.json): fixture usada para verificar que a versão legada continua validável.

## EIR v0.2.0 — congelado

- [`0.2.0.schema.json`](0.2.0.schema.json): EIR versionado, regiões geométricas, atribuições materiais e frames.
- [`input-bundle-0.2.0.schema.json`](input-bundle-0.2.0.schema.json): fonte única de URI, media type, formato e hash dos bytes de cada artefato.
- [`mesh-group-manifest-0.2.0.schema.json`](mesh-group-manifest-0.2.0.schema.json): referências por `artifact_id`; não repete URI, formato nem hash.
- [`schema-lock-0.2.0.json`](schema-lock-0.2.0.json): hashes SHA-256 dos três schemas congelados.
- [Exemplos v0.2.0](examples/0.2.0/): um documento positivo para cada schema.
- [Perfis iniciais](profiles/): perfil axial estrutural e allowlist UCUM versionada que o fingerprint identifica por hash.

Os SHA-256 nos exemplos v0.2 são sentinelas estruturais, não hashes de arquivos existentes. Não use esses exemplos como bundle executável; testes semânticos deverão validar os digests contra bytes reais.

Os três schemas declaram Draft 2020-12. A verificação usa JSON Schema apenas para estrutura e tipos; referências cruzadas, unicidade semântica de IDs, confinamento de URI, hashes dos bytes, UCUM, norma de quaternion, cobertura completa e não sobreposta do domínio por regiões materiais e prontidão para execução pertencem ao validador semântico.

## Migração

A migração v0.1→v0.2 é explícita e preserva a entrada original. Rascunhos podem conter `null` ou relações ainda não resolvidas, mas precisam declarar `document_state: "draft"` e apontar pendências no relatório de migração. `document_state: "complete"` exige valores estruturais resolvidos; ainda assim, só pré-flight semântico v0.2 pode liberar execução. Consulte [o contrato v0.2](../../docs/architecture/EIR_V02_CONTRACT.md).

## Testes locais

`python3 -m unittest discover -s tests/eir -v` valida os três schemas contra Draft 2020-12, exemplos positivos/negativos, um rascunho com proveniência e pendências, hashes fixados dos schemas/fixture v0.1 e o framing/fingerprint golden vector. O vetor não substitui testes de interoperabilidade multi-linguagem. Requer `jsonschema` compatível com Draft 2020-12 já disponível no ambiente; nenhuma dependência foi adicionada ao projeto.
