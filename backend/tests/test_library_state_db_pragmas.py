"""library_state.db runs in WAL mode with a busy timeout under thread contention."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.library_state_db import LibraryStateDb


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "library_state.db"
    monkeypatch.setattr(
        "app.services.library_state_db.settings.library_state_db_path", path
    )
    LibraryStateDb.initialize()
    return path


def test_pragmas(db_path: Path):
    with LibraryStateDb.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db_path.with_name(db_path.name + "-wal").exists()


def test_concurrent_writers_and_readers_never_hit_database_is_locked(db_path: Path):
    def writer(i: int) -> None:
        for j in range(25):
            LibraryStateDb.add_project_pin(f"project-{i}-{j}", f"series-{i}")
            LibraryStateDb.remove_project_pins(f"project-{i}-{j}")
            LibraryStateDb.add_project_pin(f"project-{i}-{j}", f"series-{i}")

    def reader(i: int) -> int:
        total = 0
        for _ in range(50):
            total += LibraryStateDb.count_project_pins(f"series-{i}")
        return total

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(writer, i) for i in range(8)]
        futures += [pool.submit(reader, i) for i in range(8)]
        for future in futures:
            future.result(timeout=60)  # raises sqlite3.OperationalError if locked

    with LibraryStateDb.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM project_series_pins").fetchone()[0]
    assert count == 8 * 25
