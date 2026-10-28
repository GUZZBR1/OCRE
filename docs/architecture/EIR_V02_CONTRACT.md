# Contrato do EIR v0.2

**Estado:** contrato e schemas v0.2.0 congelados após aprovação do responsável e revisão independente. Digests dos três schemas em [`schema-lock-0.2.0.json`](../../schemas/eir/schema-lock-0.2.0.json). O validador e o pré-flight têm implementação local; OCRE-AF/1 ainda bloqueia `ready`, e a ferramenta de migração segue pendente. Mudanças incompatíveis exigem uma nova versão de schema/contrato.
**Escopo:** fechar as relações necessárias ao pré-flight semântico do primeiro perfil estrutural. Não implementa o validador nem libera o executor.

Este documento complementa [EIR_MINIMUM.md](EIR_MINIMUM.md), [EIR_VALIDATION_ARCHITECTURE.md](EIR_VALIDATION_ARCHITECTURE.md) e [ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md). Onde houver divergência, esta proposta define o alvo v0.2; EIR/schema v0.1 permanece imutável.

## Colocação no produto (semântica do EIR não depende do local de execução)

O EIR v0.2 e o `InputBundle` são portáveis e não codificam caminhos de máquina nem exigem que um motor esteja instalado no cliente. No produto OCRE, o Core Cloud executa a validação autoritativa e `execution_preflight`; somente após `ready` pode despachar o bundle imutável a um Scientific Worker para OCCT complexo, Gmsh, CalculiX ou outro adaptador habilitado. O cliente mantém UI/CAD interativo leve, cache e sincronização; qualquer validação offline é informativa e não libera solver. Execução local avançada é uma opção futura e não muda o contrato EIR.

## Decisões propostas

| Tema | Regra v0.2 proposta |
|---|---|
| Escopo das referências | Geometrias, materiais, frames e casos são entidades da `revision_id` declarada; referências não resolvem para “latest”. |
| Material | Cada `geometry_ref` representa uma região homogênea segundo as propriedades materiais exigidas pelo perfil estrutural v0.2. Uma geometria pode conter várias regiões, cada uma identificada separadamente e com uma atribuição material. |
| Frames | Registro explícito de frames estáticos, com uma raiz e grafo acíclico. Transformações têm convenção e unidades definidas abaixo. |
| Manifesto de artefatos | O catálogo é o `immutable_input_manifest` externo do `InputBundle` do adaptador. IDs de artefato são únicos dentro do bundle. |
| `group_manifest_ref` | ID opaco que resolve apenas no catálogo de artefatos do bundle, nunca caminho ou nome inferido. |
| Hashes | Hashes de bytes de artefatos obrigatórios são exigidos e verificados em `execution_preflight`. `revision.manifest_sha256` pode continuar `null` até a canonicalização ser definida. |
| Versionamento | Schemas v0.2 distintos; v0.1 continua válido segundo seu schema e não é promovido silenciosamente para execução. |

## Identidade e vínculos

1. O documento EIR seleciona exatamente um `revision_id`. Todas as entidades e referências de geometria, material, frame, caso e grupo usadas por ele pertencem a essa revisão.
2. IDs são únicos no namespace da entidade e da revisão correspondente. Duplicatas são erro; referências ausentes ou para outra revisão são erro.
3. `geometry_refs` identifica todas as regiões que compõem o domínio completo analisado pelo caso. Cada região é homogênea quanto às propriedades materiais exigidas pelo perfil e tem `geometry_id` estável na revisão; uma peça pode conter várias regiões e, portanto, várias referências. IDs não podem ser tags locais da malha ou do solver. As regiões devem cobrir o domínio analisado sem lacunas nem sobreposições; exclusões parciais não são suportadas pelo perfil v0.2. Cada região incluída deve ter exatamente uma atribuição material.
4. `material_assignments` contém exatamente uma atribuição para cada `geometry_ref` do caso. A geometria não pode aparecer duas vezes, não pode faltar atribuição e a atribuição deve apontar a um `material_id` existente na revisão.
5. Para preservar o campo solicitado `material_refs` sem permitir divergência, ele deve ser exatamente o conjunto sem duplicatas dos `material_id` presentes em `material_assignments`. Implementações podem derivá-lo na preparação, mas a instância EIR v0.2 deve obedecer à igualdade.
6. Regiões distintas de uma mesma peça devem ter `geometry_id` distintos. Mapeamentos muitos-para-muitos entre região e material ficam fora do v0.2; exigirão uma extensão explícita baseada em seleções persistentes de geometria, não identificadores de malha.

Essa política restringe o primeiro perfil, não estabelece uma ontologia geral de materiais. Geometrias da revisão não usadas por um caso podem existir, mas não são entradas desse caso e não devem ser inferidas pelo validador.

## Frames estáticos

O documento EIR v0.2 contém um registro `frames[]`. Cada frame inclui `frame_id`, `parent_frame_id` (`null` apenas para a raiz), `translation` como quantidade `{ "value": [x,y,z], "unit": "..." }`, e `rotation_xyzw` como quaternion `[x,y,z,w]`.

- Há exatamente uma raiz. Ela usa translação zero e quaternion identidade.
- O grafo tem IDs únicos, todos os pais resolvem e não há ciclos.
- Frames são destros, com eixos cartesianos X/Y/Z; frames dinâmicos não são aceitos.
- A transformação `T_parent_child` leva coordenadas expressas no frame filho para coordenadas no frame pai. A translação é a origem do filho expressa no pai; o quaternion XYZW representa a orientação dos eixos do filho no pai, usando quaternion unitário e rotação ativa de vetores coluna.
- As unidades de translação devem ser uma unidade de comprimento reconhecida pelo perfil de unidades; conversões são explícitas. Não há unidade implícita nem exceção à forma tipada de quantidade do EIR.
- Todas as componentes devem ser finitas. O quaternion deve ter norma dentro da tolerância definida pelo perfil de validação v0.2; valor fora da tolerância é rejeitado e nunca normalizado silenciosamente. O perfil deverá fixar numericamente essa tolerância junto com a implementação.
- A tolerância normativa v0.2 é `abs(||q||₂ - 1) <= 1e-6`, calculada em precisão IEEE 754 binary64. Valores não finitos e valores fora da tolerância são inválidos.
- Para reprodutibilidade, calcular `n² = (((x*x + y*y) + z*z) + w*w)` em ordem escrita, arredondando cada multiplicação e soma para binary64 round-to-nearest ties-to-even e sem fused multiply-add; calcular `n = sqrt(n²)` com raiz quadrada binary64 corretamente arredondada; aceitar se `abs(n - 1) <= 1e-6`. Overflow, NaN e infinito invalidam a entrada.
- A validação não altera os bytes nem os valores do EIR. Se uma representação de solver exigir normalização, ela só pode ocorrer na preparação explícita; o relatório deve registrar o quaternion original, o valor preparado, a regra/versão aplicada e o frame afetado. A aprovação vincula os bytes originais e a versão da preparação.
- `geometry.frame_id`, `simulation_case.frame_id` e cada carga/restrição que interprete componentes direcionais devem resolver para o registro. O frame da condição define os eixos dos componentes; preparação converte para o frame requerido pelo solver e registra a transformação.

### Política de unidades

- Quantidades físicas usam símbolos UCUM reconhecidos pelo perfil de validação versionado (`unit_profile_id`, `unit_profile_version`). O perfil lista as unidades aceitas para cada dimensão; unidade desconhecida, dimensão incompatível ou unidade omitida é erro. Não se inferem unidades do projeto, arquivo CAD ou solver.
- O EIR conserva valor e unidade declarados. A validação não converte nem reescreve a entrada.
- Conversões necessárias são feitas somente na preparação, por uma implementação/perfil de unidades identificado por nome e versão. O relatório registra campo de origem, valor/unidade de origem, valor/unidade de destino e identidade/versão da política. Não arredondar silenciosamente além da precisão requerida pelo formato de destino.
- Sistemas canônicos usados por adaptadores são decisões do perfil/adaptador, não uma reinterpretação dos valores originais.
- Cada registro de conversão contém JSON Pointer da quantidade, valor e unidade de origem, valor e unidade de destino, versão da política e versão do conversor. A saída preparada registra precisão/rounding do formato de destino; o pré-flight revalida dimensionalidade e limites após conversão.

O primeiro perfil executável `structural.axial_bar.v1` usa a política `ucum.structural_axial.v1`. As definições machine-readable, incluindo o mapeamento campo→dimensão e os únicos tipos de condição aceitos (`concentrated_force` em cargas e `displacement` em restrições), ficam em [`schemas/eir/profiles`](../../schemas/eir/profiles/); as unidades aceitas e canônicas são:

| Dimensão | Unidades aceitas (UCUM) | Unidade de preparação |
|---|---|---|
| Comprimento | `mm`, `m` | `mm` |
| Força | `N`, `kN` | `N` |
| Tensão/módulo | `N/mm2`, `Pa`, `kPa`, `MPa`, `GPa` | `N/mm2` |
| Adimensional | `1` | `1` |

Outras dimensões ou símbolos são rejeitados por este perfil; perfis futuros podem ampliar a allowlist sem reinterpretar o v0.1. Conversões usam fatores exatos decimais (`m→mm=1000`, `kN→N=1000`, `Pa→N/mm2=0.000001`, `kPa→N/mm2=0.001`, `MPa→N/mm2=1`, `GPa→N/mm2=1000`); a aplicação em valores usa IEEE 754 binary64 round-to-nearest ties-to-even, sem fused multiply-add. Arredondamento específico do solver só ocorre na serialização do deck e é registrado.

## Manifesto do bundle e grupos

O catálogo de artefatos é um documento externo ao EIR, fornecido como parte do `InputBundle` imutável. A proposta não cria um segundo catálogo dentro da revisão: isso evita duas fontes de autoridade entre `revision.manifest` mencionado no EIR mínimo e `immutable_input_manifest` do contrato do adaptador. O nome canônico de interface é `immutable_input_manifest`; sua versão/schema deve ser declarado junto ao pacote.

Cada registro do catálogo contém ao menos `artifact_id`, `uri`, `media_type`, `format`, `sha256` e classificação de uso (`required` ou `optional`) no perfil selecionado. `artifact_id` é único no bundle. A URI é relativa à raiz do bundle e segue as regras de confinamento descritas em `EIR_VALIDATION_ARCHITECTURE.md`; o resolver rejeita caminho absoluto, travessia, esquema/autoridade e escape por symlink.

`group_manifest_ref` é um `artifact_id` e só resolve contra esse catálogo. O artefato resolvido deve ter o tipo/formato de manifesto de grupos aceito pelo perfil e identificar exatamente a revisão, o caso, geometria e hash de malha aplicáveis. O validador verifica grupos referidos por cargas/restrições e coerência de identidade/contagens. Nomes de arquivo não resolvem referências.

A lista `artifacts[]` do bundle declara se cada arquivo é obrigatório ou opcional; um artefato opcional não pode ser consumido por uma execução. Se o executor consumir um arquivo, o perfil o trata como obrigatório, ainda que a declaração diga opcional, e o pré-flight bloqueia a inconsistência.

## Política de hash e autorização do pré-flight

Há três noções diferentes, que não devem ser confundidas:

1. **Hash do artefato:** SHA-256 dos bytes exatos do arquivo declarado no catálogo. Pode ser `null` durante autoria. Em `execution_preflight`, todo artefato obrigatório/consumido deve ter hash declarado e igual ao hash dos bytes lidos.
2. **Hash lógico da revisão:** `revision.manifest_sha256` identifica uma serialização canônica ainda não definida. Pode permanecer `null` em v0.2 e sua ausência, isoladamente, não bloqueia execução; não substituirá os hashes dos bytes do pacote.
3. **Fingerprint de aprovação:** SHA-256 independente que identifica todas as entradas que autorizam o relatório `ready`. Não substitui nem define `revision.manifest_sha256`.

### Codificação normativa do fingerprint (`OCRE-AF/1`)

O fingerprint é o SHA-256 dos seguintes bytes, sem o checksum final incorporado à própria entrada:

```text
ASCII("OCRE:EIR-APPROVAL:FINGERPRINT") || 0x00 || U16BE(1) || RECORD*
```

Cada `RECORD` usa framing binário inequívoco:

```text
U16BE(field_id) || U8(type) || U64BE(payload_length) || payload
```

`field_id` é um identificador normativo da tabela abaixo; registros aparecem em ordem crescente de `field_id`, sem omissões. `U16BE`, `U64BE` são inteiros sem sinal big-endian. `type` é `0x00` para nulo (payload vazio), `0x01` para bytes opacos e `0x03` para JSON Canonicalization Scheme (RFC 8785) codificado em UTF-8. `0x02` fica reservado e é rejeitado em `OCRE-AF/1`. Valor nulo é distinto de campo ausente e de valor vazio; campo obrigatório ausente é erro e impede gerar fingerprint. Arrays e mapas JSON do tipo `0x03` seguem RFC 8785. Arrays que o perfil define como conjuntos são ordenados antes da canonicalização pela chave estável indicada abaixo; arrays ordenados semanticamente mantêm sua ordem. Números seguem a representação numérica JCS, sem NaN/Infinity. Strings não são normalizadas Unicode; IDs normativos permanecem ASCII. Hashes usados como valores de campo são os 32 bytes binários do digest, não texto hexadecimal.

| ID | Nome do campo | Tipo e regra |
|---:|---|---|
| 1 | `eir_document_sha256` | `0x01`, hash dos bytes exatos do EIR validado. |
| 2 | `bundle_manifest_sha256` | `0x01`, hash dos bytes exatos do manifesto de bundle. |
| 3 | `profile` | `0x03`, `{ "id": ..., "version": ..., "sha256": ... }`; digest dos bytes da definição imutável do perfil. |
| 4 | `schemas` | `0x03`, lista de `{document_kind, schema_version, schema_sha256}` ordenada por `document_kind` ASCII e depois `schema_version` ASCII; a chave `(document_kind, schema_version)` é única. |
| 5 | `consumed_artifacts` | `0x03`, lista `{artifact_id, sha256}` ordenada por `artifact_id` ASCII; somente artefatos consumidos, todos com hash verificado. |
| 6 | `execution_parameters` | `0x03`, projeção JSON definida pelo perfil; contém todos os parâmetros de entrada que alteram a execução, sem campos de saída, timestamps ou dados de UI. |
| 7 | `validator` | `0x03`, `{id, version, implementation_sha256, policy_version}`; hash obrigatório dos bytes do pacote imutável do validador. |
| 8 | `adapters` | `0x03`, lista `{stage_id, adapter_id, version, binary_sha256, capabilities_sha256}` ordenada por `(stage_id, adapter_id)` ASCII e sem chave duplicada; hashes obrigatórios. |
| 9 | `unit_policy` | `0x03`, `{id, version, sha256}` da política usada na validação/conversão. |
| 10 | `preparation_policy` | `0x03`, `{id, version, sha256}` para transformações determinísticas do bundle preparado; nulo quando nenhuma preparação se aplica. |

Os campos 1–10 são sempre emitidos; somente o campo 10 pode usar nulo, explicitamente quando nenhuma preparação se aplica. `execution_parameters` é o objeto completo de `simulation_case` selecionado por `case_id`, incluindo extensions, serializado por JCS; assim os parâmetros efetivos não dependem de uma projeção implícita do perfil. O perfil deve conter exatamente um registro por `case_id` por aprovação. A serialização de `revision.manifest_sha256` permanece separada e pode continuar nula. Hashes de perfil, política, implementação do validador, binário do adaptador e capacidades são obrigatórios; ausência bloqueia pré-flight. Este fingerprint vincula conteúdo; não é assinatura, prova de identidade nem autorização criptográfica do ator. A autorização deve ser controlada por mecanismo separado.

Todos os campos JSON com nome `sha256`, `schema_sha256`, `implementation_sha256`, `binary_sha256` ou `capabilities_sha256` usam exatamente 64 caracteres hexadecimais minúsculos. A regra não se aplica aos campos binários 1–2, que contêm 32 bytes. O resultado final do fingerprint é transmitido/armazenado como exatamente 64 caracteres hexadecimais minúsculos, sem prefixo.

No modo `authoring`, hash nulo é permitido e produz `incomplete`/`not_requested` conforme o relatório; não é executável. Em `execution_preflight`, hash nulo, ausente ou divergente em dependência obrigatória bloqueia. Artefato opcional não consumido pode ter hash nulo.

Antes de `submit`, o orquestrador re-hasha o snapshot staged e recalcula `OCRE-AF/1`; qualquer divergência invalida a aprovação e exige novo pré-flight. Implementações devem compartilhar golden vectors para bytes de entrada e digest resultante antes de declarar interoperabilidade.

## Versões e migração

- EIR v0.2 usa `eir_schema_version: "0.2.0"` e schema próprio; o manifesto de bundle e o manifesto de grupos também usam versões/schema IDs próprios.
- O registry seleciona schema por `(document_kind, schema_version)`. Versão desconhecida é erro; não há download de schemas durante validação.
- Os schemas e instâncias v0.1 permanecem imutáveis e validáveis pelo schema v0.1. Os schemas v0.2 são três documentos independentes: EIR, bundle e manifesto de grupos.
- Migração v0.1→v0.2 é uma operação explícita, nunca automática/silenciosa. A ferramenta recebe a instância original e os mapeamentos/resoluções fornecidos pelo usuário; preserva os bytes originais e produz novos artefatos, sem sobrescrita.
- A migração registra hashes SHA-256 de entrada e saída, versão da ferramenta, schemas de origem/destino, mapeamentos aplicados, decisões explícitas, avisos, referências pendentes e ambiguidades. Referências ausentes não são inventadas; casos ambíguos ficam pendentes até resolução humana.
- O relatório de migração versionado registra: `report_version`; IDs e SHA-256 de origem e saída; `(document_kind, schema_version, schema_sha256)` de entrada e destino; identidade/versão/hash da ferramenta; `resolutions[]` com JSON Pointer, valor escolhido, justificativa, ator e timestamp; `pending[]` com JSON Pointer, código, explicação e bloqueio associado; e diagnósticos/avisos. URI e formato continuam exclusivamente no bundle e são referidos por `artifact_id`.
- Resoluções fornecidas explicitamente pelo usuário identificam o JSON Pointer de destino, o valor escolhido, o motivo e o ator da resolução; o relatório registra timestamp e todas as tentativas, inclusive alterações posteriores.
- A migração produz um pacote de rascunho separado com: origem v0.1 imutável; relatório de migração; e documento EIR v0.2 em estado `draft`, que pode conter `null` somente nos campos ainda não resolvidos e deve apontar esses campos no relatório. A forma completa do EIR é exigida quando `document_state` for `complete`. Enquanto houver pendência obrigatória, a validação reporta `incomplete` e o pré-flight v0.2 bloqueia. Após resolução, a saída passa por validação estrutural e semântica completas; execução só é elegível depois de novo pré-flight v0.2 com resultado `ready`.
- A ferramenta de migração é módulo independente e não bloqueia a primeira implementação do validador v0.2.
- O migrador pode copiar dados de v0.1 sem alterar semântica; não pode inferir frame raiz, atribuição material, geometria usada no caso ou vínculo de grupo a partir de nomes/único exemplo. O usuário deve fornecer essas relações ou o rascunho permanece incompleto.
- EIRs migrados declaram `document_origin: "migration_v0.1"` e devem carregar `migration` com referência ao relatório. EIRs escritos diretamente declaram `document_origin: "authored"` e não carregam proveniência de migração. A validação semântica confirma que o `report_artifact_id` resolve no bundle e que cada `pending[].json_pointer` corresponde a campo ainda não resolvido; origem migrada sem relatório é erro.
- JSON Schema valida forma. Resolução de referências, igualdade de `material_refs`, árvore de frames, confinamento de URI, hashes, coerência dos grupos e compatibilidade do adaptador são regras semânticas do validador.

## Resultado e prontidão

Manter o `ValidationReport` existente: `valid`, `invalid` ou `incomplete`, com `execution_readiness` `ready`/`blocked` em pré-flight. Hashes pendentes e vínculos ainda não resolvidos não podem virar sucesso por omissão. `ready` significa somente que as verificações determinísticas exigidas pelo perfil passaram; não certifica segurança, precisão física, convergência ou adequação de engenharia.

## Aceitação do contrato e schemas

- Referência inexistente, duplicada ou de revisão diferente é erro.
- `artifact_id` duplicado no bundle é erro semântico (JSON Schema não compara propriedades de IDs entre objetos distintos).
- Cobertura incompleta ou sobreposta do domínio por `geometry_refs`, material sem atribuição, atribuição duplicada ou `material_refs` divergente é erro.
- Tipo de carga/restrição não listado no perfil ou unidade incompatível com o tipo e componente é erro.
- Frame ausente, múltiplas raízes, ciclo, quaternion inválido, valor não finito ou frame dinâmico é erro.
- `group_manifest_ref` não resolvido no catálogo, URI insegura, artefato ausente, tipo incompatível ou hash divergente bloqueia.
- Hash nulo em autoria é permitido e não elegível à execução; hash nulo em artefato obrigatório/consumido bloqueia o pré-flight.
- Alterar bytes EIR, catálogo, artefato, schema/perfil ou capacidades invalida aprovação anterior.
- `revision.manifest_sha256: null` não bloqueia por si só no v0.2.
- v0.1 continua validável sob seu schema; v0.1 não pode passar pré-flight sem migração explícita.
- A migração não altera bytes de entrada e produz um relatório que permite reproduzir/inspecionar cada mapeamento e pendência.
- Fingerprint muda quando qualquer entrada relevante muda e permanece igual para a mesma entrada, perfil, schemas, parâmetros e implementações em qualquer plataforma.
- `document_origin: "migration_v0.1"` sem bloco `migration` ou relatório resolvível no bundle é inválido; origem `authored` com proveniência de migração é inválida.
- Instância completa e inalterada pode ser marcada `ready` somente após as verificações semânticas, de pacote e de capacidade previstas pelo perfil.

## Estado e trabalho restante

As regras conceituais, os três schemas versionados, os perfis iniciais e um golden vector de framing foram aprovados e congelados. O validador semântico e o pré-flight têm implementação local, mas a prontidão continua bloqueada porque OCRE-AF/1 ainda não está ligado ao fluxo. Testes de ponta a ponta da ferramenta de migração serão adicionados com sua implementação, que segue como módulo independente; a liberação do executor deve seguir os gates em `EIR_VALIDATION_ARCHITECTURE.md`.

O bloqueio do executor permanece uma etapa posterior, como definido em `EIR_VALIDATION_ARCHITECTURE.md`.
