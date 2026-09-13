"""Safe system SSH/SCP execution for the Windows remote operator."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Optional

from pipeline.operator_config import RemoteConfig


_SECRET_PATTERNS = (
    re.compile(r"(?i)(GEOAPIFY_API_KEY\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(DATABASE_URL\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(authorization\s*:\s*)(?:bearer|basic)\s+\S+"),
    re.compile(
        r"(?i)(\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*[=:]\s*)\S+"
    ),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb)://[^\s]+"),
    re.compile(r"(?i)([^\r\n]*@[^\r\n]*'s password:)"),
)


def redact_secrets(text: str) -> str:
    for index, pattern in enumerate(_SECRET_PATTERNS):
        replacement = (
            "[REDACTED_DATABASE_URL]"
            if index == 5
            else "[SSH_PASSWORD_PROMPT]"
            if index == 6
            else r"\1[REDACTED]"
        )
        text = pattern.sub(replacement, text)
    return text


def powershell_literal(value: str) -> str:
    if any(character in value for character in "\r\n\0"):
        raise ValueError("PowerShell argument contains a forbidden control character")
    return "'" + value.replace("'", "''") + "'"


def encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    attempts: int


class RemoteExecutionError(RuntimeError):
    """A bounded remote command or copy operation failed."""


_TRANSIENT_TRANSPORT_MARKERS = (
    "connection reset",
    "connection timed out",
    "connection closed",
    "connection aborted",
    "broken pipe",
    "network is unreachable",
    "temporary failure",
)
_DETERMINISTIC_FAILURE_MARKERS = (
    "geoapify_api_key",
    "missing geoapify",
    "model is missing",
    "model not found",
    "git commit mismatch",
    "not recognized as the name of a cmdlet",
    "invalid manifest",
    "row-count mismatch",
    "row count mismatch",
)


def is_transient_transport_failure(
    returncode: int, stdout: str, stderr: str
) -> bool:
    """Classify only genuine SSH/SCP transport interruptions as retryable."""

    if returncode not in {1, 255}:
        return False
    diagnostic = f"{stdout}\n{stderr}".casefold()
    if any(marker in diagnostic for marker in _DETERMINISTIC_FAILURE_MARKERS):
        return False
    return any(marker in diagnostic for marker in _TRANSIENT_TRANSPORT_MARKERS)


class RemoteExecutor:
    def __init__(
        self,
        remote: RemoteConfig,
        *,
        maximum_retries: int = 2,
        retry_delay_seconds: float = 1.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        sleeper: Callable[[float], None] = time.sleep,
        log_path: Optional[Path] = None,
        verbose: bool = False,
    ) -> None:
        self.remote = remote
        self.maximum_retries = maximum_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.runner = runner
        self.popen_factory = popen_factory
        self.sleeper = sleeper
        self.log_path = log_path
        self.verbose = verbose

    def _record(self, value: str) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(redact_secrets(value))

    def _run(
        self, arguments: list[str], *, transient_code: int, check: bool = True
    ) -> CommandResult:
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self.runner(
                    arguments,
                    shell=False,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                if attempts > self.maximum_retries:
                    raise RemoteExecutionError(str(exc)) from exc
                self.sleeper(self.retry_delay_seconds * attempts)
                continue
            stdout = redact_secrets(result.stdout or "")
            stderr = redact_secrets(result.stderr or "")
            self._record(stdout)
            self._record(stderr)
            if result.returncode == 0:
                return CommandResult(0, stdout, stderr, attempts)
            retryable = (
                result.returncode == transient_code
                and is_transient_transport_failure(result.returncode, stdout, stderr)
            )
            if not retryable or attempts > self.maximum_retries:
                if not check:
                    return CommandResult(
                        result.returncode, stdout, stderr, attempts
                    )
                raise RemoteExecutionError(
                    stderr.strip() or f"Remote command failed with exit code {result.returncode}"
                )
            self.sleeper(self.retry_delay_seconds * attempts)

    def run_powershell(self, script: str, *, check: bool = True) -> CommandResult:
        return self._run(
            self.powershell_arguments(script), transient_code=255, check=check
        )

    def powershell_arguments(self, script: str) -> list[str]:
        return [
            "ssh",
            self.remote.host,
            self.remote.powershell,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encode_powershell(script),
        ]

    def stream_powershell(self, script: str) -> CommandResult:
        """Stream sanitized remote progress to stderr while retaining a safe log."""

        arguments = self.powershell_arguments(script)
        attempts = 0
        while True:
            attempts += 1
            try:
                process = self.popen_factory(
                    arguments,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                if attempts > self.maximum_retries:
                    raise RemoteExecutionError(str(exc)) from exc
                self.sleeper(self.retry_delay_seconds * attempts)
                continue
            output: list[str] = []
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = redact_secrets(raw_line)
                output.append(line)
                self._record(line)
                print(line, end="", file=sys.stderr)
            returncode = process.wait()
            joined = "".join(output)
            if returncode == 0:
                return CommandResult(0, joined, "", attempts)
            retryable = is_transient_transport_failure(returncode, joined, "")
            if not retryable or attempts > self.maximum_retries:
                raise RemoteExecutionError(
                    joined.strip() or f"Remote command failed with exit code {returncode}"
                )
            self.sleeper(self.retry_delay_seconds * attempts)

    def copy_from(self, remote_path: str, local_path: Path) -> CommandResult:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        portable_path = remote_path.replace("\\", "/")
        arguments = ["scp", f"{self.remote.host}:{portable_path}", str(local_path)]
        return self._run(arguments, transient_code=1)

    def copy_to(self, local_path: Path, remote_path: str) -> CommandResult:
        """Copy one explicit local artifact to one explicit remote path."""

        if not local_path.is_file():
            raise RemoteExecutionError(f"Local upload artifact is missing: {local_path}")
        portable_path = remote_path.replace("\\", "/")
        arguments = ["scp", str(local_path), f"{self.remote.host}:{portable_path}"]
        return self._run(arguments, transient_code=1)
