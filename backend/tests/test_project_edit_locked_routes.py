"""`project_edit_locked` on FastAPI handlers: lock semantics + DI transparency."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.project_locks import ProjectLocks, project_edit_locked

# The read-modify-write handlers that were race-free only because they never
# yielded between load and save; moving their I/O off the loop requires the lock.
LOCKED_HANDLERS = {
    "update_match",
    "update_matches_batch",
    "update_gap_timing",
    "validate_raw_scenes",
    "confirm_raw_scenes",
    "update_transcription",
    "split_scene",
    "merge_scenes",
}


@pytest.fixture(autouse=True)
def _reset_locks():
    ProjectLocks.reset()
    yield
    ProjectLocks.reset()


class Body(BaseModel):
    value: int


def _app() -> FastAPI:
    router = APIRouter(prefix="/projects/{project_id}")
    seen: list[bool] = []

    @router.post("/items/{item_id}")
    @project_edit_locked
    async def edit_item(project_id: str, item_id: int, body: Body):
        seen.append(ProjectLocks.is_held(project_id))
        await asyncio.sleep(0)
        return {"project_id": project_id, "item_id": item_id, "value": body.value}

    app = FastAPI()
    app.include_router(router)
    app.state.seen = seen
    return app


def test_dependency_injection_and_openapi_signature_survive_the_decorator():
    app = _app()
    client = TestClient(app)
    response = client.post("/projects/p1/items/7", json={"value": 3})
    assert response.status_code == 200, response.text
    assert response.json() == {"project_id": "p1", "item_id": 7, "value": 3}
    assert app.state.seen == [True]
    assert not ProjectLocks.is_held("p1")

    params = app.openapi()["paths"]["/projects/{project_id}/items/{item_id}"]["post"]["parameters"]
    assert {p["name"] for p in params} == {"project_id", "item_id"}


@pytest.mark.asyncio
async def test_concurrent_calls_to_the_same_project_are_serialised():
    order: list[str] = []

    @project_edit_locked
    async def handler(project_id: str, tag: str):
        order.append(f"{tag}:start")
        await asyncio.sleep(0.01)
        order.append(f"{tag}:end")

    await asyncio.gather(handler("p1", tag="a"), handler("p1", tag="b"))
    assert order == ["a:start", "a:end", "b:start", "b:end"]

    order.clear()
    await asyncio.gather(handler("p1", tag="a"), handler("p2", tag="b"))
    assert order[:2] == ["a:start", "b:start"]  # different projects interleave


@pytest.mark.asyncio
async def test_handler_without_project_id_is_rejected():
    @project_edit_locked
    async def handler(other: str):
        return other

    with pytest.raises(TypeError, match="project_id"):
        await handler("x")


def test_the_eight_read_modify_write_routes_are_locked():
    from app.api.routes import gaps, matching, raw_scenes, scenes, transcription

    modules = {
        "update_match": matching,
        "update_matches_batch": matching,
        "update_gap_timing": gaps,
        "validate_raw_scenes": raw_scenes,
        "confirm_raw_scenes": raw_scenes,
        "update_transcription": transcription,
        "split_scene": scenes,
        "merge_scenes": scenes,
    }
    assert set(modules) == LOCKED_HANDLERS
    for name, module in modules.items():
        handler = getattr(module, name)
        assert getattr(handler, "__project_edit_locked__", False), name
        assert "project_id" in handler.__wrapped__.__code__.co_varnames, name
