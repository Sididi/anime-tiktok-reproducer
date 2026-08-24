"""Tests for app.services.cep_launch_store (Premiere Link durable queue)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.cep_launch import LAUNCH_TTL, CepLaunch
from app.services.cep_launch_store import CepLaunchStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


async def _upsert(store: CepLaunchStore, project_id: str = "p1", **kw) -> CepLaunch:
    params = dict(
        project_id=project_id,
        anime_title="Title",
        requested_at=NOW,
        discord_message_id="m1",
        discord_content="**Title**: done\nLien: <http://localhost:48653/p/p1>",
        now=NOW,
    )
    params.update(kw)
    return await store.upsert(**params)


async def test_upsert_replaces_pending_with_new_launch_id(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    first = await _upsert(store)
    second = await _upsert(store, discord_message_id="m2")
    assert first.launch_id != second.launch_id
    assert first.launch_id.startswith("l_")
    assert second.expires_at == NOW + LAUNCH_TTL
    all_launches = await store.list_all()
    assert [launch.launch_id for launch in all_launches] == [second.launch_id]
    assert all_launches[0].discord_message_id == "m2"
    assert all_launches[0].status == "pending"


async def test_upsert_after_ack_creates_fresh_pending(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    first = await _upsert(store)
    await store.record_ack(
        "p1", first.launch_id, result="accepted", detail=None, panel_build_id="b1", now=NOW
    )
    second = await _upsert(store)
    assert second.status == "pending"
    assert second.acked_at is None
    assert [launch.launch_id for launch in await store.list_pending(NOW)] == [second.launch_id]


async def test_record_ack_updates_current_and_ignores_stale(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    old = await _upsert(store)
    current = await _upsert(store)
    assert (
        await store.record_ack(
            "p1", old.launch_id, result="accepted", detail=None, panel_build_id="b1"
        )
        is None
    )
    assert (await store.get("p1")).status == "pending"
    updated = await store.record_ack(
        "p1",
        current.launch_id,
        result="duplicate",
        detail="already tracked",
        panel_build_id="b1",
        now=NOW + timedelta(minutes=1),
    )
    assert updated is not None
    assert updated.status == "duplicate"
    assert updated.ack_detail == "already tracked"
    assert updated.panel_build_id == "b1"
    assert updated.acked_at == NOW + timedelta(minutes=1)
    assert (
        await store.record_ack(
            "missing", "l_x", result="accepted", detail=None, panel_build_id=None
        )
        is None
    )


async def test_record_ack_rejects_unknown_result(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    launch = await _upsert(store)
    with pytest.raises(ValueError):
        await store.record_ack(
            "p1", launch.launch_id, result="maybe", detail=None, panel_build_id=None
        )


async def test_list_pending_excludes_expired_and_acked(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    await _upsert(store, project_id="fresh")
    stale = await _upsert(
        store, project_id="stale", requested_at=NOW - LAUNCH_TTL - timedelta(hours=1)
    )
    acked = await _upsert(store, project_id="acked")
    await store.record_ack(
        "acked", acked.launch_id, result="accepted", detail=None, panel_build_id=None
    )
    pending = await store.list_pending(NOW)
    assert [launch.project_id for launch in pending] == ["fresh"]
    assert stale.is_expired(NOW)


async def test_expire_stale_flips_only_pending_expired(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    await _upsert(store, project_id="fresh")
    await _upsert(store, project_id="stale", requested_at=NOW - LAUNCH_TTL - timedelta(hours=1))
    acked_old = await _upsert(
        store, project_id="acked_old", requested_at=NOW - LAUNCH_TTL - timedelta(hours=1)
    )
    await store.record_ack(
        "acked_old", acked_old.launch_id, result="error", detail="boom", panel_build_id=None
    )

    changed = await store.expire_stale(NOW)
    assert [launch.project_id for launch in changed] == ["stale"]
    assert (await store.get("stale")).status == "expired"
    assert (await store.get("fresh")).status == "pending"
    assert (await store.get("acked_old")).status == "error"
    assert await store.expire_stale(NOW) == []


async def test_mark_delivered_counts_and_ignores_stale_id(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    launch = await _upsert(store)
    await store.mark_delivered("p1", launch.launch_id, now=NOW)
    await store.mark_delivered("p1", launch.launch_id, now=NOW + timedelta(seconds=5))
    await store.mark_delivered("p1", "l_stale", now=NOW)
    stored = await store.get("p1")
    assert stored.delivery_count == 2
    assert stored.delivered_at == NOW + timedelta(seconds=5)
    assert stored.status == "pending"


async def test_delete_is_idempotent(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    await _upsert(store)
    assert (await store.delete("p1")).project_id == "p1"
    assert await store.delete("p1") is None
    assert await store.get("p1") is None


async def test_disk_roundtrip_and_no_temp_files(tmp_path: Path):
    path = tmp_path / "cep_launches.json"
    store = CepLaunchStore(path)
    launch = await _upsert(store)
    raw = json.loads(path.read_text())
    assert set(raw) == {"launches"}
    assert raw["launches"]["p1"]["launch_id"] == launch.launch_id
    reloaded = CepLaunchStore(path)
    assert await reloaded.get("p1") == launch
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".cep_launches.")] == []


async def test_concurrent_upserts_keep_one_entry_per_project(tmp_path: Path):
    store = CepLaunchStore(tmp_path / "cep_launches.json")
    await asyncio.gather(*[_upsert(store, project_id=f"p{i % 3}") for i in range(12)])
    assert sorted(launch.project_id for launch in await store.list_all()) == ["p0", "p1", "p2"]
