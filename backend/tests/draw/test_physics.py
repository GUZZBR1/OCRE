"""Tests for the beta physics worker (PyBullet + MuJoCo + Pinocchio).

The axial bar is the FEA reference case (100x10x10 mm, E=210000 N/mm2,
Rx=2100 N, S11=21 MPa) with a DECLARED density (steel, 7850 kg/m3) for
dynamics — the FEA case itself does not declare density.

Engine tests are skipped, not faked, when the engine is not installed in
the venv: ``pybullet_available`` / ``mujoco_available`` /
``pinocchio_available`` decide. Newton is NOT part of this module by user
decision (2026-11-05) and is never imported here.
"""
from __future__ import annotations

import json
import math
import unittest

from ocre_core.draw.physics import (
    AXIAL_BAR,
    DEFAULT_TOLERANCE,
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


if __name__ == "__main__":
    unittest.main()
