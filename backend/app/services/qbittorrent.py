"""qBittorrent Web API v2 client."""

import asyncio
import logging

import httpx

from ..config import settings

logger = logging.getLogger("uvicorn.error")


class QBittorrentClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.qbittorrent_url, timeout=30
        )
        self._authenticated = False

    async def login(self) -> None:
        resp = await self._client.post(
            "/api/v2/auth/login",
            data={
                "username": settings.qbittorrent_username,
                "password": settings.qbittorrent_password,
            },
        )
        if resp.text.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {resp.text}")
        self._authenticated = True

    async def _ensure_auth(self) -> None:
        if not self._authenticated:
            await self.login()

    async def list_torrents(self) -> list[dict]:
        await self._ensure_auth()
        resp = await self._client.get("/api/v2/torrents/info")
        resp.raise_for_status()
        return resp.json()

    async def get_torrent_files(self, info_hash: str) -> list[dict]:
        await self._ensure_auth()
        resp = await self._client.get(
            "/api/v2/torrents/files", params={"hash": info_hash}
        )
        resp.raise_for_status()
        return resp.json()

    async def add_torrent(
        self,
        magnet_uri: str,
        save_path: str,
    ) -> None:
        await self._ensure_auth()
        data = {"urls": magnet_uri, "savepath": save_path, "autoTMM": "false"}
        resp = await self._client.post("/api/v2/torrents/add", data=data)
        resp.raise_for_status()

    async def set_file_priority(
        self, info_hash: str, file_ids: list[int], priority: int
    ) -> None:
        await self._ensure_auth()
        resp = await self._client.post(
            "/api/v2/torrents/filePrio",
            data={
                "hash": info_hash,
                "id": "|".join(str(i) for i in file_ids),
                "priority": str(priority),
            },
        )
        resp.raise_for_status()

    async def get_torrent_info(self, info_hash: str) -> dict | None:
        await self._ensure_auth()
        resp = await self._client.get(
            "/api/v2/torrents/info", params={"hashes": info_hash}
        )
        resp.raise_for_status()
        torrents = resp.json()
        return torrents[0] if torrents else None

    async def delete_torrent(
        self, info_hash: str, delete_files: bool = True
    ) -> None:
        """Remove a torrent from qBittorrent, optionally deleting its files."""
        await self._ensure_auth()
        resp = await self._client.post(
            "/api/v2/torrents/delete",
            data={
                "hashes": info_hash,
                "deleteFiles": "true" if delete_files else "false",
            },
        )
        resp.raise_for_status()

    async def wait_for_torrent_metadata(
        self, info_hash: str, timeout: float = 60
    ) -> list[dict]:
        """Poll until a newly added torrent resolves its metadata and file list.

        Returns the file list once available, or raises TimeoutError.
        """
        await self._ensure_auth()
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                files = await self.get_torrent_files(info_hash)
                # qBittorrent returns files once metadata is resolved.
                # A non-empty list with at least one file with a real name means ready.
                if files and any(f.get("name", "") for f in files):
                    return files
            except httpx.HTTPStatusError:
                pass
            await asyncio.sleep(2)
        raise TimeoutError(
            f"Torrent metadata did not resolve within {timeout}s for {info_hash}"
        )

    async def close(self) -> None:
        await self._client.aclose()
