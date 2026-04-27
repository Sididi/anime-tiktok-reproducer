"""FastAPI auth dependencies. Settings are read from app.state at request time."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request


def _bearer(authorization: str) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization[7:].strip()


async def require_internal_token(
    request: Request, authorization: str = Header(default="")
) -> None:
    token = _bearer(authorization)
    expected = request.app.state.settings.internal_api_token
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid token")


