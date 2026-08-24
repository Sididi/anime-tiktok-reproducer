"""WebSocket endpoint for the Premiere Pro CEP panel (Premiere Link).

Auth happens inside the socket (first frame carries ATR_CEP_LINK_TOKEN) because
a browser WebSocket cannot set request headers.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket

router = APIRouter(prefix="/api/cep")


@router.websocket("/ws")
async def cep_ws(websocket: WebSocket) -> None:
    await websocket.app.state.cep_link.handle_socket(websocket)
