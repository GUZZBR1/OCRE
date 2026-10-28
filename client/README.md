# OCRE Offline Viewer Proof

## Decisão técnica local

Esta primeira prova usa TypeScript, Vite e Three.js, sem framework de interface. O baseline do OCRE já define TypeScript para o cliente e ainda não escolhe framework. Three.js fornece `BufferGeometry` para a malha e as cores por vértice, `Raycaster` para selecionar nós/elementos e controles de órbita para navegar na cena. Vite compila e serve a aplicação localmente; não integra o runtime final. Um renderer WebGL2 próprio economizaria a dependência Three.js, mas colocaria câmera, picking, buffers e manutenção de GPU dentro do protótipo. React/Vue/etc. adicionariam uma camada de interface sem necessidade para esta única tela.

Versões exatas no `package.json` e `package-lock.json`: Three.js 0.186.1 (MIT), `@types/three` 0.186.0 (MIT), Vite 8.3.5 (MIT) e TypeScript 7.0.2 (Apache-2.0). `@types/three` é mantido pela comunidade Three Types. Vite 8 requer Node.js 20.19+ ou 22.12+; o ambiente usado na bancada tinha Node 22.22.2.

A comparação considerou Three.js, Babylon.js e WebGL2 direto. Babylon.js é uma engine válida, porém mais abrangente que a necessidade desta prova. WebGL2 direto reduz dependências, mas transfere manutenção de câmera, seleção e geometria ao produto. O escopo pequeno e reversível justifica a escolha Three.js; ainda não há evidência de desempenho em malhas grandes ou compatibilidade ampla de dispositivos. Three.js requer WebGL2.

Referências oficiais consultadas em 2026-10-28:

- [Three.js: instalação local com npm e Vite](https://threejs.org/manual/pages/installation.html)
- [Three.js: BufferGeometry](https://threejs.org/docs/pages/BufferGeometry.html) e [Raycaster](https://threejs.org/docs/pages/Raycaster.html)
- [Three.js: WebGLRenderer](https://threejs.org/docs/pages/WebGLRenderer.html) e [licença MIT](https://github.com/mrdoob/three.js/blob/dev/LICENSE)
- [Vite: guia e requisito Node](https://vite.dev/guide/) e [licença MIT](https://github.com/vitejs/vite/blob/main/LICENSE)
- [TypeScript: distribuição npm](https://www.typescriptlang.org/download/) e [licença Apache-2.0](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt)
- [Tipos comunitários para Three.js](https://github.com/three-types/three-ts-types)

## Escopo da prova

A cena carrega apenas `src/fixture.ts`, embutida no bundle local. Seus metadados dizem `synthetic-demo`, `synthetic: true`, `bundled-local-fixture` e `not-validated`. A fixture inventada tem 8 nós e 6 tetraedros; usa mm para posição/deslocamento e MPa para tensão equivalente. A deformação só aplica os deslocamentos inventados às coordenadas com escala visual 30× e pode ser desligada; a consulta continua mostrando o deslocamento base da fixture. Ela não calcula nem valida física. Cada tensão de elemento é a média dos quatro valores nodais correspondentes.

Use o mouse para orbitar e o scroll para aproximar. Alterne a malha e as cores do resultado, habilite a deformação estática e escolha a seleção de nó ou elemento para consultar valores da fixture. A legenda e os valores consultados derivam dos metadados e registros dessa mesma fixture.

O protótipo não afirma validade física, execução aprovada, readiness ou compatibilidade de solver. Não há backend, API, autenticação, cloud, importação CAD ou solver. Nenhuma fixture remota pode ser carregada.

## Executar localmente

Requer Node.js 22.12+ (ou 20.19+) e npm.

```sh
npm ci
npm run dev
```

Abra o endereço local informado pelo Vite. O servidor vincula somente `127.0.0.1`.

Comandos reproduzíveis:

```sh
npm run typecheck
npm run build
npm test
npm run check
```

`npm test` usa o test runner nativo do Node e verifica a marcação/origem da fixture, referências e unidades, deformação, faces exteriores e faixa/paleta de tensão. `npm run check` reúne typecheck, build e testes. Os testes cobrem regras da prova e coerência dos dados locais; não são testes científicos nem prova de browser/WebGL em todos os dispositivos.
