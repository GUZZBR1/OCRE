# Plano de auditoria do repositório OCRE (para execução pelo Devin)

Objetivo: mapear o repo inteiro e produzir um inventário rankeado de **(a) bugs**, **(b) pontos de simplificação**, **(c) código morto/inútil a cortar**, e depois entregar os cortes e correções em PRs pequenos.

Entregáveis:
- `docs/repo-audit-findings.md` (relatório da Fase 3)
- Fila de PRs (Fase 4), aberta somente após revisão humana do relatório

Leitura inicial obrigatória: `README.md`, `AGENTS.md`, `docs/PROJECT_CONTEXT.md`.

---

## Regras globais (valem para todas as fases)

- [ ] **Fases 0–3 são read-only.** Nada é deletado ou editado, exceto os arquivos de saída do plano (`docs/repo-audit-findings.md` e rascunhos em `.omc/audit/` ou `.tmp/audit/`).
- [ ] Nunca commitar em `main`. Todo trabalho sai via PR.
- [ ] Worktrees novas somente em `.worktrees/<nome>` dentro da raiz do repo. Nada do OCRE fora de `/home/gusta/projetos/OCRE`.
- [ ] Núcleo = `backend/ocre_core/`. Código fora do núcleo só é tocado se estiver quebrado.
- [ ] Nada é deletado sem evidência de **zero referências** (grep + import + teste + docs + config).
- [ ] Dados e fixtures existentes são preservados até aprovação explícita.
- [ ] Contratos congelados (`schemas/eir`, EIR v0.2 e seus digests, `schemas/draw-paper`): **só reportar, nunca alterar**.
- [ ] Não alterar `requirements*.txt`, `pytest.ini` nem `package.json`. Ferramentas extras (ruff, vulture, radon, coverage) só em venv descartável.
- [ ] Cada finding diz se é **evidência** (reproduzido ou provado por grep/execução) ou **inferência** (explícito).

---

## Fase 0 — Reconciliação com trabalho em andamento

Objetivo: não duplicar findings nem propor PRs conflitantes.

### 0.1 Setup e baseline

```bash
cd /home/gusta/projetos/OCRE
git fetch --all --prune
git status --short
# detectar o venv real (README diz backend/.venv; também existe .venv-beta/ na raiz)
for p in backend/.venv .venv-beta .venv; do
  test -x "$p/bin/python" && echo "$p" && "$p/bin/python" -c "import pytest, pyflakes; print('ok', pytest.__version__)"
done
```

- [ ] Escolha o venv onde `import pytest, pyflakes` funciona. Registre em `.tmp/audit/env.md`. Daqui em diante `PY=<venv>/bin/python`.
- [ ] Baseline Python: `$PY -m pytest -q 2>&1 | tail -20`. Registre passed/failed/skipped. Falha pré-existente vira finding, não bloqueio.
- [ ] Baseline client: `cd client && npm run check 2>&1 | tail -20`.
- [ ] Venv descartável para ferramentas extras:

```bash
python3 -m venv .tmp/audit/venv
.tmp/audit/venv/bin/pip install ruff vulture radon coverage
```

### 0.2 Branches e worktrees

```bash
git branch -a -vv
git worktree list
ls .worktrees/
git log --oneline main..origin/chore/limpeza-5
git log --oneline main..origin/chore/limpeza-5-residual
git log --oneline main..origin/chore/bugs-draw
```

- [ ] Para cada branch de limpeza e cada worktree em `.worktrees/*` (incluindo `limpeza`, `limpeza-residual`, `arquivo-prs-1-2`, `draw-core` e outras), rode:

```bash
git diff --stat main...<branch>
git diff main...<branch> -- backend client schemas tests experiments docs
```

- [ ] Classifique cada branch: **aberta** (PR aberto ou commits não mergeados), **mergeada**, **abandonada**.
- [ ] Para cada branch aberta registre: arquivos removidos, símbolos removidos, bugs corrigidos, testes adicionados.
- [ ] Verifique PRs abertos: `gh pr list --state open --json number,title,headRefName,files`.

### 0.3 Saída

- [ ] Gere `.tmp/audit/exclusions.md` com a tabela: `arquivo | símbolo/trecho | branch/PR | status`.
- [ ] Gere a lista de arquivos tocados por branches abertas. Nas fases 2–4 esses arquivos só entram como finding com a nota "conflita com <branch>".

**Done (Fase 0):** venv e baseline registrados; toda branch e worktree classificada; `exclusions.md` existe.

---

## Fase 1 — Mapa do repo (read-only)

### 1.1 Inventário de entry points

Confirme existência e função de cada um:
- `backend/ocre_core/draw/cli.py`
- `backend/ocre_core/api/app.py`
- `backend/ocre_core/integrations/process_host.py`
- `client/src/main.ts`

```bash
for f in backend/ocre_core/draw/cli.py backend/ocre_core/api/app.py backend/ocre_core/integrations/process_host.py client/src/main.ts; do
  test -f "$f" && echo "OK  $f" || echo "FALTA $f"
done
grep -rn "__main__\|console_scripts\|entry_points" backend --include=*.py --include=*.toml --include=*.cfg | head -30
```

Registre qualquer entry point adicional encontrado (scripts, `experiments/`, `tests/` helpers).

### 1.2 Inventário de módulos e linhas

```bash
find backend/ocre_core -name '*.py' -not -path '*/__pycache__/*' | sort | xargs wc -l | sort -rn > .tmp/audit/py-loc.txt
find client/src -name '*.ts' | xargs wc -l
```

Pacotes a cobrir: `api, application, draw, draw/ai, integrations, migration, persistence, provenance, validation, jobs, sync`.

### 1.3 Grafo de imports por pacote

```bash
$PY - <<'EOF' > .tmp/audit/import-graph.txt
import ast, pathlib, collections
root = pathlib.Path("backend")
edges = collections.defaultdict(set)
for p in root.rglob("*.py"):
    if "__pycache__" in p.parts: continue
    mod = ".".join(p.relative_to(root).with_suffix("").parts)
    try: tree = ast.parse(p.read_text())
    except SyntaxError as e:
        print("SYNTAXERR", p, e); continue
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("ocre_core"): edges[mod].add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("ocre_core"):
            edges[mod].add(n.module)
for k in sorted(edges):
    print(k, "->", ", ".join(sorted(edges[k])))
EOF
```

- [ ] Resuma dependências entre pacotes (pacote → pacote). Aponte ciclos e imports que violam a direção documentada em `docs/architecture/`.

### 1.4 Módulos sem importador

```bash
$PY - <<'EOF' > .tmp/audit/no-importers.txt
import ast, pathlib
root = pathlib.Path("backend")
mods = {".".join(p.relative_to(root).with_suffix("").parts): p
        for p in root.rglob("*.py") if "__pycache__" not in p.parts and p.name != "__init__.py"}
imported = set()
for base in (root, pathlib.Path("tests"), pathlib.Path("experiments")):
    for p in base.rglob("*.py"):
        try: tree = ast.parse(p.read_text())
        except SyntaxError: continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module)
                imported.update(f"{n.module}.{a.name}" for a in n.names)
for m, p in sorted(mods.items()):
    if m not in imported: print(m, p)
EOF
```

- [ ] Cada módulo listado vira candidato. Antes de qualquer finding: confirme com `grep -rn "<nome>" --include=* . ` (cobre import dinâmico, string em config, `-m`, docs).

### 1.5 Módulos × testes

```bash
$PY -m pytest --collect-only -q 2>&1 | tail -5
.tmp/audit/venv/bin/coverage run --source=backend/ocre_core -m pytest -q
.tmp/audit/venv/bin/coverage report -m > .tmp/audit/coverage.txt
```

- [ ] Monte tabela `módulo | testes que o importam | cobertura %`. Módulos com 0% e sem teste são candidatos a "código morto ou não testado".
- [ ] Rode também `$PY -m pytest -q -m "not slow"` para separar tempo; registre quais testes são `slow`.

### 1.6 Schemas e contratos congelados (só reportar)

```bash
find schemas -type f | sort
grep -rn "digest\|sha256" schemas docs/architecture/EIR_V02_CONTRACT.md | head -30
```

- [ ] Liste cada schema, quem o consome (grep) e quais testes validam o digest. **Não alterar.** Divergência entre schema e implementação vira finding categoria `contrato`, severidade conforme impacto, ação "reportar ao dono".

### 1.7 Experiments e fixtures

```bash
ls experiments experiments/fea-reference tests/experiments tests/eir
find tests backend/tests -type d -name 'fixtures' -o -type d -name 'data' | head
ls -la .tmp 2>&1 | head -20
```

- [ ] Para cada fixture/arquivo de dados: quem referencia (`grep -rn "<nome>" tests backend experiments docs`). Os sem referência viram candidatos, mas **ficam preservados** até aprovação explícita.

### 1.8 Saída

- [ ] `.tmp/audit/repo-map.md` com: entry points, grafo por pacote, tabela módulo×teste, órfãos, schemas.

**Done (Fase 1):** `repo-map.md` cobre todos os pacotes listados e os 4 entry points; órfãos têm grep de confirmação anexado.

---

## Fase 2 — Detecção (read-only)

Para todo candidato: consulte `exclusions.md` (Fase 0) e descarte ou marque "já em <branch>".

### 2A — Bugs

Cada bug exige: `arquivo:linha`, evidência, e (quando possível) um **teste de reprodução proposto** (escrito no relatório, não commitado).

```bash
# excepts silenciosos e genéricos
grep -rnE "except\s*:|except (Base)?Exception" backend/ocre_core --include=*.py
grep -rnE -A1 "except.*:\s*$" backend/ocre_core --include=*.py | grep -E "^\S+-[0-9]+-\s*(pass|\.\.\.|continue)\s*$"

# marcadores
grep -rnE "TODO|FIXME|XXX|HACK" backend client/src schemas docs --include=*.py --include=*.ts --include=*.md --include=*.json

# assert em produção (fora de tests)
grep -rn "^\s*assert " backend/ocre_core --include=*.py

# estado mutável compartilhado
grep -rnE "^[A-Za-z_]+\s*(:\s*[^=]+)?=\s*(\[\]|\{\}|set\(\)|dict\(\)|list\(\))" backend/ocre_core --include=*.py
grep -rnE "def .*=\s*(\[\]|\{\}|set\(\)|dict\(\))" backend/ocre_core --include=*.py
grep -rn "global " backend/ocre_core --include=*.py

# pyflakes (nomes indefinidos, variáveis não usadas)
$PY -m pyflakes backend/ocre_core tests backend/tests 2>&1 | tee .tmp/audit/pyflakes.txt

# ruff (descartável): bugbear + erros
.tmp/audit/venv/bin/ruff check backend/ocre_core --select E9,F,B,PLW,S110,S112 --no-fix --output-format concise > .tmp/audit/ruff-bugs.txt
```

Revisão manual obrigatória (ler o código, não só grep):
- [ ] Caminhos de erro em `jobs/`, `sync/`, `persistence/`, `integrations/process_host.py`, `draw/ai/`, `migration/`: erro engolido, retorno que mascara falha, cleanup ausente em `finally`, subprocess sem timeout, arquivo temporário não removido.
- [ ] Erros de lógica: comparações off-by-one, `is` com literais, mutação de argumento, retorno de objeto mutável interno, race em jobs/sync.
- [ ] **Contrato × implementação:** para cada doc em `docs/architecture/*` (`ADAPTER_CONTRACT.md`, `CLOUD_API_CONTRACT.md`, `EIR_V02_CONTRACT.md`, `DRAW_*.md`, etc.), liste as afirmações verificáveis (campos, códigos de erro, ordem de operações, nomes) e confira no código. Divergência = finding `contrato` com as duas citações (`doc:linha` e `código:linha`).
- [ ] Testes que falham no baseline ou são `skip`/`xfail` sem motivo: `grep -rnE "skip|xfail" tests backend/tests`.
- [ ] Client: `grep -rnE "catch\s*(\([^)]*\))?\s*\{\s*\}|as any|@ts-ignore|@ts-expect-error" client/src`.

### 2B — Código morto

```bash
# pyflakes: imports e variáveis não usados
$PY -m pyflakes backend tests 2>&1 | grep -E "imported but unused|assigned to but never used|unable to detect undefined"

# vulture (confiança 80+ primeiro, depois 60)
.tmp/audit/venv/bin/vulture backend/ocre_core tests backend/tests --min-confidence 80 > .tmp/audit/vulture-80.txt
.tmp/audit/venv/bin/vulture backend/ocre_core tests backend/tests --min-confidence 60 > .tmp/audit/vulture-60.txt

# símbolos públicos sem referência (confirmar cada um)
$PY - <<'EOF' > .tmp/audit/unreferenced-symbols.txt
import ast, pathlib, re, subprocess
root = pathlib.Path("backend/ocre_core")
for p in sorted(root.rglob("*.py")):
    if "__pycache__" in p.parts: continue
    tree = ast.parse(p.read_text())
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.name.startswith("_"):
            r = subprocess.run(["grep","-rnw",n.name,"backend","tests","experiments","docs","client/src","schemas","--include=*.py","--include=*.ts","--include=*.md","--include=*.json","--include=*.toml","--include=*.yml"],capture_output=True,text=True)
            hits = [l for l in r.stdout.splitlines() if not l.startswith(f"{p}:{n.lineno}:")]
            if len(hits) == 0:
                print(f"{p}:{n.lineno} {n.name}")
EOF

# código comentado em bloco
grep -rnE "^\s*#\s*(def |class |import |from |return |if |for |while |[a-z_]+\s*=)" backend client/src --include=*.py --include=*.ts

# arquivos soltos / lixo
ls -la .tmp
git ls-files | grep -E "\.(bak|orig|tmp|log|pyc)$|~$"
git ls-files | grep -E "(^|/)(old|legacy|deprecated|backup)"
```

- [ ] **Deps sem uso:** para cada pacote em `requirements*.txt`/`backend/requirements*`, grep do nome do import correspondente:

```bash
ls requirements*.txt backend/requirements*.txt 2>/dev/null
# para cada dep X:  grep -rnE "^\s*(import|from)\s+X\b" backend tests experiments
```

- [ ] **Fixtures/experiments órfãos:** itens da seção 1.7 sem nenhuma referência em testes, docs ou código.
- [ ] **Frontend:** exports TS não usados:

```bash
cd client
npx tsc --noEmit --noUnusedLocals --noUnusedParameters 2>&1 | tee ../.tmp/audit/tsc-unused.txt
grep -rnE "^export " src | sed -E 's/.*export (default )?(async )?(function|const|class|type|interface|enum) ([A-Za-z0-9_]+).*/\4/' | sort -u | while read s; do
  n=$(grep -rnw "$s" src tests 2>/dev/null | wc -l); [ "$n" -le 1 ] && echo "SEM USO: $s"
done
cd ..
```

- [ ] **Dependências npm sem uso:** comparar `client/package.json` com imports em `client/src` e `client/tests`.

**Regra de confirmação:** um símbolo/módulo só vira finding de código morto com (1) saída do vulture ou lista de símbolos, **e** (2) grep com zero referências em `backend tests experiments docs client schemas`, **e** (3) verificação de uso dinâmico (`getattr`, `importlib`, strings em JSON/YAML/TOML, registro por decorator). Sem (3), confiança = **inferência**.

### 2C — Simplificação

Critério: cada proposta é uma **vitória clara de legibilidade**. Proibido: code golf, mexida de whitespace, renomear por gosto, trocar estilo equivalente.

```bash
# complexidade e tamanho
.tmp/audit/venv/bin/radon cc backend/ocre_core -n C -s > .tmp/audit/radon-cc.txt
.tmp/audit/venv/bin/radon raw backend/ocre_core -s > .tmp/audit/radon-raw.txt
$PY - <<'EOF'
import ast, pathlib
for p in pathlib.Path("backend/ocre_core").rglob("*.py"):
    for n in ast.walk(ast.parse(p.read_text())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ln = n.end_lineno - n.lineno + 1
            if ln > 80: print(f"{ln:4d} {p}:{n.lineno} {n.name}")
EOF

# duplicação real: blocos idênticos de >=8 linhas
$PY - <<'EOF' > .tmp/audit/duplicates.txt
import pathlib, collections, hashlib
W = 8
seen = collections.defaultdict(list)
for p in pathlib.Path("backend/ocre_core").rglob("*.py"):
    lines = [l.strip() for l in p.read_text().splitlines()]
    for i in range(len(lines) - W):
        blk = lines[i:i+W]
        if sum(1 for l in blk if len(l) > 3 and not l.startswith("#")) < W - 1: continue
        seen[hashlib.md5("\n".join(blk).encode()).hexdigest()].append((str(p), i+1))
for h, locs in seen.items():
    if len({f for f, _ in locs}) > 1 or len(locs) > 1: print(locs)
EOF

# abstrações com único uso: classes/funções privadas usadas 1x
grep -rnE "^(class|def) _?[A-Za-z]+" backend/ocre_core --include=*.py | head -50   # lista completa vem do script de símbolos da 2B; filtrar contagem == 1

# parâmetros/flags nunca exercitados: parâmetros com default cujo valor nunca é passado
# (revisão manual guiada por: grep -rn "<func>(" e inspeção dos call sites)
```

Revisão manual:
- [ ] Classes base/abstrações com **um** único herdeiro/uso e sem plano documentado de segundo uso.
- [ ] Parâmetros e flags (`strict=`, `legacy=`, `debug=`) cujo call site nunca passa o valor não-default; nenhum teste exercita.
- [ ] Wrappers que só repassam argumentos.
- [ ] Camadas defensivas sem caminho de erro alcançável (validações duplicadas entre `api` → `application` → `validation`; `try/except` em volta de código que não lança).
- [ ] Funções longas/complexas com split óbvio (cada trecho com nome natural).
- [ ] Duplicação **real** (mesma lógica, mesmos invariantes). Semelhança superficial não conta.

Para cada proposta escreva "antes → depois" em 3–6 linhas e confirme que o comportamento observável fica idêntico (quais testes cobrem; se nenhum, a proposta exige teste de caracterização antes).

**Done (Fase 2):** cada categoria rodou todos os comandos acima; saídas brutas em `.tmp/audit/`; cada candidato filtrado contra `exclusions.md`; nenhum arquivo do repo modificado (`git status --short` limpo, exceto `.tmp/`/`docs/repo-audit-findings.md`).

---

## Fase 3 — Relatório (read-only)

Arquivo: `docs/repo-audit-findings.md`.

### 3.1 Formato de cada finding

```
### F-<nnn> — <título curto>
- categoria: bug | morto | simplificação | contrato
- severidade: crítica | alta | média | baixa
- confiança: evidência | inferência (explicar o que falta para virar evidência)
- local: arquivo:linhas (lista)
- evidência: trecho citado, saída de comando, ou grep com zero hits (colar o comando)
- ação sugerida: <ação concreta e mínima>
- esforço: S (<1h) | M (<1 dia) | L
- risco de remoção/mudança: baixo | médio | alto (o que pode quebrar)
- teste: <teste de reprodução (bug) ou teste que prova a equivalência (simplificação/morto)>
- conflito: <branch/PR da Fase 0 ou "nenhum">
```

### 3.2 Ranking

- [ ] Ordem: (1) bugs com severidade alta/crítica, (2) código morto com zero referências e risco baixo, (3) bugs médios/baixos, (4) simplificações de maior ganho, (5) inferências.
- [ ] Dentro de cada grupo: maior impacto ÷ risco de remoção primeiro.
- [ ] Inclua no topo: sumário (contagem por categoria/severidade), baseline de testes, lista de exclusões da Fase 0, e seção "Contratos congelados: divergências reportadas (não alterar)".
- [ ] Inclua no fim: "Descartados" (candidatos investigados e rejeitados, com o motivo), para o revisor não refazer o trabalho.

### 3.3 Gate de revisão

- [ ] Abra o relatório para revisão humana. **Pare.** Fase 4 só começa com aprovação explícita (por finding ou por lote).

**Done (Fase 3):** relatório salvo; todo finding tem os 10 campos; nenhum finding sem comando de evidência; aprovação humana registrada.

---

## Fase 4 — Filas de PRs (somente após aprovação da Fase 3)

### 4.1 Agrupamento

Regras:
- PR pequeno e independente; **uma** vitória clara por PR. Proibido "cleanup geral".
- Ordem por segurança de merge:
  1. Código morto com **zero referências** e confiança "evidência" (inclui imports/variáveis não usados).
  2. Deps/arquivos órfãos aprovados explicitamente (fixtures e dados só com aprovação por item).
  3. Simplificações com teste de equivalência verde.
  4. Bugs: **sempre** com teste de regressão (o teste falha em `main` e passa no PR).
- Um PR não toca arquivos tocados por branch aberta da Fase 0; se tocar, rebase depois que a branch mergear, ou deixe na fila como "bloqueado por <branch>".
- Finding de categoria `contrato`: **não gera PR**; vai como issue/nota ao dono.

Saída: `.tmp/audit/pr-queue.md` com `PR-n | findings | arquivos | ordem | bloqueios | verificação`.

### 4.2 Execução de cada PR

```bash
cd /home/gusta/projetos/OCRE
git fetch origin && git checkout main && git pull --ff-only
git worktree add .worktrees/audit-<nome> -b chore/audit-<nome> origin/main
cd .worktrees/audit-<nome>
# ... aplicar APENAS os findings do PR ...
$PY -m pytest -q                       # $PY = python do venv detectado na Fase 0 (caminho absoluto)
$PY -m pyflakes backend/ocre_core      # sem novos avisos
(cd client && npm run check)           # se o PR tocar client/
git diff --stat origin/main            # confirmar escopo
git add -A && git commit -m "<tipo>(<escopo>): <resumo>"
git push -u origin chore/audit-<nome>
gh pr create --base main --title "<título>" --body "<findings F-nnn, evidência, testes>"
```

- [ ] Para bugs: commit 1 = teste de regressão (falhando), commit 2 = correção. Cole no PR a saída do teste falhando em `main` e passando na branch.
- [ ] Para dead code: cole no PR o grep com zero referências e a saída de `pytest -q` antes/depois (mesma contagem de testes, exceto testes removidos junto do código morto, listados).
- [ ] Nunca `git push` para `main`. Nunca `--force` em branch compartilhada.
- [ ] Comentários de revisão que apontem correção: aplicar, rodar a verificação, dar push na branch do PR e conferir comentários novos. Perguntar apenas se a correção for ambígua, contraditória ou sair do escopo do PR.
- [ ] Depois do merge: `git worktree remove .worktrees/audit-<nome>` e `git branch -d chore/audit-<nome>` (somente do que o executor criou).

**Done (Fase 4, por PR):** `pytest -q` verde, `pyflakes` sem novos avisos, `npm run check` verde (se aplicável), escopo = um finding ou grupo coeso, evidência colada no corpo do PR.

---

## Painel de progresso (o executor marca)

| Fase | Status | Saída | Done verificado |
|---|---|---|---|
| 0 Reconciliação | [x] | `.tmp/audit/exclusions.md` | [x] |
| 1 Mapa | [x] | `.tmp/audit/repo-map.md` | [x] |
| 2A Bugs | [x] | `.tmp/audit/*` | [x] |
| 2B Morto | [x] | `.tmp/audit/*` | [x] |
| 2C Simplificação | [x] | `.tmp/audit/*` | [x] |
| 3 Relatório | [x] | `docs/repo-audit-findings.md` (PR #22 mergeado 2026-11-23; errata em PR de acompanhamento) | [ ] aprovação por finding pendente — gate da Fase 4 |
| 4 PRs | [ ] | `.tmp/audit/pr-queue.md` + PRs | [ ] por PR |

## Critérios de parada (o executor para e pergunta)

- Baseline de testes vermelho por causa não relacionada ao plano e que impede a verificação.
- Finding exige alterar contrato congelado (EIR v0.2, digests, `schemas/`).
- Remoção candidata tem referência dinâmica ambígua (`getattr`, `importlib`, string em config).
- Conflito com branch aberta da Fase 0 que o executor não consegue resolver por ordem de merge.
- Comentário de revisão ambíguo, contraditório ou fora do escopo do PR.
