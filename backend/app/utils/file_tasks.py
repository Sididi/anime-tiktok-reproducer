"""Wait for filesystem workers to finish before acknowledging cancellation."""
import asyncio


async def file_io(function, /, *args, **kwargs):
    return await wait_for_worker(asyncio.to_thread(function, *args, **kwargs))


async def wait_for_worker(awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A Python thread cannot be cancelled. In particular, deleting its
        # destination while a copy is still running can recreate orphan files.
        try:
            await task
        except Exception:
            pass
        raise
