"""Crash-safe text file writes (write to a sibling temp file, then rename)."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import suppress
from pathlib import Path

_REPLACE_ATTEMPTS = 3


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` so readers only ever see the old or the new file.

    The temp file gets a unique name (two concurrent writers of the same path
    cannot collide on it) and lives in the same directory so the final
    ``os.replace`` is an atomic rename.  On Windows a reader holding the file
    open makes the rename fail with ``PermissionError``; that is retried a few
    times before giving up.  The temp file is removed on any failure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(text, encoding=encoding)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        with suppress(OSError):
            tmp_path.unlink()
        raise
