"""Shared byte-progress contract for Storage Box transfers.

Both directions now report through rclone's per-second JSON stats
(:mod:`storage_box_rclone`), mapped onto :class:`ProgressSnapshot`. This
module only carries the contract shared by the transfer layer and its
subscribers (SSE routes, indexation jobs, operation rows).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

ProgressCallback = Callable[["ProgressSnapshot"], Awaitable[None] | None]


@dataclass
class ProgressSnapshot:
    bytes_transferred: int
    bytes_total: int
    mib_per_sec: float | None
    eta_seconds: float | None
    active_transfers: int
