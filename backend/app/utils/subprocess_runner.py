"""Async subprocess helpers with timeout and cancellation-safe cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass
class CommandResult:
    """Result of a subprocess execution."""

    returncode: int
    stdout: bytes
    stderr: bytes


class CommandTimeoutError(RuntimeError):
    """Raised when a subprocess exceeds the configured timeout."""


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    terminate_grace_seconds: float = 3.0,
) -> None:
    """Terminate a subprocess and escalate to kill if needed."""
    if process.returncode is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=terminate_grace_seconds)
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass

    try:
        process.kill()
    except ProcessLookupError:
        return

    try:
        await process.wait()
    except ProcessLookupError:
        pass


async def run_command(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Run a command and capture both stdout/stderr safely."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )

    try:
        communicate_task = process.communicate()
        if timeout_seconds is None:
            stdout, stderr = await communicate_task
        else:
            stdout, stderr = await asyncio.wait_for(communicate_task, timeout=timeout_seconds)
        return CommandResult(returncode=process.returncode or 0, stdout=stdout, stderr=stderr)
    except asyncio.TimeoutError as exc:
        await terminate_process(process)
        raise CommandTimeoutError(
            f"Command timed out after {timeout_seconds:.1f}s: {' '.join(cmd)}"
        ) from exc
    except asyncio.CancelledError:
        await terminate_process(process)
        raise
