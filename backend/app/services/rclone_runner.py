"""Shared rclone subprocess runner.

One rclone process per batch transfer, ``--use-json-log --stats 1s`` on
stderr parsed line-by-line into :class:`RcloneStats`, error lines collected
and surfaced on non-zero exit. Used by the Storage Box (SFTP) upload and
download batches and the Google Drive export sync; each service maps
:class:`RcloneStats` onto its own progress contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shutil
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..utils.subprocess_runner import terminate_process

logger = logging.getLogger("uvicorn.error")

# stderr lines are JSON logs; stats lines stay small but keep headroom for
# long "transferring" arrays.
_STDERR_LINE_LIMIT = 1024 * 1024
_MAX_ERROR_LINES = 5


class RcloneError(RuntimeError):
    """Raised when an rclone subprocess fails or rclone is unavailable."""


class RcloneRestartRequested(RcloneError):
    """Raised by :func:`run_rclone` when ``should_restart`` asked to abandon the run."""

    def __init__(self, reason: str, last_stats: "RcloneStats | None") -> None:
        super().__init__(reason)
        self.reason = reason
        self.last_stats = last_stats


@dataclass
class RcloneFileProgress:
    """One entry of rclone's ``transferring`` array: a file in flight."""

    name: str  # path relative to the transfer root
    bytes_done: int
    size: int  # -1 while rclone does not know it
    speed_bytes_per_sec: float


@dataclass
class RcloneStats:
    bytes_transferred: int
    bytes_total: int  # rclone's totalBytes; grows while it is still scanning
    speed_bytes_per_sec: float
    eta_seconds: float | None
    transferring_names: list[str]
    transfers: int  # completed transfers so far
    total_transfers: int
    checks: int  # files compared and skipped (delta sync)
    total_checks: int
    transferring: list[RcloneFileProgress] = field(default_factory=list)


StatsCallback = Callable[[RcloneStats], Awaitable[None] | None]
# Consulted on every stats frame; a non-empty string is the reason to abandon
# the current process (the caller decides whether to run again).
RestartPredicate = Callable[[RcloneStats], str | None]


def find_binary() -> str | None:
    return shutil.which("rclone")


async def run_rclone(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    stats_callback: StatsCallback | None = None,
    error_prefix: str = "rclone",
    should_restart: RestartPredicate | None = None,
) -> RcloneStats | None:
    """Run rclone, streaming stats to ``stats_callback``.

    Returns the last stats seen (None if rclone emitted none). Raises
    :class:`RcloneError` on non-zero exit, with the last error lines, and
    :class:`RcloneRestartRequested` when ``should_restart`` returned a reason
    (the process is terminated first).
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=_STDERR_LINE_LIMIT,
    )
    error_lines: deque[str] = deque(maxlen=_MAX_ERROR_LINES)
    last_stats: RcloneStats | None = None
    try:
        assert process.stderr is not None
        while True:
            try:
                line = await process.stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not line:
                break
            stats = _parse_stderr_line(line, error_lines)
            if stats is None:
                continue
            last_stats = stats
            await _emit(stats_callback, stats)
            reason = should_restart(stats) if should_restart is not None else None
            if reason:
                await terminate_process(process)
                raise RcloneRestartRequested(reason, stats)
        returncode = await process.wait()
    except asyncio.CancelledError:
        await terminate_process(process)
        raise

    if returncode != 0:
        details = "; ".join(error_lines) or "no error output captured"
        raise RcloneError(f"{error_prefix} exited with code {returncode}: {details}")
    return last_stats


def _parse_file_progress(raw: object) -> list[RcloneFileProgress]:
    if not isinstance(raw, list):
        return []
    files: list[RcloneFileProgress] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            size = item.get("size")
            files.append(
                RcloneFileProgress(
                    name=str(item.get("name") or ""),
                    bytes_done=max(0, int(item.get("bytes") or 0)),
                    size=int(size) if size is not None else -1,
                    speed_bytes_per_sec=float(item.get("speed") or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue
    return files


def _parse_stderr_line(
    raw_line: bytes, error_lines: deque[str]
) -> RcloneStats | None:
    try:
        payload = json.loads(raw_line.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    if payload.get("level") == "error":
        message = str(payload.get("msg") or "").strip()
        if message:
            error_lines.append(message)

    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    try:
        transferring = _parse_file_progress(stats.get("transferring"))
        eta = stats.get("eta")
        return RcloneStats(
            bytes_transferred=max(0, int(stats.get("bytes") or 0)),
            bytes_total=max(0, int(stats.get("totalBytes") or 0)),
            speed_bytes_per_sec=float(stats.get("speed") or 0.0),
            eta_seconds=float(eta) if isinstance(eta, (int, float)) else None,
            transferring_names=[item.name for item in transferring],
            transfers=max(0, int(stats.get("transfers") or 0)),
            total_transfers=max(0, int(stats.get("totalTransfers") or 0)),
            checks=max(0, int(stats.get("checks") or 0)),
            total_checks=max(0, int(stats.get("totalChecks") or 0)),
            transferring=transferring,
        )
    except (TypeError, ValueError):
        return None


async def _emit(stats_callback: StatsCallback | None, stats: RcloneStats) -> None:
    if stats_callback is None:
        return
    try:
        result = stats_callback(stats)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning("rclone stats callback raised; continuing", exc_info=True)


async def obscure_password(binary: str, password: str) -> str:
    # Secret goes through stdin (never argv) and reaches rclone as an
    # obscured env value (e.g. RCLONE_SFTP_PASS).
    process = await asyncio.create_subprocess_exec(
        binary,
        "obscure",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(password.encode("utf-8"))
    if process.returncode != 0:
        raise RcloneError(
            f"rclone obscure failed: {stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode("utf-8").strip()
