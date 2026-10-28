import math
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ocre_core.integrations.process_host import (
    EnvironmentMode,
    ProcessBatchHost,
    ProcessBatchSpec,
)


class ProcessBatchHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.host = ProcessBatchHost(
            self.root / "runs",
            maximum_log_bytes_per_stream=128,
            termination_grace_seconds=0.1,
        )

    def spec(self, *arguments, timeout=2, environment=None, mode=EnvironmentMode.CLEAN, inherited=()):
        return ProcessBatchSpec(
            argv=(sys.executable, "-c", *arguments),
            timeout_seconds=timeout,
            environment_mode=mode,
            environment=environment or {},
            inherited_environment_names=inherited,
        )

    def test_argv_is_literal_and_each_run_has_its_own_cwd(self):
        marker = "$(touch SHOULD_NOT_RUN); spaces"
        code = "import os,sys; print(sys.argv[1]); print(os.getcwd())"
        first = self.host.run(self.spec(code, marker))
        second = self.host.run(self.spec(code, marker))
        self.assertEqual(first.exit_code, 0)
        self.assertIn(marker, first.stdout.text)
        self.assertNotEqual(first.working_directory, second.working_directory)
        self.assertEqual(first.working_directory.parent, self.host.workspace_root)
        self.assertFalse((self.root / "SHOULD_NOT_RUN").exists())

    def test_clean_environment_and_explicit_allowlist(self):
        os.environ["OCRE_TEST_INHERITED"] = "from-parent"
        self.addCleanup(os.environ.pop, "OCRE_TEST_INHERITED", None)
        code = "import os; print(os.getenv('OCRE_TEST_INHERITED', 'missing')); print(os.getenv('OCRE_TEST_SET', 'missing'))"
        clean = self.host.run(self.spec(code))
        self.assertEqual(clean.stdout.text.strip().splitlines(), ["missing", "missing"])

        allowlisted = self.host.run(self.spec(
            code,
            environment={"OCRE_TEST_SET": "configured"},
            mode=EnvironmentMode.INHERIT_ALLOWLIST,
            inherited=("OCRE_TEST_INHERITED",),
        ))
        self.assertEqual(allowlisted.stdout.text.strip().splitlines(), ["from-parent", "configured"])

    def test_timeout_terminates_process_group_and_reaps_direct_child(self):
        result = self.host.run(self.spec("import time; time.sleep(30)", timeout=0.1))
        self.assertTrue(result.timed_out)
        self.assertIsNotNone(result.exit_code)

    def test_timeout_also_covers_descendant_holding_open_logs(self):
        code = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
        result = self.host.run(self.spec(code, timeout=0.1))
        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_seconds, 2)

    def test_stdout_stderr_are_drained_and_capture_is_bounded(self):
        code = "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)"
        result = self.host.run(self.spec(code))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.stdout.text), 128)
        self.assertEqual(len(result.stderr.text), 128)
        self.assertTrue(result.stdout.truncated)
        self.assertTrue(result.stderr.truncated)

    def test_nonzero_exit_is_reported_without_interpreting_as_success(self):
        result = self.host.run(self.spec("raise SystemExit(7)"))
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)

    def test_rejects_relative_executable_and_invalid_environment_policy(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            self.host.run(ProcessBatchSpec(
                argv=("python", "-c", "pass"), timeout_seconds=1,
                environment_mode=EnvironmentMode.CLEAN, environment={},
            ))
        with self.assertRaisesRegex(ValueError, "cannot inherit"):
            self.host.run(ProcessBatchSpec(
                argv=(sys.executable, "-c", "pass"), timeout_seconds=1,
                environment_mode=EnvironmentMode.CLEAN, environment={},
                inherited_environment_names=("PATH",),
            ))

    def test_rejects_infinite_timeout(self):
        for timeout in (math.inf, -math.inf, math.nan):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "finite and positive"):
                self.host.run(self.spec("pass", timeout=timeout))

    def test_rejects_non_finite_shutdown_grace(self):
        for grace in (math.inf, -math.inf, math.nan):
            with self.subTest(grace=grace), self.assertRaisesRegex(ValueError, "finite and non-negative"):
                ProcessBatchHost(self.root / f"runs-{str(grace)}", termination_grace_seconds=grace)


if __name__ == "__main__":
    unittest.main()
