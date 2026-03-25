from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Callable

from ..config import settings

try:
    import asyncssh
except ImportError:  # pragma: no cover - exercised only when dependency missing
    asyncssh = None


logger = logging.getLogger("uvicorn.error")

ProgressHandler = Callable[[str, str, int, int], None]


@dataclass
class _ClientHandle:
    connection: object
    sftp: object


class StorageBoxSftpClient:
    """Thin SFTP transport with small pooled session reuse."""

    _pool_lock = asyncio.Lock()
    _pool: list[_ClientHandle] = []
    _semaphore: asyncio.Semaphore | None = None

    @classmethod
    def is_configured(cls) -> bool:
        return bool(
            settings.storage_box_enabled
            and settings.storage_box_host
            and settings.storage_box_username
        )

    @classmethod
    def _require_dependency(cls):
        if asyncssh is None:
            raise RuntimeError(
                "asyncssh is not installed. Add it to the runtime environment before using the Storage Box."
            )

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        if cls._semaphore is None:
            cls._semaphore = asyncio.Semaphore(max(1, settings.storage_box_max_connections))
        return cls._semaphore

    @classmethod
    async def _connect_handle(cls) -> _ClientHandle:
        cls._require_dependency()
        client_keys = None
        if settings.storage_box_ssh_key_path:
            client_keys = [str(settings.storage_box_ssh_key_path)]

        connect_kwargs = {
            "host": settings.storage_box_host,
            "port": settings.storage_box_port,
            "username": settings.storage_box_username,
            "client_keys": client_keys,
            "password": settings.storage_box_password or None,
        }
        if settings.storage_box_known_hosts_path:
            connect_kwargs["known_hosts"] = str(settings.storage_box_known_hosts_path)
        connection = await asyncssh.connect(**connect_kwargs)
        sftp = await connection.start_sftp_client()
        return _ClientHandle(connection=connection, sftp=sftp)

    @classmethod
    async def _acquire_handle(cls) -> _ClientHandle:
        await cls._get_semaphore().acquire()
        try:
            async with cls._pool_lock:
                while cls._pool:
                    handle = cls._pool.pop()
                    if not handle.connection.is_closing():
                        return handle
            return await cls._connect_handle()
        except Exception:
            cls._get_semaphore().release()
            raise

    @classmethod
    async def _release_handle(cls, handle: _ClientHandle) -> None:
        try:
            if handle.connection.is_closing():
                await handle.sftp.exit()
                handle.connection.close()
                await handle.connection.wait_closed()
            else:
                async with cls._pool_lock:
                    cls._pool.append(handle)
        finally:
            cls._get_semaphore().release()

    @classmethod
    async def close_pool(cls) -> None:
        async with cls._pool_lock:
            pool = cls._pool[:]
            cls._pool.clear()
        for handle in pool:
            try:
                await handle.sftp.exit()
            except Exception:
                pass
            handle.connection.close()
            try:
                await handle.connection.wait_closed()
            except Exception:
                pass

    @classmethod
    @asynccontextmanager
    async def sftp_session(cls) -> AsyncIterator[object]:
        if not cls.is_configured():
            raise RuntimeError("Storage Box is not configured.")
        handle = await cls._acquire_handle()
        try:
            yield handle.sftp
        finally:
            await cls._release_handle(handle)

    @staticmethod
    def normalize_remote_path(value: str | PurePosixPath) -> PurePosixPath:
        path = PurePosixPath(str(value).strip() or ".")
        if str(path) == ".":
            return path
        if path.is_absolute():
            return path
        root = PurePosixPath(str(settings.storage_box_root or "").strip() or ".")
        return root / path

    @classmethod
    async def exists(cls, remote_path: str | PurePosixPath) -> bool:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            return await sftp.exists(str(remote))

    @classmethod
    async def stat(cls, remote_path: str | PurePosixPath):
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            return await sftp.stat(str(remote))

    @classmethod
    async def makedirs(cls, remote_path: str | PurePosixPath) -> None:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            await sftp.makedirs(str(remote), exist_ok=True)

    @classmethod
    async def listdir(cls, remote_path: str | PurePosixPath) -> list[str]:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            return list(await sftp.listdir(str(remote)))

    @classmethod
    async def scandir(cls, remote_path: str | PurePosixPath):
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            return [entry async for entry in sftp.scandir(str(remote))]

    @classmethod
    async def rename(cls, src: str | PurePosixPath, dst: str | PurePosixPath) -> None:
        src_path = cls.normalize_remote_path(src)
        dst_path = cls.normalize_remote_path(dst)
        async with cls.sftp_session() as sftp:
            await sftp.makedirs(str(dst_path.parent), exist_ok=True)
            await sftp.rename(str(src_path), str(dst_path))

    @classmethod
    async def remove_file(cls, remote_path: str | PurePosixPath) -> None:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            await sftp.remove(str(remote))

    @classmethod
    async def remove_tree(cls, remote_path: str | PurePosixPath) -> None:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            await sftp.rmtree(str(remote))

    @classmethod
    async def upload_file(
        cls,
        local_path: Path,
        remote_path: str | PurePosixPath,
        *,
        progress_handler: ProgressHandler | None = None,
    ) -> None:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            await sftp.makedirs(str(remote.parent), exist_ok=True)
            await sftp.put(
                str(local_path),
                str(remote),
                progress_handler=progress_handler,
            )

    @classmethod
    async def download_file(
        cls,
        remote_path: str | PurePosixPath,
        local_path: Path,
        *,
        progress_handler: ProgressHandler | None = None,
    ) -> None:
        remote = cls.normalize_remote_path(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        async with cls.sftp_session() as sftp:
            await sftp.get(
                str(remote),
                str(local_path),
                progress_handler=progress_handler,
            )

    @classmethod
    async def write_text(
        cls,
        remote_path: str | PurePosixPath,
        content: str,
    ) -> None:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            await sftp.makedirs(str(remote.parent), exist_ok=True)
            async with sftp.open(str(remote), "w") as handle:
                await handle.write(content)

    @classmethod
    async def read_text(
        cls,
        remote_path: str | PurePosixPath,
    ) -> str:
        remote = cls.normalize_remote_path(remote_path)
        async with cls.sftp_session() as sftp:
            async with sftp.open(str(remote), "r") as handle:
                return await handle.read()

    @classmethod
    async def health_check(cls) -> dict[str, object]:
        if not cls.is_configured():
            return {"configured": False, "available": False, "latency_ms": None}

        started = asyncio.get_running_loop().time()
        try:
            async with cls.sftp_session() as sftp:
                await sftp.listdir(str(cls.normalize_remote_path(".")))
        except Exception as exc:
            logger.warning("Storage Box health check failed: %s", exc)
            return {"configured": True, "available": False, "latency_ms": None, "error": str(exc)}

        latency_ms = round((asyncio.get_running_loop().time() - started) * 1000, 1)
        return {"configured": True, "available": True, "latency_ms": latency_ms}
