"""POSIX subprocess host with bounded logs; this is not a security sandbox."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping


class EnvironmentMode(str, Enum):
    CLEAN = "clean"
    INHERIT_ALLOWLIST = "inherit_allowlist"


@dataclass(frozen=True)
class ProcessBatchSpec:
    argv: tuple[str, ...]
    timeout_seconds: float
    environment_mode: EnvironmentMode
    environment: Mapping[str, str]
    inherited_environment_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapturedLog:
    text: str
    truncated: bool


@dataclass(frozen=True)
class ProcessBatchResult:
    exit_code: int
    timed_out: bool
    working_directory: Path
    stdout: CapturedLog
    stderr: CapturedLog
    duration_seconds: float


class ProcessLaunchError(RuntimeError):
    """The configured executable could not be started."""


class _BoundedDrain(threading.Thread):
    def __init__(self, pipe, maximum_bytes: int):
        super().__init__(daemon=True)
        self.pipe = pipe
        self.maximum_bytes = maximum_bytes
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        while True:
            chunk = self.pipe.read(65536)
            if not chunk:
                break
            remaining = self.maximum_bytes - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True
        self.pipe.close()

    def result(self) -> CapturedLog:
        return CapturedLog(self.data.decode("utf-8", errors="replace"), self.truncated)


class ProcessBatchHost:
    """Runs a trusted adapter-configured executable in a fresh per-run cwd.

    The worker supervisor/container must provide CPU, memory, disk, network and
    filesystem isolation, privilege dropping and stronger descendant control.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        maximum_log_bytes_per_stream: int = 256_000,
        termination_grace_seconds: float = 1.0,
    ) -> None:
        if os.name != "posix":
            raise RuntimeError("ProcessBatchHost currently supports POSIX only")
        if maximum_log_bytes_per_stream < 1:
            raise ValueError("maximum_log_bytes_per_stream must be positive")
        if not math.isfinite(termination_grace_seconds) or termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds must be finite and non-negative")
        workspace_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root = workspace_root.resolve(strict=True)
        self.maximum_log_bytes_per_stream = maximum_log_bytes_per_stream
        self.termination_grace_seconds = termination_grace_seconds

    def run(self, spec: ProcessBatchSpec) -> ProcessBatchResult:
        self._validate_spec(spec)
        working_directory = Path(
            tempfile.mkdtemp(prefix="execution-", dir=self.workspace_root)
        ).resolve(strict=True)
        if working_directory.parent != self.workspace_root:
            raise RuntimeError("execution directory escaped workspace root")

        environment = self._environment(spec)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessLaunchError(f"configured executable could not start: {exc.strerror}") from exc

        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedDrain(process.stdout, self.maximum_log_bytes_per_stream)
        stderr = _BoundedDrain(process.stderr, self.maximum_log_bytes_per_stream)
        stdout.start()
        stderr.start()
        timed_out = False
        deadline = started + spec.timeout_seconds
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(process)
        for drain in (stdout, stderr):
            drain.join(timeout=max(0, deadline - time.monotonic()))
        if stdout.is_alive() or stderr.is_alive():
            timed_out = True
            self._terminate_process_group(process)
            stdout.join()
            stderr.join()
        return ProcessBatchResult(
            exit_code=process.returncode,
            timed_out=timed_out,
            working_directory=working_directory,
            stdout=stdout.result(),
            stderr=stderr.result(),
            duration_seconds=time.monotonic() - started,
        )

    def _terminate_process_group(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        grace_deadline = time.monotonic() + self.termination_grace_seconds
        try:
            process.wait(timeout=max(0, grace_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        remaining_grace = grace_deadline - time.monotonic()
        if remaining_grace > 0:
            time.sleep(remaining_grace)
        # The direct child may have exited while descendants still hold pipes.
        # Signal the process group regardless, then reap the direct child.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    @staticmethod
    def _validate_spec(spec: ProcessBatchSpec) -> None:
        if not spec.argv or not spec.argv[0]:
            raise ValueError("argv must include a configured executable")
        executable = Path(spec.argv[0])
        if not executable.is_absolute():
            raise ValueError("configured executable must be an absolute path")
        if any("\x00" in argument for argument in spec.argv):
            raise ValueError("argv arguments must not contain NUL")
        if not math.isfinite(spec.timeout_seconds) or spec.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if spec.environment_mode is EnvironmentMode.CLEAN and spec.inherited_environment_names:
            raise ValueError("clean environment cannot inherit names")
        if len(set(spec.inherited_environment_names)) != len(spec.inherited_environment_names):
            raise ValueError("inherited environment names must be unique")
        for name, value in spec.environment.items():
            if not name or "=" in name or "\x00" in name or "\x00" in value:
                raise ValueError("invalid environment variable")

    @staticmethod
    def _environment(spec: ProcessBatchSpec) -> dict[str, str]:
        inherited = {}
        if spec.environment_mode is EnvironmentMode.INHERIT_ALLOWLIST:
            inherited = {
                name: os.environ[name]
                for name in spec.inherited_environment_names
                if name in os.environ
            }
        inherited.update(spec.environment)
        return inherited
