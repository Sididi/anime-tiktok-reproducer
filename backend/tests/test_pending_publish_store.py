from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.pending_publish_store import (
    PendingArtifact,
    PendingPublishRecord,
    PendingPublishStore,
)


def _make_record(
    tmp_path: Path,
    *,
    publish_id: str = "pub-1",
    series_id: str = "series-1",
    display_name: str = "Sakamoto Days",
    library_type: str = "anime",
) -> PendingPublishRecord:
    staged_dir = PendingPublishStore.staging_dir_root() / publish_id
    staged_dir.mkdir(parents=True, exist_ok=True)
    (staged_dir / "index-file.bin").write_bytes(b"shard")
    series_dir = tmp_path / "library" / library_type / display_name
    series_dir.mkdir(parents=True, exist_ok=True)
    return PendingPublishRecord(
        publish_id=publish_id,
        library_type=library_type,
        series_id=series_id,
        release_id=f"release-{publish_id}",
        display_name=display_name,
        series_dir=str(series_dir),
        staged_index_dir=str(staged_dir),
        is_brand_new_series=True,
        uploads=[
            PendingArtifact(
                remote_relative_path="payload/library/ep1.mkv",
                size_bytes=5,
                sha256="abc",
                artifact_type="library",
                local_path=str(series_dir / "ep1.mkv"),
                local_relative_path=f"{display_name}/ep1.mkv",
            )
        ],
        manifest={
            "series_id": series_id,
            "release_id": f"release-{publish_id}",
            "display_name": display_name,
            "episode_count": 1,
            "episodes": [],
        },
    )


@pytest.fixture
def store_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    return tmp_path


def test_save_load_roundtrip(store_env: Path) -> None:
    record = _make_record(store_env)
    PendingPublishStore.save(record)

    loaded = PendingPublishStore.load("pub-1")
    assert loaded is not None
    assert loaded == record
    # Atomic write leaves no temp file behind.
    leftovers = list(PendingPublishStore.records_root().glob("*.tmp"))
    assert leftovers == []


def test_delete_removes_record_and_staged_dir(store_env: Path) -> None:
    record = _make_record(store_env)
    PendingPublishStore.save(record)
    staged_dir = Path(record.staged_index_dir)
    assert staged_dir.exists()

    PendingPublishStore.delete("pub-1")

    assert PendingPublishStore.load("pub-1") is None
    assert not staged_dir.exists()
    # Deleting again is a no-op.
    PendingPublishStore.delete("pub-1")


def test_find_by_series_and_display_name(store_env: Path) -> None:
    record_a = _make_record(store_env, publish_id="pub-a", series_id="series-a")
    record_b = _make_record(
        store_env,
        publish_id="pub-b",
        series_id="series-b",
        display_name="Frieren",
    )
    PendingPublishStore.save(record_a)
    PendingPublishStore.save(record_b)

    assert len(PendingPublishStore.list_all()) == 2
    found = PendingPublishStore.find_by_series("anime", "series-b")
    assert found is not None and found.publish_id == "pub-b"
    assert PendingPublishStore.find_by_series("ln", "series-b") is None
    found = PendingPublishStore.find_by_display_name("anime", "Frieren")
    assert found is not None and found.publish_id == "pub-b"
    assert PendingPublishStore.find_by_display_name("anime", "Unknown") is None


def test_unreadable_record_is_skipped(store_env: Path) -> None:
    record = _make_record(store_env)
    PendingPublishStore.save(record)
    (PendingPublishStore.records_root() / "broken.json").write_text(
        "{not json", encoding="utf-8"
    )

    records = PendingPublishStore.list_all()
    assert [r.publish_id for r in records] == ["pub-1"]
