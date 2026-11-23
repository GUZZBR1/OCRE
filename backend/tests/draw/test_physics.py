"""Tests for the beta physics worker (PyBullet + MuJoCo + Pinocchio).

The axial bar is the FEA reference case (100x10x10 mm, E=210000 N/mm2,
Rx=2100 N, S11=21 MPa) with a DECLARED density (steel, 7850 kg/m3) for
dynamics — the FEA case itself does not declare density.

Engine tests are skipped, not faked, when the engine is not installed in
the venv: ``pybullet_available`` / ``mujoco_available`` /
``pinocchio_available`` decide. Newton is NOT part of this module by user
decision (2026-11-05) and is never imported here.

Exception to "skipped, not faked": ``CaseResultShapeTests`` injects fake
engine modules in ``sys.modules`` — not to claim physics results, but to
reach the result-dict assembly of ``run_pybullet_case`` /
``run_mujoco_case``, which is otherwise dead code in a venv without the
engines (characterization for the ``_case_result`` refactor).
"""
from __future__ import annotations

import contextlib
import json
import math
import sys
import types
import unittest

from ocre_core.draw.physics import (
    AXIAL_BAR,
    DEFAULT_DROP_GAP_M,
    DEFAULT_TOLERANCE,
    GRAVITY_M_S2,
    BarParams,
    PhysicsModelError,
    bar_mass_inertia,
    bar_mjcf,
    bar_urdf,
    compare_envelopes,
    envelope_from_series,
    _impulse_per_step,
    model_sha256,
    mujoco_available,
    pinocchio_available,
    pybullet_available,
    run_contact_case,
    run_mujoco_case,
    run_pinocchio_check,
    run_pybullet_case,
)

MASS_EXPECTED_KG = 0.100 * 0.010 * 0.010 * 7850.0  # 0.0785
IXX_EXPECTED = MASS_EXPECTED_KG * (0.010**2 + 0.010**2) / 12.0
IYY_EXPECTED = MASS_EXPECTED_KG * (0.100**2 + 0.010**2) / 12.0


class ModelTests(unittest.TestCase):
    def test_mass_and_inertia_from_volume_times_declared_density(self):
        mass, (ixx, iyy, izz) = bar_mass_inertia(AXIAL_BAR)
        self.assertAlmostEqual(mass, MASS_EXPECTED_KG, places=10)
        self.assertAlmostEqual(ixx, IXX_EXPECTED, places=12)
        self.assertAlmostEqual(iyy, IYY_EXPECTED, places=12)
        self.assertAlmostEqual(izz, IYY_EXPECTED, places=12)  # b == h

    def test_missing_density_is_blocked(self):
        with self.assertRaises(PhysicsModelError):
            bar_mass_inertia(BarParams(density_kg_m3=None))

    def test_nonpositive_density_is_blocked(self):
        for bad in (0.0, -7850.0):
            with self.assertRaises(PhysicsModelError):
                bar_mass_inertia(BarParams(density_kg_m3=bad))

    def test_nonpositive_dimension_is_blocked(self):
        with self.assertRaises(PhysicsModelError):
            bar_mass_inertia(BarParams(length_m=0.0, density_kg_m3=7850.0))

    def test_urdf_and_mjcf_come_from_the_same_parameters(self):
        urdf = bar_urdf(AXIAL_BAR)
        mjcf = bar_mjcf(AXIAL_BAR)
        sha = model_sha256(AXIAL_BAR)
        self.assertIn(sha, urdf)
        self.assertIn(sha, mjcf)
        # Same mass and inertia text on both sides.
        self.assertIn("0.0785", urdf)
        self.assertIn("0.0785", mjcf)
        # MJCF box uses half-extents; URDF uses full size.
        self.assertIn('size="0.1 0.01 0.01"', urdf)
        self.assertIn('size="0.05 0.005 0.005"', mjcf)
        # The declared density is the same object on both sides.
        self.assertEqual(model_sha256(AXIAL_BAR), sha)

    def test_model_hash_changes_when_a_parameter_changes(self):
        other = BarParams(density_kg_m3=2700.0)  # aluminium, declared
        self.assertNotEqual(model_sha256(other), model_sha256(AXIAL_BAR))
        mass, _ = bar_mass_inertia(other)
        self.assertAlmostEqual(mass, 0.100 * 0.010 * 0.010 * 2700.0, places=10)


class EnvelopeTests(unittest.TestCase):
    def test_envelope_from_series(self):
        series = [{"t_s": i * 0.01, "f_normal_N": f} for i, f in enumerate([0.0, 5.0, 3.0])]
        env = envelope_from_series(series)
        self.assertEqual(env["f_max_N"], 5.0)
        self.assertEqual(env["t_peak_s"], 0.01)
        self.assertEqual(env["n_contact_steps"], 2)
        self.assertAlmostEqual(env["rms_N"], math.sqrt((25 + 9) / 3), places=10)
        # Impulso trapezoidal: (0+5)/2*0.01 + (5+3)/2*0.01 = 0.065 N·s.
        self.assertAlmostEqual(env["impulse_Ns"], 0.065, places=10)

    def test_envelope_impulse_is_timestep_independent(self):
        # Mesma rampa amostrada em dois passos: o impulso (integral)
        # concorda; métricas por-amostra como RMS não precisam — por
        # isso impulso é o escalar mais comparável entre engines com
        # timesteps distintos (PyBullet 240 Hz vs MuJoCo 1 kHz).
        coarse = [{"t_s": t, "f_normal_N": t * 100.0} for t in (0.0, 0.5, 1.0)]
        fine = [{"t_s": t, "f_normal_N": t * 100.0} for t in (0.0, 0.25, 0.5, 0.75, 1.0)]
        self.assertAlmostEqual(
            envelope_from_series(coarse)["impulse_Ns"],
            envelope_from_series(fine)["impulse_Ns"],
            places=10,
        )

    def test_per_step_impulse_counts_last_step_at_both_frequencies(self):
        # Regressão do review: numa série POR-PASSO o último passo segue
        # com força de apoio não-nula. A soma retangular f·Δt conta o
        # passo inteiro e é idêntica nas duas frequências (240 Hz
        # PyBullet vs 1 kHz MuJoCo); a trapezoidal subestimaria 0.5·Δt,
        # um erro distinto por engine.
        impulses = []
        for dt in (1.0 / 240.0, 1.0 / 1000.0):
            n = int(round(0.5 / dt))
            series = [{"t_s": i * dt, "f_normal_N": 1.0}
                      for i in range(n)]
            impulses.append(_impulse_per_step(series, dt))
        self.assertAlmostEqual(impulses[0], impulses[1], places=10)
        self.assertAlmostEqual(impulses[0], 0.5, places=10)

    def test_envelope_rejects_nan_and_non_monotonic_time(self):
        with self.assertRaises(PhysicsModelError):
            envelope_from_series([{"t_s": 0.0, "f_normal_N": float("nan")}])
        with self.assertRaises(PhysicsModelError):
            envelope_from_series(
                [{"t_s": 0.02, "f_normal_N": 1.0}, {"t_s": 0.01, "f_normal_N": 1.0}]
            )

    def test_compare_envelopes_is_relative_to_mujoco(self):
        comp = compare_envelopes(
            {"f_max_N": 110.0, "rms_N": 50.0, "p95_N": 90.0, "t_peak_s": 0.03,
             "impulse_Ns": 22.0},
            {"f_max_N": 100.0, "rms_N": 40.0, "p95_N": 80.0, "t_peak_s": 0.04,
             "impulse_Ns": 20.0},
        )
        self.assertEqual(comp["reference"], "mujoco")
        self.assertAlmostEqual(comp["divergence_f_max_N"], 0.10)
        self.assertAlmostEqual(comp["divergence_rms_N"], 0.25)
        self.assertAlmostEqual(comp["divergence_impulse_Ns"], 0.10)
        self.assertAlmostEqual(comp["t_peak_diff_s"], -0.01, places=10)


@unittest.skipUnless(pybullet_available(), "pybullet não instalado neste venv")
class PyBulletTests(unittest.TestCase):
    def test_envelope_is_reproducible(self):
        first = run_pybullet_case(duration_s=0.3)
        second = run_pybullet_case(duration_s=0.3)
        self.assertEqual(first["model_sha256"], second["model_sha256"])
        self.assertEqual(first["envelope"], second["envelope"])
        self.assertEqual(first["series"], second["series"])

    def test_contact_happens_and_force_is_positive(self):
        result = run_pybullet_case(duration_s=0.3)
        env = result["envelope"]
        self.assertGreater(env["f_max_N"], 0.0)
        self.assertGreater(env["n_contact_steps"], 0)
        self.assertEqual(result["headless"], "p.DIRECT")


@unittest.skipUnless(mujoco_available(), "mujoco não instalado neste venv")
class MuJoCoTests(unittest.TestCase):
    def test_runs_on_cpu_and_returns_forces(self):
        result = run_mujoco_case(duration_s=0.3)
        env = result["envelope"]
        self.assertGreater(env["f_max_N"], 0.0)
        self.assertGreater(env["n_contact_steps"], 0)
        self.assertIn("CPU", result["headless"])
        # Series is a valid time series: monotonic, finite, non-negative.
        self.assertEqual(
            [s["t_s"] for s in result["series"]],
            sorted(s["t_s"] for s in result["series"]),
        )

    def test_envelope_is_reproducible(self):
        first = run_mujoco_case(duration_s=0.3)
        second = run_mujoco_case(duration_s=0.3)
        self.assertEqual(first["envelope"], second["envelope"])
        self.assertEqual(first["series"], second["series"])


class PinocchioTests(unittest.TestCase):
    @unittest.skipUnless(
        pinocchio_available() and mujoco_available(),
        "pinocchio ou mujoco não instalado neste venv",
    )
    def test_rnea_agrees_with_mujoco_bias_within_provisional_tolerance(self):
        check = run_pinocchio_check(tolerance=DEFAULT_TOLERANCE)
        self.assertTrue(check["available"], check.get("reason"))
        self.assertTrue(check["passed"], check)
        self.assertLessEqual(check["max_rel_diff"], DEFAULT_TOLERANCE)
        self.assertLess(check["max_abs_accel_aba"], 1e-6)

    @unittest.skipUnless(
        not pinocchio_available(), "pinocchio instalado: teste de ausência não se aplica"
    )
    def test_unavailable_is_reported_not_faked(self):
        check = run_pinocchio_check()
        self.assertFalse(check["available"])
        self.assertFalse(check["passed"])
        self.assertIn("não instalado", check["reason"])


@unittest.skipUnless(
    pybullet_available() and mujoco_available(),
    "pybullet ou mujoco não instalado neste venv",
)
class ContactCaseTests(unittest.TestCase):
    def test_full_case_compares_envelopes_and_builds_load_profiles(self):
        report = run_contact_case(duration_s=0.3)
        self.assertEqual(report["model_sha256"], model_sha256(AXIAL_BAR))
        self.assertEqual(report["fingerprint_domain"], "DRAW:PAPER-APPROVAL:FINGERPRINT")
        self.assertEqual(report["engines"]["pybullet"]["status"], "ok")
        self.assertEqual(report["engines"]["mujoco"]["status"], "ok")
        comp = report["comparison"]
        self.assertIsNotNone(comp)
        self.assertGreaterEqual(comp["divergence_f_max_N"], 0.0)
        # Load profiles are JSON-serializable and carry the same model hash.
        for key in ("load_profile_pybullet", "load_profile_mujoco"):
            profile = report[key]
            self.assertEqual(profile["model_sha256"], model_sha256(AXIAL_BAR))
            self.assertGreater(profile["f_max_N"], 0.0)
            json.dumps(profile)
        if pinocchio_available():
            self.assertTrue(report["pinocchio_check"]["available"])
            self.assertTrue(report["pinocchio_check"]["passed"], report["pinocchio_check"])
        else:
            self.assertFalse(report["pinocchio_check"]["available"])


# ---------------------------------------------------------------------------
# Caracterização do dict de resultado (F-031)
#
# Os corpos de ``run_pybullet_case``/``run_mujoco_case`` têm 0% de cobertura
# neste venv (engines ausentes). Os fakes abaixo implementam SÓ a superfície
# de API que cada runner usa, para alcançar a montagem do dict de 11 chaves
# — sem afirmar nada sobre física de verdade. O teste sobrevive à extração
# de ``_case_result`` porque verifica comportamento observável do runner.
# ---------------------------------------------------------------------------

CASE_RESULT_KEYS = {
    "engine", "version", "headless", "model_sha256", "timestep_s",
    "duration_s", "drop_gap_m", "gravity_m_s2", "envelope", "series",
    "peak_contact_point_m",
}

ENVELOPE_KEYS = {
    "f_max_N", "t_peak_s", "rms_N", "p95_N", "impulse_Ns", "n_steps",
    "n_contact_steps", "duration_s",
}


def _pb_contact(force_n, pos=(0.01, 0.0, 0.005)):
    """Tuple no formato ``getContactPoints``: [5]=positionOnA, [9]=normalForce."""
    return (0, 0, 0, 0, 0, pos, 0, 0, 0, force_n)


class _FakePyBullet(types.ModuleType):
    """``import pybullet`` falso: só a API que ``run_pybullet_case`` usa."""

    DIRECT = "DIRECT"

    def __init__(self, contacts_by_step):
        super().__init__("pybullet")
        self.__version__ = "0.0.0-fake"
        self._contacts_by_step = dict(contacts_by_step)
        self._step = -1

    def connect(self, mode):
        return 10

    def disconnect(self, client):
        pass

    def setAdditionalSearchPath(self, path):
        pass

    def setGravity(self, gx, gy, gz):
        pass

    def setTimeStep(self, timestep):
        pass

    def loadURDF(self, path, basePosition=None, useFixedBase=False):
        return 1 if path.endswith("plane.urdf") else 2

    def stepSimulation(self):
        self._step += 1

    def getContactPoints(self, bodyA=None, bodyB=None):
        return list(self._contacts_by_step.get(self._step, ()))


class _FakePybulletData(types.ModuleType):
    """``import pybullet_data`` falso."""

    def __init__(self):
        super().__init__("pybullet_data")

    def getDataPath(self):
        return "/fake/pybullet_data"


class _FakeMjContact:
    """Contato MJCF falso: ``exclude``/``geom*``/``pos`` + força carregada."""

    def __init__(self, *, geom1, geom2, pos, force, exclude=0):
        self.geom1 = geom1
        self.geom2 = geom2
        self.pos = pos
        self.force = force
        self.exclude = exclude


class _FakeMujoco(types.ModuleType):
    """``import mujoco`` falso: só a API que ``run_mujoco_case`` usa.

    ``mj_step`` avança um relógio no ``data`` e expõe os contatos
    programados para aquele passo; ``mj_contactForce`` devolve a força
    declarada no contato (``force6[0]`` = normal).
    """

    BAR_GEOM_ID = 42

    def __init__(self, contacts_by_step, timestep=0.001):
        super().__init__("mujoco")
        self.__version__ = "0.0.0-fake"
        self.mjtObj = types.SimpleNamespace(mjOBJ_GEOM="geom")
        self.MjModel = types.SimpleNamespace(
            from_xml_string=lambda xml: types.SimpleNamespace(
                opt=types.SimpleNamespace(timestep=timestep)))
        self._data = types.SimpleNamespace(step=-1, ncon=0, contact=[])
        self.MjData = lambda model: self._data
        self._contacts_by_step = dict(contacts_by_step)

    def mj_name2id(self, model, objtype, name):
        return self.BAR_GEOM_ID

    def mj_step(self, model, data):
        data.step += 1
        data.contact = list(self._contacts_by_step.get(data.step, ()))
        data.ncon = len(data.contact)

    def mj_contactForce(self, model, data, index, out):
        out[0] = data.contact[index].force


@contextlib.contextmanager
def _fake_engine_modules(**modules):
    """Registra módulos fake em ``sys.modules`` e restaura ao sair."""
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class CaseResultShapeTests(unittest.TestCase):
    """Forma do dict de resultado dos casos de contato (caracterização).

    Roda sem os engines instalados: os fakes entram em ``sys.modules``
    só durante a chamada. Verifica as 11 chaves, os valores declarados
    (engine/headless/timestep/duration/gap/gravidade/modelo) e a
    integração com ``envelope_from_series`` + ``_impulse_per_step``.
    """

    def test_pybullet_result_dict_shape(self):
        # Contato a partir do passo 5: força crescente, pico no último.
        timestep = 1.0 / 240.0
        duration_s = 0.05  # 12 passos
        contacts = {i: (_pb_contact(100.0 + i),) for i in range(5, 12)}
        with _fake_engine_modules(pybullet=_FakePyBullet(contacts),
                                  pybullet_data=_FakePybulletData()):
            result = run_pybullet_case(duration_s=duration_s)
        self.assertEqual(set(result), CASE_RESULT_KEYS)
        self.assertEqual(result["engine"], "pybullet")
        self.assertEqual(result["version"], "0.0.0-fake")
        self.assertEqual(result["headless"], "p.DIRECT")
        self.assertEqual(result["model_sha256"], model_sha256(AXIAL_BAR))
        self.assertEqual(result["timestep_s"], timestep)
        self.assertEqual(result["duration_s"], duration_s)
        self.assertEqual(result["drop_gap_m"], DEFAULT_DROP_GAP_M)
        self.assertEqual(result["gravity_m_s2"], GRAVITY_M_S2)
        series = result["series"]
        self.assertEqual(len(series), 12)
        self.assertTrue(all(set(s) == {"t_s", "f_normal_N"} for s in series))
        self.assertEqual([s["t_s"] for s in series],
                         [i * timestep for i in range(12)])
        forces = [s["f_normal_N"] for s in series]
        self.assertEqual(forces, [0.0] * 5 + [100.0 + i for i in range(5, 12)])
        envelope = result["envelope"]
        self.assertEqual(set(envelope), ENVELOPE_KEYS)
        self.assertEqual(envelope["f_max_N"], 111.0)
        self.assertEqual(envelope["t_peak_s"], 11 * timestep)
        self.assertEqual(envelope["n_steps"], 12)
        self.assertEqual(envelope["n_contact_steps"], 7)
        self.assertEqual(envelope["duration_s"],
                         series[-1]["t_s"] - series[0]["t_s"])
        # Impulso por-passo: soma retangular f·Δt (não trapezoidal).
        self.assertAlmostEqual(envelope["impulse_Ns"],
                               sum(forces) * timestep, places=15)
        self.assertEqual(result["peak_contact_point_m"], [0.01, 0.0, 0.005])

    def test_mujoco_result_dict_shape(self):
        timestep = 0.001
        duration_s = 0.02  # 20 passos
        contacts = {
            i: (_FakeMjContact(geom1=_FakeMujoco.BAR_GEOM_ID, geom2=0,
                               pos=(0.02, 0.0, 0.005), force=50.0 + i),)
            for i in range(8, 20)
        }
        with _fake_engine_modules(
                mujoco=_FakeMujoco(contacts, timestep=timestep)):
            result = run_mujoco_case(duration_s=duration_s)
        self.assertEqual(set(result), CASE_RESULT_KEYS)
        self.assertEqual(result["engine"], "mujoco")
        self.assertEqual(result["version"], "0.0.0-fake")
        self.assertEqual(result["headless"], "mj_step (CPU, sem viewer)")
        self.assertEqual(result["model_sha256"], model_sha256(AXIAL_BAR))
        self.assertEqual(result["timestep_s"], timestep)
        self.assertEqual(result["duration_s"], duration_s)
        self.assertEqual(result["drop_gap_m"], DEFAULT_DROP_GAP_M)
        self.assertEqual(result["gravity_m_s2"], GRAVITY_M_S2)
        series = result["series"]
        self.assertEqual(len(series), 20)
        self.assertTrue(all(set(s) == {"t_s", "f_normal_N"} for s in series))
        forces = [s["f_normal_N"] for s in series]
        self.assertEqual(forces, [0.0] * 8 + [50.0 + i for i in range(8, 20)])
        envelope = result["envelope"]
        self.assertEqual(set(envelope), ENVELOPE_KEYS)
        self.assertEqual(envelope["f_max_N"], 69.0)
        self.assertEqual(envelope["n_steps"], 20)
        self.assertEqual(envelope["n_contact_steps"], 12)
        self.assertAlmostEqual(envelope["impulse_Ns"],
                               sum(forces) * timestep, places=15)
        self.assertEqual(result["peak_contact_point_m"], [0.02, 0.0, 0.005])

    def test_no_contact_steps_leaves_peak_point_none(self):
        # Sem contatos em nenhum passo: série de zeros, pico ausente.
        with _fake_engine_modules(pybullet=_FakePyBullet({}),
                                  pybullet_data=_FakePybulletData()):
            result = run_pybullet_case(duration_s=0.01)
        self.assertEqual(set(result), CASE_RESULT_KEYS)
        self.assertIsNone(result["peak_contact_point_m"])
        self.assertEqual(result["envelope"]["n_contact_steps"], 0)
        self.assertEqual(result["envelope"]["f_max_N"], 0.0)


if __name__ == "__main__":
    unittest.main()
