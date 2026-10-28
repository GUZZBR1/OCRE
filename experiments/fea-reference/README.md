# Prova estrutural OCRE: barra em tração

Harness experimental local, com Python padrão. O caso, as referências e os gates
canônicos permanecem em `docs/architecture/STRUCTURAL_REFERENCE_CASE.md` e
`docs/architecture/STRUCTURAL_VALIDATION_ARCHITECTURE.md`.

## Comandos

```sh
python3 experiments/fea-reference/run_reference.py preflight
python3 experiments/fea-reference/run_reference.py prepare --output experiments/fea-reference/runs/baseline-c3d8
python3 experiments/fea-reference/run_reference.py prepare-gmsh --output experiments/fea-reference/runs
python3 experiments/fea-reference/run_reference.py solve --output experiments/fea-reference/runs
```

Para conferir, sem modificar, os arquivos e hashes registrados nos manifestos:

```sh
python3 experiments/fea-reference/audit_evidence.py \
  experiments/fea-reference/runs/solver_runs_manifest.json \
  --evidence-root experiments/fea-reference/runs
python3 experiments/fea-reference/audit_evidence.py \
  experiments/fea-reference/runs/manifest.json \
  --evidence-root experiments/fea-reference/runs
```

O auditor aceita os formatos de execução do solver e de preparação da malha
que o harness produz, além do sidecar experimental de proveniência descrito
abaixo. Todos os artefatos referenciados devem estar dentro da raiz indicada;
referências ausentes, alteradas ou fora dela retornam erro ou estado
incompleto. Caminhos absolutos de comandos e ferramentas nos manifestos são
ignorados e nunca executados.

O sidecar experimental de proveniência está em
`runs/cad_mesh_provenance.json`. Para auditá-lo separadamente:

```sh
python3 experiments/fea-reference/audit_evidence.py \
  experiments/fea-reference/runs/cad_mesh_provenance.json \
  --evidence-root experiments/fea-reference/runs
```

Ele identifica a origem atual como `synthetic_geo`: primitivas `.geo` não são
CAD importado. O sidecar só referencia artefatos já versionados e métricas já
registradas; não executa Gmsh ou CalculiX. A métrica de mean ratio é específica
do benchmark e não define limiar geral. A proveniência de STEP/OCCT permanece
pendente; veja [CAD_MESH_PROVENANCE_LOCAL.md](../../docs/architecture/CAD_MESH_PROVENANCE_LOCAL.md).

`preflight` procura, nesta ordem, `--gmsh`/`--ccx`, `GMSH_BIN`/`CCX_BIN`, os
binários em `.local-tools/` e por fim `PATH`. `CCX_LIB_DIR` ou
`--ccx-lib-dir` acrescenta uma pasta de bibliotecas só ao ambiente do processo.
O probe `ccx --version` roda em diretório temporário por execução dentro de
`runs/ccx-probes/`, pois o CCX interpreta `--version` como nome de job e cria
arquivos `.dat`, `.sta` e `.cvg`.

`prepare` continua sendo o baseline estruturado C3D8, em arquivos próprios.
`prepare-gmsh` cria, para cada refinamento, o `.geo`, o `.inp` de malha Gmsh e
um arquivo de análise CalculiX separado. O harness confere IDs únicos,
conectividade C3D10, mapeamento completo de `ELSET=VOLUME`, correspondência
exata de `NSET=X_FIXED/X_DISPLACED` com as faces por coordenada, volume,
orientação do tetraedro de canto, nós midside no ponto médio e unicidade das
coordenadas dos pinos. Os pinos são selecionados por coordenadas e escritos
como `NSET=PIN_A/PIN_B` no deck CalculiX; não são grupos físicos exportados pelo
Gmsh. O deck separado é inspecionado quanto a material, seção, step, apoios,
deslocamentos e pedidos de saída antes de ser mantido.

`solve` copia cada deck para seu próprio diretório sob
`runs/solver_runs/<nome-do-deck>/` e executa o CCX somente com esse diretório
como `cwd`. Se já houver uma execução, cria `attempt_NNN/` e preserva as saídas
anteriores. A extração usa `S`, `E` e `COORD` dos pontos de integração em `.dat`,
resultantes RF totais, volume do tetraedro e peso `volume/4` por ponto. Ela
confirma nós midside afins antes de usar pesos iguais e registra amostras,
volumes pesados, valores, erros, hashes e artefatos nativos. `--size 5.0` limita
a execução a um refinamento.

Após `solve`, `runs/manifest.json` é reconciliado com a execução e os gates; a
preparação anterior permanece em `runs/manifest.prepared.json`. O baseline C3D8
tem manifesto separado em `runs/baseline-c3d8/manifest.json`. Cada entrada do
`runs/solver_runs_manifest.json` contém o `cwd` absoluto usado, o deck/hash, o
diretório de saída e os hashes dos artefatos. `runs/solver_runs_history.json`
preserva as tentativas anteriores e suas saídas. O arquivo exploratório
`runs/axial_bar_gmsh_h5mm.raw.inp` continua como export bruto somente de malha;
não é deck de análise e não alimenta as métricas finais.

## Critérios e interpretação

As referências deste benchmark são `Rx=2100 N`, `S11=21 MPa`,
`E11=0.0001` e `E22=E33=-0.00003`. O relatório compara reação, tensão axial no
terço central e contração transversal às metas registradas no caso. Para a
convergência, compara reação e tensão entre os dois níveis mais finos. Esses
resultados verificam somente este caso e configuração; não validam projeto real.

O teste atual mede determinante/orientação e volume e confirma o mapeamento
afim dos nós midside, o que torna o Jacobiano positivo e constante por elemento.
Uma métrica geral de forma (aspect ratio/skewness) continua `not_assessed`.
Não há validação física experimental. A aceitação dos critérios numéricos não
equivale a certificação ou suporte geral do OCRE a FEA.

Estado dos gates desta execução: A `not_assessed` (entrada hardcoded sem schema
externo); B `warning` (mean ratio medido sem limite de aceitação); C `passed`;
D `passed` (erro de reação/tensão/contração dentro dos limites e mudança entre
as duas malhas mais finas de 0%); E `warning` (hashes e saídas nativas presentes,
mas sem teste de falhas/replay ou validação física). O estado global é
`solver_runs_completed`; isso não declara suporte geral a FEA.

## Ferramentas locais e proveniência

Os binários e runtimes ficam em `.local-tools/`, ignorados pelo Git e sem
alteração do PATH/sistema global. Foram obtidos para esta execução e podem ser
removidos localmente quando não forem mais necessários.

| Artefato | Origem | Licença | Tamanho do download | SHA-256 |
| --- | --- | --- | ---: | --- |
| Gmsh 4.15.2 Linux64 | `https://gmsh.info/bin/Linux/gmsh-4.15.2-Linux64.tgz` | GPL-2-or-later, exceção de linking | 38,329,303 bytes | `6c62116e072db29fd1f701fdb9d3d34b46ed5373545063e177b965a008274745` |
| CalculiX CCX 2.23 Linux | `https://dhondt.de/ccx_2.23.tar.bz2` | GPL-2-or-later | 2,502,788 bytes | `35d426fed5eb164fbbaaafa20819d13d22e30bc2b9bc9c6c4ed957e9468355dd` |
| `libgfortran4` 7.5.0 runtime | [Ubuntu archive](https://archive.ubuntu.com/ubuntu/pool/main/g/gcc-7/libgfortran4_7.5.0-3ubuntu1~18.04_amd64.deb), Bionic amd64 | GPL-3 com GCC Runtime Library Exception | 492,148 bytes | `f89b2473bc251ba0c8eb9479ad31d5f96019e2ea1f8be466beea3a68b11b19fc` |

O runtime `libgfortran.so.4` é necessário porque o executável oficial CCX 2.23
distribuído foi linkado contra essa ABI e o sistema atual contém somente a ABI
mais recente. O `.deb` é extraído em `.local-tools/ccx-2.23/runtime/`; nada é
instalado no sistema. Gmsh e CalculiX são ferramentas opcionais para reproduzir
as etapas, não dependências Python.
