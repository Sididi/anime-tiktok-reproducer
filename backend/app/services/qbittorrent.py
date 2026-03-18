"""qBittorrent Web API v2 client."""

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

    async def close(self) -> None:
        await self._client.aclose()
