"""Content identity (sha256) for shared-source Drive dedup.

Resolution ladder, cheapest first:

1. The library series' own ``.atr_hash_cache.json`` (written by the Storage
   Box publish path) — read-only here, keyed by path relative to the series
   dir, validated against current size + mtime_ns.
2. A central write-through cache under ``cache_dir/drive_shared_sources``,
   keyed by inode (covers pure-mode duplicates, which hardlink the same
   bytes into several project dirs) and by absolute path.
3. Hashing the file now, then recording it in the central cache.

All methods are synchronous; callers wrap them in ``run_heavy``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from .storage_box_repository import (
    HASH_CACHE_FILENAME,
    LOCAL_STORAGE_BOX_METADATA,
    _load_hash_cache,
    _sha256_file,
)

logger = logging.getLogger("uvicorn.error")

_CENTRAL_CACHE_VERSION = 1
_lock = threading.Lock()


class SourceHashService:
    @classmethod
    def _central_cache_path(cls) -> Path:
        return settings.cache_dir / "drive_shared_sources" / "hash_cache.json"

    @staticmethod
    def _stat_signature(path: Path) -> tuple[int, int, str]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, f"{stat.st_dev}:{stat.st_ino}"

    @staticmethod
    def _entry_matches(entry: Any, size: int, mtime_ns: int) -> str | None:
        if not isinstance(entry, dict):
            return None
        if int(entry.get("size") or -1) != size:
            return None
        if int(entry.get("mtime_ns") or -1) != mtime_ns:
            return None
        sha256 = str(entry.get("sha256") or "")
        return sha256 if len(sha256) == 64 else None

    @classmethod
    def _series_cache_lookup(cls, path: Path, size: int, mtime_ns: int) -> str | None:
        """Walk ancestors for a library series dir carrying a hash cache."""
        for ancestor in path.parents:
            has_cache = (ancestor / HASH_CACHE_FILENAME).exists()
            is_series_dir = has_cache or (
                ancestor / LOCAL_STORAGE_BOX_METADATA
            ).exists()
            if not is_series_dir:
                continue
            if not has_cache:
                return None
            cache = _load_hash_cache(ancestor)
            relative = path.relative_to(ancestor).as_posix()
            return cls._entry_matches(cache.get(relative), size, mtime_ns)
        return None

    @classmethod
    def _load_central_cache(cls) -> dict[str, Any]:
        cache_path = cls._central_cache_path()
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": _CENTRAL_CACHE_VERSION, "by_inode": {}, "by_path": {}}
        if not isinstance(payload, dict):
            return {"version": _CENTRAL_CACHE_VERSION, "by_inode": {}, "by_path": {}}
        payload.setdefault("by_inode", {})
        payload.setdefault("by_path", {})
        return payload

    @classmethod
    def _store_central_entry(
        cls, path: Path, sha256: str, size: int, mtime_ns: int, inode_key: str
    ) -> None:
        entry = {"sha256": sha256, "size": size, "mtime_ns": mtime_ns}
        cache_path = cls._central_cache_path()
        with _lock:
            payload = cls._load_central_cache()
            payload["by_inode"][inode_key] = entry
            payload["by_path"][str(path)] = entry
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = cache_path.with_name(cache_path.name + ".tmp")
                tmp_path.write_text(
                    json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                tmp_path.replace(cache_path)
            except OSError:
                logger.warning(
                    "Failed to write shared-sources hash cache %s",
                    cache_path,
                    exc_info=True,
                )

    @classmethod
    def sha256_for(cls, path: Path) -> str:
        path = Path(path)
        size, mtime_ns, inode_key = cls._stat_signature(path)

        cached = cls._series_cache_lookup(path, size, mtime_ns)
        if cached:
            return cached

        with _lock:
            central = cls._load_central_cache()
            cached = cls._entry_matches(
                central["by_inode"].get(inode_key), size, mtime_ns
            ) or cls._entry_matches(central["by_path"].get(str(path)), size, mtime_ns)
        if cached:
            return cached

        sha256 = _sha256_file(path)
        cls._store_central_entry(path, sha256, size, mtime_ns, inode_key)
        return sha256

    @classmethod
    def sha256_for_many(
        cls,
        paths: list[Path],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[Path, str]:
        results: dict[Path, str] = {}
        for index, path in enumerate(paths):
            results[path] = cls.sha256_for(path)
            if on_progress is not None:
                on_progress(index + 1, len(paths))
        return results
