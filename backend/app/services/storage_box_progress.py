"""Polling-based aggregate transfer progress for Storage Box operations.

Tracks active uploads/downloads by stat'ing destination file sizes
periodically and emitting `ProgressSnapshot`s to a subscriber callback.
Backend-agnostic: works for sftp, rsync, and lftp transfers alike.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

from .storage_box_sftp_client import StorageBoxSftpClient


logger = logging.getLogger("uvicorn.error")


Direction = Literal["upload", "download"]
ProgressCallback = Callable[["ProgressSnapshot"], Awaitable[None] | None]


@dataclass
class ProgressSnapshot:
    bytes_transferred: int
    bytes_total: int
    mib_per_sec: float | None
    eta_seconds: float | None
    active_transfers: int


@dataclass
class _ActiveTransfer:
    local_path: Path
    remote_path: PurePosixPath
    target_size: int
    initial_size: int


class TransferSession:
    """A scoped tracker for one logical operation (e.g. one publish_series call).

    Use `track(...)` as a context manager around each individual file transfer.
    Call `close()` when the operation ends to stop the poller.
    """

    _SAMPLE_WINDOW = 5  # rolling window for speed computation

    def __init__(
        self,
        session_id: str,
        *,
        direction: Direction,
        total_bytes: int,
        on_update: ProgressCallback,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.session_id = session_id
        self.direction: Direction = direction
        self.total_bytes = max(0, int(total_bytes))
        self._on_update = on_update
        self._poll_interval = max(0.05, float(poll_interval_seconds))
        self._active: list[_ActiveTransfer] = []
        self._completed_bytes: int = 0
        self._lock = asyncio.Lock()
        self._samples: deque[tuple[float, int]] = deque(maxlen=self._SAMPLE_WINDOW)
        self._poller_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_snapshot_at: float = 0.0

    @asynccontextmanager
    async def track(
        self,
        *,
        local_path: Path,
        remote_path: PurePosixPath,
        target_size: int,
    ) -> AsyncIterator[None]:
        """Register a single file transfer; deregister on exit."""
        if self._closed:
            yield
            return

        initial_size = await self._stat_size(local_path, remote_path)
        transfer = _ActiveTransfer(
            local_path=local_path,
            remote_path=remote_path,
            target_size=max(0, int(target_size)),
            initial_size=max(0, initial_size),
        )

        async with self._lock:
            self._active.append(transfer)
            self._ensure_poller_running()

        try:
            yield
        finally:
            async with self._lock:
                if transfer in self._active:
                    self._active.remove(transfer)
                self._completed_bytes += max(
                    0, transfer.target_size - transfer.initial_size
                )
            # One immediate snapshot after a transfer ends so the UI updates
            # promptly even between polls.
            await self._emit_snapshot()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._poller_task
        self._poller_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Emit a final snapshot so the subscriber sees the terminal state.
        try:
            await self._emit_snapshot()
        except Exception:
            logger.debug(
                "TransferSession %s final snapshot raised", self.session_id, exc_info=True
            )

    def _ensure_poller_running(self) -> None:
        if self._closed:
            return
        if self._poller_task is None or self._poller_task.done():
            self._poller_task = asyncio.create_task(
                self._run_poller(), name=f"storage-box-progress:{self.session_id}"
            )

    async def _run_poller(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._poll_interval)
                await self._emit_snapshot()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "TransferSession %s poller crashed", self.session_id
            )

    async def _emit_snapshot(self) -> None:
        snapshot = await self._build_snapshot()
        try:
            result = self._on_update(snapshot)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "TransferSession %s on_update raised; continuing",
                self.session_id,
                exc_info=True,
            )

    async def _build_snapshot(self) -> ProgressSnapshot:
        async with self._lock:
            active_copy = list(self._active)
            completed_bytes = self._completed_bytes
            active_count = len(active_copy)

        active_delta = 0
        for transfer in active_copy:
            current = await self._stat_size(transfer.local_path, transfer.remote_path)
            if current > transfer.initial_size:
                active_delta += min(
                    current - transfer.initial_size,
                    transfer.target_size - transfer.initial_size
                    if transfer.target_size > 0
                    else current - transfer.initial_size,
                )

        bytes_transferred = completed_bytes + active_delta
        if self.total_bytes > 0:
            bytes_transferred = min(bytes_transferred, self.total_bytes)

        now = time.monotonic()
        self._samples.append((now, bytes_transferred))

        mib_per_sec: float | None = None
        eta_seconds: float | None = None
        if len(self._samples) >= 2:
            t_old, b_old = self._samples[0]
            t_new, b_new = self._samples[-1]
            dt = t_new - t_old
            db = b_new - b_old
            if dt > 0 and db >= 0:
                bytes_per_sec = db / dt
                mib_per_sec = round(bytes_per_sec / (1024 * 1024), 2)
                if bytes_per_sec > 0 and self.total_bytes > 0:
                    remaining = max(0, self.total_bytes - bytes_transferred)
                    eta_seconds = round(remaining / bytes_per_sec, 1)

        return ProgressSnapshot(
            bytes_transferred=bytes_transferred,
            bytes_total=self.total_bytes,
            mib_per_sec=mib_per_sec,
            eta_seconds=eta_seconds,
            active_transfers=active_count,
        )

    async def _stat_size(self, local_path: Path, remote_path: PurePosixPath) -> int:
        if self.direction == "download":
            try:
                return local_path.stat().st_size
            except OSError:
                return 0
        try:
            stat_result = await StorageBoxSftpClient.stat(remote_path)
        except Exception:
            return 0
        return int(getattr(stat_result, "size", 0) or 0)


class StorageBoxTransferProgress:
    """Factory + registry for active progress sessions."""

    _sessions: dict[str, TransferSession] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def open_session(
        cls,
        session_id: str,
        *,
        direction: Direction,
        total_bytes: int,
        on_update: ProgressCallback,
        poll_interval_seconds: float = 0.5,
    ) -> TransferSession:
        session = TransferSession(
            session_id,
            direction=direction,
            total_bytes=total_bytes,
            on_update=on_update,
            poll_interval_seconds=poll_interval_seconds,
        )
        async with cls._lock:
            cls._sessions[session_id] = session
        return session

    @classmethod
    async def close_session(cls, session: TransferSession) -> None:
        await session.close()
        async with cls._lock:
            cls._sessions.pop(session.session_id, None)


async def noop_progress_callback(_snapshot: ProgressSnapshot) -> None:
    """Default callback used when callers don't subscribe."""
    return None
