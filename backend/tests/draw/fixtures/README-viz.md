# Fixtures de visualização (beta v0.1)

## `axial_bar_h5mm.frd`

- **Origem:** cópia do worktree do worker FEA,
  `~/workspace/ocre-beta-wt-fea/experiments/fea-reference/runs/solver_runs/axial_bar_gmsh_deck_h5mm/attempt_004/axial_bar_gmsh_deck_h5mm.frd`
  (não gerado neste worktree; o worker FEA não estava mergeado).
- **Caso:** barra axial 100×10×10 mm, E=210000 MPa, Rx=2100 N
  (caso de referência do OCRE, gate G4.1). Malha Gmsh h=5 mm,
  433 elementos C3D10, solver CalculiX 2.23.
- **Validação da origem:** `metrics.json` do mesmo run registra
  S11 observado = 21.0 MPa (referência 21.0 MPa, erro 0.0%) e
  reações ±2100 N. O campo `SXX` no `.frd` é ~21 MPa nos nós.
- **Uso:** fixture real para `read_frd`/`render_field` — nunca um
  mock que finja um resultado FEA.
