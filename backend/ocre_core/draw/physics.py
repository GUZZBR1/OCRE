"""Física do beta v0.1 OCRE<->Draw: PyBullet + MuJoCo + Pinocchio.

Ordem do Senado (corrida beta, fórmula fechada; "aprovado" = selecionado
para o beta provisório, NÃO provado contra conjunto selado):

- PyBullet primeiro: baseline CPU leve + envelope de carga (``normalForce``).
- MuJoCo: verdade dinâmica (CPU determinístico, ``mj_contactForce`` 6D).
- Pinocchio: check das cargas do MuJoCo (``rnea``/``aba``, MESMO modelo).

FORA do beta por decisão do usuário (2026-11-05 ~16:35): Newton (nem
Warp, nem GPU), Gazebo+ROS 2, Chrono, Webots, O3DE e Isaac Sim. Nada
neste módulo importa ou instala qualquer um deles.

Modelo único versionado: a barra axial do caso FEA de referência
(``docs/architecture/STRUCTURAL_REFERENCE_CASE.md``: prisma 100x10x10 mm,
E=210000 N/mm2, Rx=2100 N, S11=21 MPa). O MESMO conjunto de parâmetros
gera o URDF (PyBullet/Pinocchio) e o MJCF (MuJoCo); modelos diferentes
invalidam o check, por isso os dois textos carregam o sha256 dos
parâmetros e o relatório registra ``model_sha256`` dos dois lados.

Unidades: SI em todo o módulo (m, kg, s, N). O caso FEA é declarado em
mm; a conversão mm -> m é explícita e única, aqui.

Densidade: o caso FEA de referência declara E e ν, mas NÃO declara
densidade. A dinâmica de corpo rígido precisa de massa/inércia, então
a densidade é um parâmetro DECLARADO do caso beta (aço, 7850 kg/m3),
registrado como tal no sidecar/relatório — não é propriedade medida.
``BarParams`` exige densidade (``None`` ou <= 0 é bloqueado, fail-closed);
não existe densidade default escondida no código.

Execução: os engines só são importados dentro das funções que os usam
(import preguiçoso), então importar este módulo nunca puxa PyBullet,
MuJoCo ou Pinocchio para o processo do servidor. O ``__main__`` roda o
caso completo e escreve o relatório em JSON — é o ponto de entrada para
rodar como subprocesso (``process_host``), nunca em processo.

Tolerância do check Pinocchio: 5% PROVISÓRIA, declarada. A métrica exata
de divergência do G4.2 (">5%") está pendente do usuário (lacuna L4 do
Senado); 5% aqui é tolerância de trabalho do beta, nunca decisão final.

Domínio do fingerprint onde citado: DRAW:PAPER-APPROVAL:FINGERPRINT
(errata do Senado; "DRAW-AF/1" é só o rótulo da família).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AXIAL_BAR",
    "DEFAULT_TOLERANCE",
    "GRAVITY_M_S2",
    "PhysicsModelError",
    "PhysicsEngineError",
    "BarParams",
    "bar_mass_inertia",
    "bar_mjcf",
    "bar_urdf",
    "compare_envelopes",
    "envelope_from_series",
    "load_profile_for_fea",
    "model_sha256",
    "mujoco_available",
    "pinocchio_available",
    "pybullet_available",
    "run_contact_case",
    "run_mujoco_case",
    "run_pinocchio_check",
    "run_pybullet_case",
    "validate_params",
]

# ---------------------------------------------------------------------------
# Parâmetros declarados do caso beta
# ---------------------------------------------------------------------------

#: Gravidade usada nos três engines (m/s2).
GRAVITY_M_S2 = 9.81

#: Tolerância PROVISÓRIA do check Pinocchio (ver docstring do módulo).
DEFAULT_TOLERANCE = 0.05

#: Densidade DECLARADA do caso beta (aço, kg/m3). Não é propriedade medida
#: do caso FEA de referência (que não declara densidade); é o valor
#: declarado para a dinâmica. Trocar aqui se o caso declarar outro material.
DENSITY_DECLARED_KG_M3 = 7850.0

#: Módulo de Young do caso FEA de referência (210000 N/mm2 = 210 GPa).
YOUNG_MODULUS_PA = 210.0e9

#: Poisson do caso FEA de referência.
POISSON_RATIO = 0.3

#: Versão do modelo da barra (muda se qualquer parâmetro declarado mudar).
MODEL_VERSION = "axial_bar.v1"

#: Caso de contato do beta: altura de queda da face inferior ao plano (m).
DEFAULT_DROP_GAP_M = 0.005

#: Duração da simulação de contato (s).
DEFAULT_DURATION_S = 0.6


class PhysicsModelError(ValueError):
    """Modelo incompleto ou inválido — fail-closed, nunca inventar dado."""


class PhysicsEngineError(RuntimeError):
    """O engine falhou ou não está instalado (registrar, não fingir)."""


@dataclass(frozen=True)
class BarParams:
    """Parâmetros declarados da barra axial (SI).

    ``density_kg_m3`` é obrigatório: sem densidade declarada não há
    massa nem inércia, e o modelo é recusado (ver ``validate_params``).
    """

    length_m: float = 0.100
    width_m: float = 0.010
    height_m: float = 0.010
    density_kg_m3: float | None = None
    young_modulus_pa: float = YOUNG_MODULUS_PA
    poisson_ratio: float = POISSON_RATIO
    name: str = "axial_bar"
    model_version: str = MODEL_VERSION

    def declared_dict(self) -> dict[str, Any]:
        """Dicionário canônico dos parâmetros (ordem fixa, p/ hash)."""
        return {
            "name": self.name,
            "model_version": self.model_version,
            "length_m": self.length_m,
            "width_m": self.width_m,
            "height_m": self.height_m,
            "density_kg_m3": self.density_kg_m3,
            "young_modulus_pa": self.young_modulus_pa,
            "poisson_ratio": self.poisson_ratio,
        }


#: A barra do caso axial de referência, com densidade declarada (aço).
AXIAL_BAR = BarParams(density_kg_m3=DENSITY_DECLARED_KG_M3)


# ---------------------------------------------------------------------------
# Modelo: massa/inércia + URDF/MJCF a partir dos mesmos parâmetros
# ---------------------------------------------------------------------------

def validate_params(params: BarParams) -> None:
    """Fail-closed: dimensões e densidade têm que existir e ser positivas."""
    for field in ("length_m", "width_m", "height_m"):
        value = getattr(params, field)
        if value is None or not math.isfinite(value) or value <= 0:
            raise PhysicsModelError(f"{field} deve ser positivo e finito, got {value!r}")
    if params.density_kg_m3 is None:
        raise PhysicsModelError(
            "density_kg_m3 não declarada: sem densidade não há massa/inércia; "
            "declare a densidade do material (caso beta: aço, 7850 kg/m3)"
        )
    if not math.isfinite(params.density_kg_m3) or params.density_kg_m3 <= 0:
        raise PhysicsModelError(
            f"density_kg_m3 deve ser positiva e finita, got {params.density_kg_m3!r}"
        )
    if not math.isfinite(params.young_modulus_pa) or params.young_modulus_pa <= 0:
        raise PhysicsModelError(
            f"young_modulus_pa deve ser positivo e finito, got {params.young_modulus_pa!r}"
        )


def bar_mass_inertia(params: BarParams) -> tuple[float, tuple[float, float, float]]:
    """Massa e inércias principais (sobre o centro de massa) de um prisma.

    m = volume x densidade; Ixx = m (b2 + h2) / 12 etc. (eixo x = comprimento).
    """
    validate_params(params)
    volume_m3 = params.length_m * params.width_m * params.height_m
    mass_kg = volume_m3 * params.density_kg_m3
    lx, ly, lz = params.length_m, params.width_m, params.height_m
    ixx = mass_kg * (ly * ly + lz * lz) / 12.0
    iyy = mass_kg * (lx * lx + lz * lz) / 12.0
    izz = mass_kg * (lx * lx + ly * ly) / 12.0
    return mass_kg, (ixx, iyy, izz)


def model_sha256(params: BarParams) -> str:
    """Hash canônico dos parâmetros declarados (o que versiona o modelo)."""
    blob = json.dumps(params.declared_dict(), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def bar_urdf(params: BarParams) -> str:
    """URDF da barra, gerado dos parâmetros declarados (PyBullet/Pinocchio)."""
    mass_kg, (ixx, iyy, izz) = bar_mass_inertia(params)
    return (
        f'<?xml version="1.0"?>\n'
        f'<!-- {params.name} {params.model_version} model_sha256={model_sha256(params)} -->\n'
        f'<robot name="{params.name}">\n'
        f'  <link name="bar">\n'
        f'    <inertial>\n'
        f'      <origin xyz="0 0 0" rpy="0 0 0"/>\n'
        f'      <mass value="{mass_kg:.10g}"/>\n'
        f'      <inertia ixx="{ixx:.10g}" ixy="0" ixz="0" '
        f'iyy="{iyy:.10g}" iyz="0" izz="{izz:.10g}"/>\n'
        f'    </inertial>\n'
        f'    <visual>\n'
        f'      <origin xyz="0 0 0" rpy="0 0 0"/>\n'
        f'      <geometry><box size="{params.length_m:.10g} '
        f'{params.width_m:.10g} {params.height_m:.10g}"/></geometry>\n'
        f'    </visual>\n'
        f'    <collision>\n'
        f'      <origin xyz="0 0 0" rpy="0 0 0"/>\n'
        f'      <geometry><box size="{params.length_m:.10g} '
        f'{params.width_m:.10g} {params.height_m:.10g}"/></geometry>\n'
        f'    </collision>\n'
        f'  </link>\n'
        f'</robot>\n'
    )


def bar_mjcf(params: BarParams, drop_gap_m: float = DEFAULT_DROP_GAP_M) -> str:
    """MJCF da barra + plano, gerado dos MESMOS parâmetros (MuJoCo).

    A caixa MJCF usa semi-extensões; a barra cai de ``drop_gap_m``
    acima do plano, com o eixo x ao longo do comprimento. O ``freejoint``
    dá 6 DoF à barra (qpos 7: xyz + quat wxyz).
    """
    mass_kg, (ixx, iyy, izz) = bar_mass_inertia(params)
    hx, hy, hz = params.length_m / 2, params.width_m / 2, params.height_m / 2
    z0 = hz + drop_gap_m
    return (
        f'<?xml version="1.0"?>\n'
        f'<!-- {params.name} {params.model_version} model_sha256={model_sha256(params)} -->\n'
        f'<mujoco model="{params.name}">\n'
        f'  <option timestep="0.001" gravity="0 0 -{GRAVITY_M_S2}"/>\n'
        f'  <worldbody>\n'
        f'    <geom name="ground" type="plane" size="1 1 0.1" friction="0.5 0.1 0.1"/>\n'
        f'    <body name="bar" pos="0 0 {z0:.10g}">\n'
        f'      <freejoint/>\n'
        f'      <inertial pos="0 0 0" mass="{mass_kg:.10g}" '
        f'diaginertia="{ixx:.10g} {iyy:.10g} {izz:.10g}"/>\n'
        f'      <geom name="bar_geom" type="box" size="{hx:.10g} {hy:.10g} {hz:.10g}" '
        f'friction="0.5 0.1 0.1"/>\n'
        f'    </body>\n'
        f'  </worldbody>\n'
        f'</mujoco>\n'
    )


# ---------------------------------------------------------------------------
# Envelope de carga
# ---------------------------------------------------------------------------

def envelope_from_series(series: list[dict[str, float]]) -> dict[str, Any]:
    """Envelope a partir da série temporal de força normal (N).

    ``series``: [{"t_s": float, "f_normal_N": float}, ...] com tempo
    monotônico. Devolve f_max, instante do pico, RMS, p95, impulso
    (∫F dt, trapezoidal — transferência de momentum, robusta às
    rigidezes de contato distintas dos engines, ao contrário do pico
    instantâneo) e contagens.
    """
    if not series:
        raise PhysicsModelError("série de força vazia: sem envelope")
    forces = [float(s["f_normal_N"]) for s in series]
    times = [float(s["t_s"]) for s in series]
    if any(not math.isfinite(f) for f in forces) or any(
        not math.isfinite(t) for t in times
    ):
        raise PhysicsModelError("série com NaN/Inf: envelope recusado")
    if any(times[i + 1] <= times[i] for i in range(len(times) - 1)):
        raise PhysicsModelError("tempo da série não é monotônico")
    if any(f < 0 for f in forces):
        raise PhysicsModelError("força normal negativa na série")
    peak_idx = max(range(len(forces)), key=lambda i: forces[i])
    ordered = sorted(forces)
    p95_idx = min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)
    rms = math.sqrt(sum(f * f for f in forces) / len(forces))
    impulse = sum(
        0.5 * (forces[i] + forces[i + 1]) * (times[i + 1] - times[i])
        for i in range(len(forces) - 1)
    )
    return {
        "f_max_N": forces[peak_idx],
        "t_peak_s": times[peak_idx],
        "rms_N": rms,
        "p95_N": ordered[p95_idx],
        "impulse_Ns": impulse,
        "n_steps": len(series),
        "n_contact_steps": sum(1 for f in forces if f > 0),
        "duration_s": times[-1] - times[0],
    }


def compare_envelopes(
    pybullet_envelope: dict[str, Any], mujoco_envelope: dict[str, Any]
) -> dict[str, Any]:
    """Divergência PyBullet vs MuJoCo, relativa ao MuJoCo (verdade).

    Não é um gate de passa/falha: a métrica exata do G4.2 está pendente
    do usuário (L4). Só reporta os números.
    """
    out: dict[str, Any] = {"reference": "mujoco"}
    for key in ("f_max_N", "rms_N", "p95_N", "impulse_Ns"):
        ref = float(mujoco_envelope[key])
        val = float(pybullet_envelope[key])
        out[f"divergence_{key}"] = abs(val - ref) / ref if ref else float("nan")
        out[f"pybullet_{key}"] = val
        out[f"mujoco_{key}"] = ref
    out["t_peak_diff_s"] = float(pybullet_envelope["t_peak_s"]) - float(
        mujoco_envelope["t_peak_s"]
    )
    return out


def load_profile_for_fea(
    envelope: dict[str, Any],
    params: BarParams,
    engine: str,
    peak_point_m: list[float] | None = None,
) -> dict[str, Any]:
    """Perfil de carga (JSON) que o caso FEA pode consumir depois.

    Só agrega o envelope medido; não é resultado FEA nem tensão.
    """
    return {
        "profile": "physics.load_profile.v1",
        "engine": engine,
        "model": params.name,
        "model_version": params.model_version,
        "model_sha256": model_sha256(params),
        "units": {"force": "N", "time": "s", "length": "m"},
        "f_max_N": envelope["f_max_N"],
        "t_peak_s": envelope["t_peak_s"],
        "rms_N": envelope["rms_N"],
        "p95_N": envelope["p95_N"],
        "direction": [0.0, 0.0, 1.0],
        "peak_contact_point_m": peak_point_m,
        "note": "Envelope de contato medido na dinâmica; o caso FEA o consome "
        "como perfil de carga. Não é tensão nem resultado FEA.",
    }


# ---------------------------------------------------------------------------
# Disponibilidade dos engines (import preguiçoso; nunca no import do módulo)
# ---------------------------------------------------------------------------

def pybullet_available() -> bool:
    try:
        import pybullet  # noqa: F401
        return True
    except ImportError:
        return False


def mujoco_available() -> bool:
    try:
        import mujoco  # noqa: F401
        return True
    except ImportError:
        return False


def pinocchio_available() -> bool:
    try:
        import pinocchio  # noqa: F401
        return True
    except ImportError:
        return False


def _engine_version(module_name: str) -> str:
    module = sys.modules.get(module_name) or __import__(module_name)
    version = str(getattr(module, "__version__", "unknown"))
    if version != "unknown":
        return version
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version(module_name)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# PyBullet: primeiro — envelope de carga, headless p.DIRECT
# ---------------------------------------------------------------------------

def run_pybullet_case(
    params: BarParams = AXIAL_BAR,
    duration_s: float = DEFAULT_DURATION_S,
    drop_gap_m: float = DEFAULT_DROP_GAP_M,
) -> dict[str, Any]:
    """Caso de contato no PyBullet: barra cai sobre o plano; série de força.

    Índices de ``getContactPoints`` confirmados (ficha de robótica):
    [5] = positionOnA, [7] = contactNormalOnB, [9] = normalForce.
    """
    validate_params(params)
    try:
        import pybullet as p
        import pybullet_data
    except ImportError as exc:
        raise PhysicsEngineError(f"pybullet não instalado neste venv: {exc}") from exc

    timestep = 1.0 / 240.0
    n_steps = int(round(duration_s / timestep))
    series: list[dict[str, float]] = []
    peak_point: list[float] | None = None
    peak_force = -1.0
    client = p.connect(p.DIRECT)
    try:
        p.setAdditionalSearchPath(pybullet_data.getDataPath())
        p.setGravity(0, 0, -GRAVITY_M_S2)
        p.setTimeStep(timestep)
        plane_id = p.loadURDF("plane.urdf")
        with tempfile.TemporaryDirectory(prefix="ocre-physics-pb-") as tmp:
            urdf_path = Path(tmp) / "axial_bar.urdf"
            urdf_path.write_text(bar_urdf(params), encoding="utf-8")
            z0 = params.height_m / 2 + drop_gap_m
            bar_id = p.loadURDF(
                str(urdf_path), basePosition=[0, 0, z0], useFixedBase=False
            )
            for step in range(n_steps):
                p.stepSimulation()
                total = 0.0
                for contact in p.getContactPoints(bodyA=bar_id, bodyB=plane_id):
                    normal_force = float(contact[9])
                    if normal_force > 0:
                        total += normal_force
                        if normal_force > peak_force:
                            peak_force = normal_force
                            peak_point = [float(v) for v in contact[5]]
                series.append({"t_s": step * timestep, "f_normal_N": total})
    finally:
        p.disconnect(client)
    envelope = envelope_from_series(series)
    return {
        "engine": "pybullet",
        "version": _engine_version("pybullet"),
        "headless": "p.DIRECT",
        "model_sha256": model_sha256(params),
        "timestep_s": timestep,
        "duration_s": duration_s,
        "drop_gap_m": drop_gap_m,
        "gravity_m_s2": GRAVITY_M_S2,
        "envelope": envelope,
        "series": series,
        "peak_contact_point_m": peak_point,
    }


# ---------------------------------------------------------------------------
# MuJoCo: verdade — CPU determinístico, mj_step + mj_contactForce
# ---------------------------------------------------------------------------

def run_mujoco_case(
    params: BarParams = AXIAL_BAR,
    duration_s: float = DEFAULT_DURATION_S,
    drop_gap_m: float = DEFAULT_DROP_GAP_M,
) -> dict[str, Any]:
    """Mesmo caso no MuJoCo: força de contato 6D via ``mj_contactForce``.

    Não existe ``SensorContact`` no MuJoCo (nome do Newton); os sensores
    válidos seriam ``touch``/``force`` e aqui se lê ``data.contact``
    direto — um contato só conta se ``exclude == 0`` e a força for não-nula.
    """
    validate_params(params)
    try:
        import mujoco
        import numpy as np
    except ImportError as exc:
        raise PhysicsEngineError(f"mujoco não instalado neste venv: {exc}") from exc

    model = mujoco.MjModel.from_xml_string(bar_mjcf(params, drop_gap_m))
    data = mujoco.MjData(model)
    bar_geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "bar_geom")
    timestep = float(model.opt.timestep)
    n_steps = int(round(duration_s / timestep))
    series: list[dict[str, float]] = []
    peak_point: list[float] | None = None
    peak_force = -1.0
    force6 = np.zeros(6, dtype=np.float64)
    for step in range(n_steps):
        mujoco.mj_step(model, data)
        total = 0.0
        for i in range(data.ncon):
            contact = data.contact[i]
            if contact.exclude != 0:
                continue
            if contact.geom1 != bar_geom and contact.geom2 != bar_geom:
                continue
            mujoco.mj_contactForce(model, data, i, force6)
            normal = float(force6[0])
            if normal > 0:
                total += normal
                if normal > peak_force:
                    peak_force = normal
                    peak_point = [float(v) for v in contact.pos]
        series.append({"t_s": step * timestep, "f_normal_N": total})
    envelope = envelope_from_series(series)
    return {
        "engine": "mujoco",
        "version": _engine_version("mujoco"),
        "headless": "mj_step (CPU, sem viewer)",
        "model_sha256": model_sha256(params),
        "timestep_s": timestep,
        "duration_s": duration_s,
        "drop_gap_m": drop_gap_m,
        "gravity_m_s2": GRAVITY_M_S2,
        "envelope": envelope,
        "series": series,
        "peak_contact_point_m": peak_point,
    }


# ---------------------------------------------------------------------------
# Pinocchio: check — rnea/aba no MESMO modelo
# ---------------------------------------------------------------------------

def run_pinocchio_check(
    params: BarParams = AXIAL_BAR,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Check Pinocchio: rnea no mesmo URDF vs ``qfrc_bias`` do MuJoCo.

    Estado de teste: barra nivelada na pose inicial de queda, v = 0, a = 0
    (só gravidade). ``pin.rnea`` deve devolver o torque generalizado de
    gravidade, que o MuJoCo expõe como ``data.qfrc_bias`` no mesmo estado
    (``mj_forward``). ``pin.aba`` com esse torque deve devolver a ~= 0.
    Divergência além da tolerância (provisória, ver módulo) trava o check.
    """
    validate_params(params)
    if not pinocchio_available():
        return {
            "engine": "pinocchio",
            "available": False,
            "passed": False,
            "reason": "pin (Pinocchio) não instalado neste venv",
        }
    if not mujoco_available():
        return {
            "engine": "pinocchio",
            "available": True,
            "passed": False,
            "reason": "check precisa do MuJoCo (qfrc_bias) e ele não está instalado",
        }
    import numpy as np
    import pinocchio as pin

    import mujoco

    # --- Referência MuJoCo no estado de teste (pose inicial, v = 0). ---
    mj_model = mujoco.MjModel.from_xml_string(bar_mjcf(params))
    mj_data = mujoco.MjData(mj_model)
    mujoco.mj_forward(mj_model, mj_data)
    bias = np.array(mj_data.qfrc_bias, dtype=np.float64)  # nv = 6
    qpos_mj = np.array(mj_data.qpos, dtype=np.float64)  # xyz + quat wxyz

    # --- Mesmo modelo no Pinocchio (mesmo URDF, mesmas inércias). ---
    with tempfile.TemporaryDirectory(prefix="ocre-physics-pin-") as tmp:
        urdf_path = Path(tmp) / "axial_bar.urdf"
        urdf_path.write_text(bar_urdf(params), encoding="utf-8")
        model = pin.buildModelFromUrdf(str(urdf_path), pin.JointModelFreeFlyer())
    model.gravity = pin.Motion(
        np.array([0.0, 0.0, -GRAVITY_M_S2]), np.zeros(3)
    )
    data = model.createData()
    # Free-flyer: Pinocchio q = xyz + quat xyzw; MuJoCo qpos = xyz + quat wxyz.
    q = np.zeros(model.nq, dtype=np.float64)
    q[0:3] = qpos_mj[0:3]
    q[3:6] = qpos_mj[4:7]  # xyz
    q[6] = qpos_mj[3]  # w
    v = np.zeros(model.nv, dtype=np.float64)
    a = np.zeros(model.nv, dtype=np.float64)
    tau_pin = np.array(pin.rnea(model, data, q, v, a), dtype=np.float64)
    a_aba = np.array(pin.aba(model, data, q, v, tau_pin), dtype=np.float64)

    diff = np.abs(tau_pin - bias)
    scale = float(np.max(np.abs(bias))) if np.max(np.abs(bias)) > 0 else 1.0
    max_rel = float(np.max(diff) / scale)
    passed = bool(max_rel <= tolerance and float(np.max(np.abs(a_aba))) < 1e-6)
    return {
        "engine": "pinocchio",
        "version": _engine_version("pinocchio"),
        "available": True,
        "model_sha256": model_sha256(params),
        "tolerance": tolerance,
        "tolerance_status": "PROVISÓRIA — métrica exata do G4.2 pendente do usuário (L4)",
        "tau_pinocchio": [float(x) for x in tau_pin],
        "qfrc_bias_mujoco": [float(x) for x in bias],
        "max_abs_diff": float(np.max(diff)),
        "max_rel_diff": max_rel,
        "max_abs_accel_aba": float(np.max(np.abs(a_aba))),
        "passed": passed,
        "reason": (
            "rnea concorda com qfrc_bias dentro da tolerância e aba devolve a~=0"
            if passed
            else f"divergência {max_rel:.4f} além da tolerância {tolerance} "
            f"ou aba com aceleração residual — check TRAVADO"
        ),
    }


# ---------------------------------------------------------------------------
# Orquestração do caso + CLI de subprocesso
# ---------------------------------------------------------------------------

def run_contact_case(
    params: BarParams = AXIAL_BAR,
    duration_s: float = DEFAULT_DURATION_S,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Roda PyBullet + MuJoCo + check Pinocchio e compara os envelopes.

    Engine indisponível não é fingido: entra no relatório como
    ``status: "unavailable"`` com o motivo, e o envelope dele fica ausente.
    """
    report: dict[str, Any] = {
        "case": "axial_bar_contact.v1",
        "model": params.name,
        "model_version": params.model_version,
        "model_sha256": model_sha256(params),
        "fingerprint_domain": "DRAW:PAPER-APPROVAL:FINGERPRINT",
        "mass_kg": bar_mass_inertia(params)[0],
        "engines": {},
    }
    results: dict[str, Any] = {}
    for name, runner in (
        ("pybullet", run_pybullet_case),
        ("mujoco", run_mujoco_case),
    ):
        try:
            result = runner(params, duration_s=duration_s)
            result["status"] = "ok"
            report["engines"][name] = result
            results[name] = result
        except PhysicsEngineError as exc:
            report["engines"][name] = {"status": "unavailable", "reason": str(exc)}
    pb_result = results.get("pybullet")
    mj_result = results.get("mujoco")
    if pb_result is not None and mj_result is not None:
        report["comparison"] = compare_envelopes(
            pb_result["envelope"], mj_result["envelope"]
        )
        report["load_profile_pybullet"] = load_profile_for_fea(
            pb_result["envelope"], params, "pybullet", pb_result["peak_contact_point_m"]
        )
        report["load_profile_mujoco"] = load_profile_for_fea(
            mj_result["envelope"], params, "mujoco", mj_result["peak_contact_point_m"]
        )
    else:
        report["comparison"] = None
        report["load_profile_pybullet"] = None
        report["load_profile_mujoco"] = None
    report["pinocchio_check"] = run_pinocchio_check(params, tolerance=tolerance)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI de subprocesso: roda o caso e escreve o relatório em JSON."""
    parser = argparse.ArgumentParser(description="Caso de contato da barra axial (beta).")
    parser.add_argument("--out", required=True, help="caminho do relatório JSON")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    args = parser.parse_args(argv)
    report = run_contact_case(duration_s=args.duration, tolerance=args.tolerance)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "model_sha256": report["model_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
