"""Per-project asyncio locks for read-modify-write sections on project files.

Once project JSON I/O runs on worker threads, a route that loads a file,
mutates it and saves it back yields the event loop in between — two
concurrent edits of the same project could otherwise silently lose one
update.  Wrap such sections in ``async with ProjectLocks.hold(project_id):``.

Rules (see ``ProjectService`` for the file helpers):

* Only the outermost async unit — a route handler or a background job
  coroutine — takes the lock; service helpers never do.
* Hold it only across ``aload → mutate → asave``: no heavy compute, no
  network, no other long await inside.  Long jobs compute unlocked, then
  reload fresh state under the lock and merge.
* Never nest two project locks.  Re-acquiring the lock from the task that
  already holds it raises immediately (``RuntimeError``) instead of
  deadlocking.
* The lock is per process: a second backend process (another machine on the
  same Storage Box) is not coordinated — exactly as before.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar
from weakref import WeakValueDictionary

T = TypeVar("T")


class ProjectLocks:
    _locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
    _owners: dict[str, asyncio.Task[object]] = {}

    @classmethod
    def _lock_for(cls, project_id: str) -> asyncio.Lock:
        lock = cls._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._locks[project_id] = lock
        return lock

    @classmethod
    @asynccontextmanager
    async def hold(cls, project_id: str) -> AsyncIterator[None]:
        task = asyncio.current_task()
        owner = cls._owners.get(project_id)
        if task is not None and owner is task:
            raise RuntimeError(
                f"project lock {project_id!r} re-entered by the task that already "
                "holds it (a helper must not take the lock its caller holds)"
            )
        lock = cls._lock_for(project_id)
        async with lock:
            if task is not None:
                cls._owners[project_id] = task
            try:
                yield
            finally:
                if cls._owners.get(project_id) is task:
                    cls._owners.pop(project_id, None)

    @classmethod
    def is_held(cls, project_id: str) -> bool:
        lock = cls._locks.get(project_id)
        return lock is not None and lock.locked()

    @classmethod
    def reset(cls) -> None:
        """Drop every lock (tests: locks bind to the loop that first used them)."""
        cls._locks.clear()
        cls._owners.clear()


def project_edit_locked(
    handler: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[T]]:
    """Hold the project's edit lock for the whole of a short async handler.

    For route handlers whose entire body is ``load → mutate → save`` on one
    project (identified by a ``project_id`` argument).  Apply *below* the
    router decorator; FastAPI still sees the original signature.
    """
    signature = inspect.signature(handler)

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        bound = signature.bind_partial(*args, **kwargs)
        project_id = bound.arguments.get("project_id")
        if not isinstance(project_id, str):
            raise TypeError(
                f"{handler.__name__}: project_edit_locked needs a str project_id argument"
            )
        async with ProjectLocks.hold(project_id):
            return await handler(*args, **kwargs)

    wrapper.__project_edit_locked__ = True  # type: ignore[attr-defined]
    return wrapper
