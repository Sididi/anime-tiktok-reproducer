"""Helpers to tear down worker processes spawned by ML/runtime libraries."""

from __future__ import annotations

from contextlib import suppress


def shutdown_torch_compile_workers() -> None:
    """Shut down TorchInductor compile worker pools when available."""
    with suppress(Exception):
        from torch._inductor.async_compile import shutdown_compile_workers

        shutdown_compile_workers()
