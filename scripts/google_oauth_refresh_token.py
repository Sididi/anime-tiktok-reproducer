#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from _env import env, load_dotenv


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

DEFAULT_SCOPES_SHARED = [
    DRIVE_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    YOUTUBE_FORCE_SSL_SCOPE,
]
DEFAULT_SCOPES_DRIVE = [DRIVE_SCOPE]
DEFAULT_SCOPES_YOUTUBE = [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_FORCE_SSL_SCOPE]

DEFAULT_SCOPES_BY_TARGET = {
    "shared": DEFAULT_SCOPES_SHARED,
    "drive": DEFAULT_SCOPES_DRIVE,
    "youtube": DEFAULT_SCOPES_YOUTUBE,
}

YOUTUBE_VERIFICATION_SCOPES = {
    YOUTUBE_UPLOAD_SCOPE,
    YOUTUBE_FORCE_SSL_SCOPE,
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly",
}


def _resolve_client_id(target: str) -> str | None:
    _ = target
    return env("ATR_GOOGLE_CLIENT_ID")


def _resolve_client_secret(target: str) -> str | None:
    _ = target
    return env("ATR_GOOGLE_CLIENT_SECRET")


def _default_scopes_for_target(target: str) -> list[str]:
    return list(DEFAULT_SCOPES_BY_TARGET[target])


def _has_youtube_scope(scopes: list[str]) -> bool:
    return any(scope in YOUTUBE_VERIFICATION_SCOPES for scope in scopes)


def _env_template_for_target(target: str, *, client_id: str, client_secret: str, refresh_token: str) -> list[str]:
    token_uri = "https://oauth2.googleapis.com/token"
    if target == "drive":
        return [
            f"ATR_GOOGLE_CLIENT_ID={client_id}",
            f"ATR_GOOGLE_CLIENT_SECRET={client_secret}",
            f"ATR_GOOGLE_DRIVE_REFRESH_TOKEN={refresh_token}",
            f"ATR_GOOGLE_TOKEN_URI={token_uri}",
        ]
    if target == "youtube":
        return [
            f"ATR_GOOGLE_CLIENT_ID={client_id}",
            f"ATR_GOOGLE_CLIENT_SECRET={client_secret}",
            f"ATR_GOOGLE_YOUTUBE_REFRESH_TOKEN={refresh_token}",
            f"ATR_GOOGLE_TOKEN_URI={token_uri}",
        ]
    return [
        f"ATR_GOOGLE_CLIENT_ID={client_id}",
        f"ATR_GOOGLE_CLIENT_SECRET={client_secret}",
        f"ATR_GOOGLE_REFRESH_TOKEN={refresh_token}",
        f"ATR_GOOGLE_TOKEN_URI={token_uri}",
    ]


def _required_env_help(target: str) -> str:
    _ = target
    return (
        "Missing Google OAuth credentials. Provide --client-id/--client-secret or set "
        "ATR_GOOGLE_CLIENT_ID and ATR_GOOGLE_CLIENT_SECRET in .env."
    )


def _print_youtube_channel_hint(youtube_channels: list[dict[str, str]]) -> None:
    if not youtube_channels:
        return
    if len(youtube_channels) == 1:
        print(f"ATR_YOUTUBE_CHANNEL_ID={youtube_channels[0].get('id')}")
        return
    print("# Set this explicitly to avoid uploading to the wrong channel:")
    print("# ATR_YOUTUBE_CHANNEL_ID=<one_of_the_channel_ids_listed_above>")


def _print_migration_hint(target: str) -> None:
    if target == "shared":
        print(
            "# Optional: if you want split Drive/YouTube refresh tokens, run this script again "
            "with --target drive and --target youtube."
        )
        return
    print("# Optional legacy compatibility fallback (if needed):")
    print("# ATR_GOOGLE_CLIENT_ID=...")
    print("# ATR_GOOGLE_CLIENT_SECRET=...")
    print("# ATR_GOOGLE_REFRESH_TOKEN=...")
    print("# ATR_GOOGLE_TOKEN_URI=https://oauth2.googleapis.com/token")


def _print_verification(verification: dict[str, Any]) -> list[dict[str, str]]:
    print("Verification:")
    if "drive_user" in verification:
        print(f"- drive_user: {verification['drive_user']}")
    if "drive_user_error" in verification:
        print(f"- drive_user_error: {verification['drive_user_error']}")

    youtube_channels: list[dict[str, str]] = verification.get("youtube_channels", [])
    if "youtube_channels_error" in verification:
        print(f"- youtube_channels_error: {verification['youtube_channels_error']}")
    elif youtube_channels:
        print("- youtube_channels:")
        for item in youtube_channels:
            print(f"  - {item.get('id')} ({item.get('title')})")
    elif "youtube_channels" in verification:
        print("- youtube_channels: none")
    return youtube_channels


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Google OAuth local flow and print refresh token for ATR env."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file to preload (default: .env)",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Google OAuth client id (defaults to ATR_GOOGLE_CLIENT_ID)",
    )
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Google OAuth client secret (defaults to ATR_GOOGLE_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--target",
        choices=["shared", "drive", "youtube"],
        default="shared",
        help="Which env profile to print and default scopes to request (default: shared).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Local callback host for OAuth redirect (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local callback port for OAuth (default: 8765)",
    )
    parser.add_argument(
        "--scopes",
        nargs="+",
        default=None,
        help="OAuth scopes override (default depends on --target)",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not auto-open browser; copy URL manually",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Print full token json payload (contains sensitive data)",
    )
    return parser


def _build_client_config(client_id: str, client_secret: str, host: str, port: int) -> dict[str, Any]:
    redirect_uri = f"http://{host}:{port}"
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def _verify_tokens(
    creds: Credentials,
    *,
    verify_drive: bool,
    verify_youtube: bool,
) -> dict[str, Any]:
    details: dict[str, Any] = {}

    if verify_drive:
        try:
            drive = build("drive", "v3", credentials=creds, cache_discovery=False)
            about = drive.about().get(fields="user(displayName,emailAddress)").execute()
            drive_user = about.get("user", {})
            details["drive_user"] = (
                f"{drive_user.get('displayName', 'unknown')} <{drive_user.get('emailAddress', 'unknown')}>"
            )
        except Exception as exc:
            details["drive_user_error"] = str(exc)

    if verify_youtube:
        try:
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            channels: list[dict[str, str]] = []
            request = youtube.channels().list(part="id,snippet", mine=True, maxResults=50)
            while request is not None:
                channel_resp = request.execute()
                items = channel_resp.get("items", [])
                if isinstance(items, list):
                    for channel in items:
                        if not isinstance(channel, dict):
                            continue
                        channel_id = str(channel.get("id") or "")
                        title = str(channel.get("snippet", {}).get("title") or "unknown")
                        if channel_id:
                            channels.append({"id": channel_id, "title": title})
                request = youtube.channels().list_next(request, channel_resp)
            details["youtube_channels"] = channels
        except Exception as exc:
            details["youtube_channels_error"] = str(exc)

    return details


def main() -> None:
    args = _parser().parse_args()
    load_dotenv(args.env_file)

    scopes = args.scopes or _default_scopes_for_target(args.target)

    client_id = args.client_id or _resolve_client_id(args.target)
    client_secret = args.client_secret or _resolve_client_secret(args.target)
    if not client_id or not client_secret:
        raise SystemExit(_required_env_help(args.target))

    flow = InstalledAppFlow.from_client_config(
        _build_client_config(client_id, client_secret, args.host, args.port),
        scopes=scopes,
    )
    redirect_uri = f"http://{args.host}:{args.port}"
    print("Target profile:")
    print(f"- {args.target}")
    print("Using OAuth client_id:")
    print(f"- {client_id}")
    print("Requested scopes:")
    for scope in scopes:
        print(f"- {scope}")
    print("Using OAuth redirect URI:")
    print(f"- {redirect_uri}")
    print("If you use a Google OAuth Web client, this exact URI must be in:")
    print("- Google Cloud Console > APIs & Services > Credentials > OAuth client > Authorized redirect URIs")
    print("If you use a Desktop OAuth client, prefer keeping this script default host/port.")

    try:
        creds = flow.run_local_server(
            host=args.host,
            port=args.port,
            open_browser=not args.no_open_browser,
            prompt="consent",
            authorization_prompt_message=(
                "Open this URL in a browser and authorize this app:\n{url}"
            ),
            success_message="Authentication complete. You can close this tab.",
            redirect_uri_trailing_slash=False,
        )
    except Exception as exc:
        message = str(exc)
        if "redirect_uri_mismatch" in message:
            raise SystemExit(
                "Google rejected the redirect URI (redirect_uri_mismatch).\n"
                f"Expected redirect URI from this run: {redirect_uri}\n"
                "Fix options:\n"
                "1. In Google Cloud OAuth client, add this exact URI to Authorized redirect URIs.\n"
                "2. Or rerun script with --host/--port matching an already-authorized URI.\n"
                "3. Prefer creating a Desktop OAuth client to avoid manual redirect URI management."
            ) from exc
        raise

    if not creds.refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Re-run and ensure consent is forced, "
            "or revoke previous app access in Google account permissions first."
        )

    verification = _verify_tokens(
        creds,
        verify_drive=DRIVE_SCOPE in scopes,
        verify_youtube=_has_youtube_scope(scopes),
    )
    expires_at = creds.expiry.astimezone(timezone.utc).isoformat() if creds.expiry else None

    print("\nGoogle OAuth completed.\n")
    youtube_channels = _print_verification(verification)

    print("\nSet these values in .env:")
    for line in _env_template_for_target(
        args.target,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=creds.refresh_token,
    ):
        print(line)
    if _has_youtube_scope(scopes):
        _print_youtube_channel_hint(youtube_channels)
    _print_migration_hint(args.target)
    if expires_at:
        print(f"# Access token expires at: {expires_at}")

    if args.dump_json:
        payload = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": creds.scopes,
            "expiry_utc": expires_at,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        print("\nFull token payload:")
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    main()
