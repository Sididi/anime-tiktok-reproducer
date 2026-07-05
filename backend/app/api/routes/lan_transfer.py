# backend/app/api/routes/lan_transfer.py
"""LAN transfer endpoints for the Premiere Pro CEP panel (spec:
docs/superpowers/specs/2026-07-05-lan-transfer-design.md)."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from ...config import settings

logger = logging.getLogger(__name__)

API_VERSION = 1


def require_lan_token(x_atr_lan_token: str | None = Header(default=None)) -> None:
    expected = settings.lan_transfer_token
    if not expected:
        raise HTTPException(status_code=503, detail="LAN transfer not configured")
    if not x_atr_lan_token or not hmac.compare_digest(x_atr_lan_token, expected):
        raise HTTPException(status_code=401, detail="Invalid LAN token")


router = APIRouter(prefix="/lan", tags=["lan-transfer"], dependencies=[Depends(require_lan_token)])


@router.get("/ping")
async def ping():
    return {"ok": True, "api_version": API_VERSION}
