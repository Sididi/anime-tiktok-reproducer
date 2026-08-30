"""Run heavy media subprocesses at minimal desktop impact.

Indexing a series decodes, transcodes, and copies tens of GB through ffmpeg
and the anime_searcher subprocess at default priority; on a single-NVMe
desktop this evicts the page cache and starves interactive tasks. Heavy
subprocesses are wrapped in a transient systemd user scope:

- ``CPUWeight=idle`` marks the whole subtree idle-class (cgroup ``cpu.idle``):
  it may use every idle core — no throughput loss on an unloaded machine —
  but yields immediately when any interactive task wants CPU. Plain ``nice``
  cannot do this here because cgroup/autogroup weighting overrides it.
- ``MemoryHigh`` bounds the scope's page cache + anon footprint, so streaming
  a whole series reclaims the indexer's own cache instead of the desktop's
  working set.
- ``IOWeight`` participates only once the io cgroup controller is enabled
  (see scripts/setup_low_impact_host.sh); it is inert otherwise.

systemd-run in ``--scope`` mode execs the payload in place after registering
the scope, so PIDs, process groups, and kill/timeout handling are unchanged.
When systemd-run is unavailable (no user session bus), commands run unwrapped.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Sequence

from ..config import settings

_PROBE_TIMEOUT_SECONDS = 10.0
_COPY_CHUNK_BYTES = 8 * 1024 * 1024


@functools.lru_cache(maxsize=1)
def _systemd_scope_available() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    try:
        probe = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "-p",
                "CPUWeight=idle",
                "/bin/true",
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def wrap_low_impact(cmd: Sequence[str]) -> list[str]:
    """Prefix ``cmd`` so it runs in an idle-priority, cache-bounded scope."""
    resolved = list(cmd)
    if not settings.low_impact_media_jobs or not _systemd_scope_available():
        return resolved
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=atr-lowimpact-{uuid.uuid4().hex[:8]}",
        "-p",
        f"CPUWeight={settings.low_impact_cpu_weight}",
        "-p",
        f"MemoryHigh={settings.low_impact_memory_high}",
        "-p",
        f"IOWeight={settings.low_impact_io_weight}",
        *resolved,
    ]


def drop_file_page_cache(path: Path) -> None:
    """Best-effort drop of a file's clean pages from the page cache."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass
    finally:
        os.close(fd)


def copy_file_drop_cache(src: Path, dst: Path) -> None:
    """``shutil.copy2`` equivalent that leaves no page-cache footprint.

    The destination is fdatasync'd before the fadvise so its pages are clean
    and droppable; this also keeps a multi-GB copy from queueing a dirty
    writeback burst that stalls unrelated fsyncs.
    """
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        try:
            os.posix_fadvise(fsrc.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except OSError:
            pass
        shutil.copyfileobj(fsrc, fdst, _COPY_CHUNK_BYTES)
        fdst.flush()
        os.fdatasync(fdst.fileno())
        for handle in (fsrc, fdst):
            try:
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
    shutil.copystat(src, dst)
