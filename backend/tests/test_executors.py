"""Heavy / light thread-pool split."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import executors


@pytest.fixture(autouse=True)
def _fresh_pools(monkeypatch):
    for name in (
        executors.HEAVY_WORKERS_ENV,
        executors.LEGACY_HEAVY_WORKERS_ENV,
        executors.IO_WORKERS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    executors.shutdown_executors()
    yield
    executors.shutdown_executors()


def test_defaults_and_clamps(monkeypatch):
    assert executors.heavy_worker_count() == executors.DEFAULT_HEAVY_WORKERS
    assert executors.io_worker_count() == executors.DEFAULT_IO_WORKERS

    monkeypatch.setenv(executors.HEAVY_WORKERS_ENV, "0")
    assert executors.heavy_worker_count() == 1
    monkeypatch.setenv(executors.HEAVY_WORKERS_ENV, "999")
    assert executors.heavy_worker_count() == 32
    monkeypatch.setenv(executors.HEAVY_WORKERS_ENV, "garbage")
    assert executors.heavy_worker_count() == executors.DEFAULT_HEAVY_WORKERS

    monkeypatch.setenv(executors.IO_WORKERS_ENV, "1")
    assert executors.io_worker_count() == 4
    monkeypatch.setenv(executors.IO_WORKERS_ENV, "1000")
    assert executors.io_worker_count() == 128


def test_legacy_variable_sizes_the_heavy_pool(monkeypatch):
    monkeypatch.setenv(executors.LEGACY_HEAVY_WORKERS_ENV, "3")
    assert executors.heavy_worker_count() == 3
    # The explicit variable wins over the alias.
    monkeypatch.setenv(executors.HEAVY_WORKERS_ENV, "5")
    assert executors.heavy_worker_count() == 5


def test_pools_are_lazy_and_distinct():
    assert executors.executor_stats()["heavy_threads"] == 0
    heavy = executors.heavy_executor()
    light = executors.light_executor()
    assert heavy is not light
    assert executors.heavy_executor() is heavy


@pytest.mark.asyncio
async def test_run_heavy_uses_heavy_threads_and_to_thread_uses_light_pool():
    executors.install_default_executor(asyncio.get_running_loop())

    heavy_thread = await executors.run_heavy(lambda: threading.current_thread().name)
    assert heavy_thread.startswith(executors.HEAVY_THREAD_PREFIX)

    light_thread = await asyncio.to_thread(lambda: threading.current_thread().name)
    assert light_thread.startswith(executors.IO_THREAD_PREFIX)

    def add(a, b=0):
        return a + b

    assert await executors.run_heavy(add, 2, b=3) == 5
    future = executors.submit_heavy(add, 4, 5)
    assert await asyncio.shield(future) == 9


@pytest.mark.asyncio
async def test_heavy_burst_does_not_block_light_io(monkeypatch):
    monkeypatch.setenv(executors.HEAVY_WORKERS_ENV, "2")
    executors.install_default_executor(asyncio.get_running_loop())

    release = threading.Event()
    started = threading.Barrier(3)

    def hog():
        started.wait(timeout=5)
        release.wait(timeout=5)

    hogs = [asyncio.ensure_future(executors.run_heavy(hog)) for _ in range(2)]
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    # Both heavy workers are busy; a light task must still run promptly.
    result = await asyncio.wait_for(asyncio.to_thread(lambda: "io ok"), timeout=1.0)
    assert result == "io ok"
    release.set()
    await asyncio.gather(*hogs)


def test_shutdown_then_lazy_recreate():
    first = executors.heavy_executor()
    executors.shutdown_executors()
    second = executors.heavy_executor()
    assert first is not second
