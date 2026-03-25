from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..library_types import LibraryType, coerce_library_type


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SeriesStateRow:
    library_type: str
    series_id: str
    release_id: str | None
    permanent_pin: bool
    hydration_status: str
    local_episode_count: int
    expected_episode_count: int
    last_error: str | None
    updated_at: str


@dataclass(frozen=True)
class OperationRow:
    library_type: str
    series_id: str
    operation_type: str
    status: str
    progress: float
    error: str | None
    updated_at: str


class LibraryStateDb:
    """Small persistent local state store for hydration and publish state."""

    @classmethod
    def path(cls) -> Path:
        return settings.library_state_db_path

    @classmethod
    def connect(cls) -> sqlite3.Connection:
        conn = sqlite3.connect(cls.path())
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @classmethod
    def initialize(cls) -> None:
        cls.path().parent.mkdir(parents=True, exist_ok=True)
        with cls.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS series_state (
                    library_type TEXT NOT NULL,
                    series_id TEXT NOT NULL,
                    release_id TEXT,
                    permanent_pin INTEGER NOT NULL DEFAULT 0,
                    hydration_status TEXT NOT NULL,
                    local_episode_count INTEGER NOT NULL DEFAULT 0,
                    expected_episode_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (library_type, series_id)
                );

                CREATE TABLE IF NOT EXISTS project_series_pins (
                    project_id TEXT NOT NULL,
                    series_id TEXT NOT NULL,
                    PRIMARY KEY (project_id, series_id)
                );

                CREATE TABLE IF NOT EXISTS operations (
                    library_type TEXT NOT NULL,
                    series_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (library_type, series_id, operation_type)
                );
                """
            )

    @classmethod
    def mark_incomplete_operations_interrupted(cls) -> None:
        now = _utc_now_iso()
        with cls.connect() as conn:
            conn.execute(
                """
                UPDATE operations
                SET status = 'interrupted', updated_at = ?
                WHERE status IN ('pending', 'running')
                """,
                (now,),
            )

    @classmethod
    def upsert_series_state(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str | None,
        permanent_pin: bool | None = None,
        hydration_status: str,
        local_episode_count: int,
        expected_episode_count: int,
        last_error: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        scoped_type = coerce_library_type(library_type).value
        with cls.connect() as conn:
            existing = conn.execute(
                """
                SELECT permanent_pin
                FROM series_state
                WHERE library_type = ? AND series_id = ?
                """,
                (scoped_type, series_id),
            ).fetchone()
            existing_pin = bool(existing["permanent_pin"]) if existing is not None else False
            pin_value = int(permanent_pin if permanent_pin is not None else existing_pin)
            conn.execute(
                """
                INSERT INTO series_state (
                    library_type, series_id, release_id, permanent_pin,
                    hydration_status, local_episode_count, expected_episode_count,
                    last_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_type, series_id) DO UPDATE SET
                    release_id = excluded.release_id,
                    permanent_pin = excluded.permanent_pin,
                    hydration_status = excluded.hydration_status,
                    local_episode_count = excluded.local_episode_count,
                    expected_episode_count = excluded.expected_episode_count,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    scoped_type,
                    series_id,
                    release_id,
                    pin_value,
                    hydration_status,
                    max(0, local_episode_count),
                    max(0, expected_episode_count),
                    last_error,
                    now,
                ),
            )

    @classmethod
    def get_series_state(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> SeriesStateRow | None:
        scoped_type = coerce_library_type(library_type).value
        with cls.connect() as conn:
            row = conn.execute(
                """
                SELECT library_type, series_id, release_id, permanent_pin, hydration_status,
                       local_episode_count, expected_episode_count, last_error, updated_at
                FROM series_state
                WHERE library_type = ? AND series_id = ?
                """,
                (scoped_type, series_id),
            ).fetchone()
        if row is None:
            return None
        return SeriesStateRow(
            library_type=row["library_type"],
            series_id=row["series_id"],
            release_id=row["release_id"],
            permanent_pin=bool(row["permanent_pin"]),
            hydration_status=row["hydration_status"],
            local_episode_count=int(row["local_episode_count"]),
            expected_episode_count=int(row["expected_episode_count"]),
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    @classmethod
    def list_series_states(
        cls,
        library_type: LibraryType | str,
    ) -> dict[str, SeriesStateRow]:
        scoped_type = coerce_library_type(library_type).value
        with cls.connect() as conn:
            rows = conn.execute(
                """
                SELECT library_type, series_id, release_id, permanent_pin, hydration_status,
                       local_episode_count, expected_episode_count, last_error, updated_at
                FROM series_state
                WHERE library_type = ?
                """,
                (scoped_type,),
            ).fetchall()
        return {
            row["series_id"]: SeriesStateRow(
                library_type=row["library_type"],
                series_id=row["series_id"],
                release_id=row["release_id"],
                permanent_pin=bool(row["permanent_pin"]),
                hydration_status=row["hydration_status"],
                local_episode_count=int(row["local_episode_count"]),
                expected_episode_count=int(row["expected_episode_count"]),
                last_error=row["last_error"],
                updated_at=row["updated_at"],
            )
            for row in rows
        }

    @classmethod
    def set_permanent_pin(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        permanent_pin: bool,
    ) -> None:
        scoped_type = coerce_library_type(library_type).value
        now = _utc_now_iso()
        with cls.connect() as conn:
            updated = conn.execute(
                """
                UPDATE series_state
                SET permanent_pin = ?, updated_at = ?
                WHERE library_type = ? AND series_id = ?
                """,
                (int(permanent_pin), now, scoped_type, series_id),
            )
            if updated.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO series_state (
                        library_type, series_id, release_id, permanent_pin,
                        hydration_status, local_episode_count, expected_episode_count,
                        last_error, updated_at
                    )
                    VALUES (?, ?, NULL, ?, 'not_hydrated', 0, 0, NULL, ?)
                    """,
                    (scoped_type, series_id, int(permanent_pin), now),
                )

    @classmethod
    def add_project_pin(cls, project_id: str, series_id: str) -> None:
        with cls.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO project_series_pins (project_id, series_id)
                VALUES (?, ?)
                """,
                (project_id, series_id),
            )

    @classmethod
    def remove_project_pins(cls, project_id: str) -> None:
        with cls.connect() as conn:
            conn.execute(
                "DELETE FROM project_series_pins WHERE project_id = ?",
                (project_id,),
            )

    @classmethod
    def count_project_pins(cls, series_id: str) -> int:
        with cls.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM project_series_pins WHERE series_id = ?",
                (series_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    @classmethod
    def get_project_pin_counts(cls, series_ids: list[str]) -> dict[str, int]:
        if not series_ids:
            return {}
        placeholders = ", ".join("?" for _ in series_ids)
        with cls.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT series_id, COUNT(*) AS count
                FROM project_series_pins
                WHERE series_id IN ({placeholders})
                GROUP BY series_id
                """,
                tuple(series_ids),
            ).fetchall()
        counts = {series_id: 0 for series_id in series_ids}
        for row in rows:
            counts[row["series_id"]] = int(row["count"])
        return counts

    @classmethod
    def get_series_id_for_project(cls, project_id: str) -> str | None:
        with cls.connect() as conn:
            row = conn.execute(
                """
                SELECT series_id
                FROM project_series_pins
                WHERE project_id = ?
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["series_id"])

    @classmethod
    def upsert_operation(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        operation_type: str,
        status: str,
        progress: float = 0.0,
        error: str | None = None,
    ) -> None:
        scoped_type = coerce_library_type(library_type).value
        now = _utc_now_iso()
        with cls.connect() as conn:
            conn.execute(
                """
                INSERT INTO operations (
                    library_type, series_id, operation_type, status, progress, error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_type, series_id, operation_type) DO UPDATE SET
                    status = excluded.status,
                    progress = excluded.progress,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    scoped_type,
                    series_id,
                    operation_type,
                    status,
                    max(0.0, min(1.0, float(progress))),
                    error,
                    now,
                ),
            )

    @classmethod
    def get_operation(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        operation_type: str,
    ) -> OperationRow | None:
        scoped_type = coerce_library_type(library_type).value
        with cls.connect() as conn:
            row = conn.execute(
                """
                SELECT library_type, series_id, operation_type, status, progress, error, updated_at
                FROM operations
                WHERE library_type = ? AND series_id = ? AND operation_type = ?
                """,
                (scoped_type, series_id, operation_type),
            ).fetchone()
        if row is None:
            return None
        return OperationRow(
            library_type=row["library_type"],
            series_id=row["series_id"],
            operation_type=row["operation_type"],
            status=row["status"],
            progress=float(row["progress"]),
            error=row["error"],
            updated_at=row["updated_at"],
        )

    @classmethod
    def list_operations(
        cls,
        *,
        library_type: LibraryType | str | None = None,
        series_id: str | None = None,
    ) -> list[OperationRow]:
        where: list[str] = []
        params: list[str] = []
        if library_type is not None:
            where.append("library_type = ?")
            params.append(coerce_library_type(library_type).value)
        if series_id is not None:
            where.append("series_id = ?")
            params.append(series_id)

        query = """
            SELECT library_type, series_id, operation_type, status, progress, error, updated_at
            FROM operations
        """
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY updated_at DESC"

        with cls.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            OperationRow(
                library_type=row["library_type"],
                series_id=row["series_id"],
                operation_type=row["operation_type"],
                status=row["status"],
                progress=float(row["progress"]),
                error=row["error"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    @classmethod
    def delete_operation(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        operation_type: str,
    ) -> None:
        scoped_type = coerce_library_type(library_type).value
        with cls.connect() as conn:
            conn.execute(
                """
                DELETE FROM operations
                WHERE library_type = ? AND series_id = ? AND operation_type = ?
                """,
                (scoped_type, series_id, operation_type),
            )


LibraryStateDb.initialize()
