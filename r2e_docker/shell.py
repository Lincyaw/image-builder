"""Standalone subprocess helper — zero external dependencies."""

from __future__ import annotations

import subprocess
from pathlib import Path
from subprocess import CompletedProcess


def run_subprocess_shell(
    command: str,
    cwd: str | Path,
    capture_output: bool = True,
    timeout: int = 120,
    **kwargs,
) -> CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            executable="/bin/bash",
            shell=True,
            capture_output=capture_output,
            text=True,
            cwd=cwd,
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        result = CompletedProcess(
            args=command,
            returncode=1,
            stderr="Timeout expired",
            stdout="Timeout",
        )
    except subprocess.CalledProcessError as e:
        result = CompletedProcess(
            args=command,
            returncode=1,
            stdout=getattr(e, "stdout", "") or "",
            stderr=getattr(e, "stderr", str(e)) or str(e),
        )
    except Exception as e:
        result = CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr=str(e),
        )
    return result
