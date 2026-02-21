from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from ...services.account_service import AccountService


router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("")
async def list_accounts():
    """List all configured accounts."""
    return {"accounts": AccountService.list_accounts()}


@router.get("/{account_id}/avatar")
async def get_account_avatar(account_id: str):
    """Serve the avatar image for an account."""
    path, content_type = AccountService.get_avatar_path(account_id)
    if path is None or not path.exists():
        # Return a 1x1 transparent PNG as placeholder
        return Response(
            content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
            media_type="image/png",
        )
    return FileResponse(path, media_type=content_type)
