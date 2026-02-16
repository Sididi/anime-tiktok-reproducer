#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from typing import Any

import requests

from _env import env, load_dotenv


def _graph_error(response: requests.Response) -> str:
    try:
        payload = response.json()
        err = payload.get("error", {})
        if isinstance(err, dict):
            message = str(err.get("message") or "").strip()
            code = err.get("code")
            subcode = err.get("error_subcode")
            fbtrace = err.get("fbtrace_id")
            parts: list[str] = []
            if message:
                parts.append(message)
            if code is not None:
                parts.append(f"code={code}")
            if subcode is not None:
                parts.append(f"subcode={subcode}")
            if fbtrace:
                parts.append(f"fbtrace={fbtrace}")
            if parts:
                return " | ".join(parts)
    except Exception:
        pass

    raw = response.text.strip()
    return raw[:600] if raw else f"HTTP {response.status_code}"


def _request_json(method: str, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(method, url, params=params, timeout=30)
    if response.status_code >= 400:
        error_text = _graph_error(response)
        try:
            payload = response.json()
            err = payload.get("error", {})
            code = err.get("code") if isinstance(err, dict) else None
            subcode = err.get("error_subcode") if isinstance(err, dict) else None
            if code == 100 and subcode == 33:
                raise SystemExit(
                    f"{error_text}\n\n"
                    "Diagnostic hint:\n"
                    "1. The page id may be wrong (use Facebook PAGE id, not IG business id).\n"
                    "2. The token does not have access to this page asset.\n"
                    "3. If using a system user token, assign this page to the system user in Meta Business Settings,\n"
                    "   then regenerate the token with required permissions."
                )
        except SystemExit:
            raise
        except Exception:
            pass
        raise SystemExit(error_text)
    payload = response.json()
    if not isinstance(payload, dict):
        raise SystemExit(f"Unexpected non-object response: {payload}")
    return payload


def _paginate_accounts(url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    next_url = url
    next_params: dict[str, Any] | None = params
    while next_url:
        payload = _request_json("GET", next_url, params=next_params)
        next_params = None
        data = payload.get("data", [])
        if isinstance(data, list):
            pages.extend(item for item in data if isinstance(item, dict))
        paging = payload.get("paging") or {}
        cursor = paging.get("next") if isinstance(paging, dict) else None
        next_url = str(cursor) if cursor else ""
    return pages


def _debug_token_type(input_token: str) -> str | None:
    """Best-effort token type lookup via /debug_token (requires app id/secret in env)."""
    app_id = env("ATR_META_APP_ID")
    app_secret = env("ATR_META_APP_SECRET")
    if not app_id or not app_secret:
        return None
    try:
        payload = _request_json(
            "GET",
            "https://graph.facebook.com/debug_token",
            params={
                "input_token": input_token,
                "access_token": f"{app_id}|{app_secret}",
            },
        )
        data = payload.get("data", {})
        if isinstance(data, dict):
            token_type = data.get("type")
            if token_type:
                return str(token_type)
    except Exception:
        return None
    return None


def cmd_exchange_user_token(args: argparse.Namespace) -> None:
    load_dotenv(args.env_file)
    app_id = args.app_id or env("ATR_META_APP_ID")
    app_secret = args.app_secret or env("ATR_META_APP_SECRET")
    user_token = args.user_token or env("ATR_META_USER_ACCESS_TOKEN")

    if not app_id or not app_secret or not user_token:
        raise SystemExit(
            "Missing app_id/app_secret/user_token. Provide arguments or set "
            "ATR_META_APP_ID, ATR_META_APP_SECRET, ATR_META_USER_ACCESS_TOKEN."
        )

    payload = _request_json(
        "GET",
        "https://graph.facebook.com/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        },
    )
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 0))
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in > 0
        else None
    )
    if not access_token:
        raise SystemExit(f"Meta response did not include access_token: {payload}")

    print("Long-lived user token acquired.")
    print(f"expires_in_seconds: {expires_in}")
    if expires_at:
        print(f"expires_at_utc: {expires_at.isoformat()}")
    print("\nSet in .env:")
    print("ATR_META_TOKEN_MODE=long_lived_user")
    print(f"ATR_META_APP_ID={app_id}")
    print(f"ATR_META_APP_SECRET={app_secret}")
    print(f"ATR_META_USER_ACCESS_TOKEN={access_token}")
    if expires_at:
        print(f"ATR_META_USER_ACCESS_TOKEN_EXPIRES_AT={expires_at.isoformat()}")


def cmd_debug_token(args: argparse.Namespace) -> None:
    load_dotenv(args.env_file)
    app_id = args.app_id or env("ATR_META_APP_ID")
    app_secret = args.app_secret or env("ATR_META_APP_SECRET")
    input_token = args.input_token or env("ATR_META_USER_ACCESS_TOKEN")
    if not app_id or not app_secret or not input_token:
        raise SystemExit("Missing app_id/app_secret/input_token.")

    app_access_token = f"{app_id}|{app_secret}"
    payload = _request_json(
        "GET",
        "https://graph.facebook.com/debug_token",
        params={
            "input_token": input_token,
            "access_token": app_access_token,
        },
    )
    print(json.dumps(payload, indent=2))


def cmd_resolve_page_assets(args: argparse.Namespace) -> None:
    load_dotenv(args.env_file)
    graph_version = args.graph_version or env("ATR_META_GRAPH_API_VERSION") or "v22.0"
    user_token = args.user_token or env("ATR_META_USER_ACCESS_TOKEN")
    page_id_hint = args.page_id or env("ATR_FACEBOOK_PAGE_ID")
    if not user_token:
        raise SystemExit("Missing user token. Provide --user-token or ATR_META_USER_ACCESS_TOKEN.")

    base = f"https://graph.facebook.com/{graph_version}"
    pages = _paginate_accounts(
        f"{base}/me/accounts",
        {
            "fields": "id,name,access_token,instagram_business_account{id}",
            "access_token": user_token,
        },
    )
    if not pages:
        token_type = _debug_token_type(user_token)
        hint = (
            "\nHint: this command expects a user token that can call /me/accounts."
            "\nIf you are using a SYSTEM_USER token, use:\n"
            "  meta_token_helper.py resolve-from-page-token --page-id <PAGE_ID> --token <SYSTEM_USER_TOKEN>"
            "\nAnd ensure the page asset is assigned to this system user in Meta Business Settings."
        )
        if token_type:
            hint = f"\nDetected token type: {token_type}.{hint}"
        raise SystemExit(f"No pages returned by /me/accounts for this token.{hint}")

    selected: dict[str, Any] | None = None
    if page_id_hint:
        for page in pages:
            if str(page.get("id")) == page_id_hint:
                selected = page
                break
        if not selected:
            available = ", ".join(
                f"{page.get('id')} ({page.get('name', 'unknown')})" for page in pages[:10]
            )
            token_type = _debug_token_type(user_token)
            type_hint = f"\nDetected token type: {token_type}." if token_type else ""
            raise SystemExit(
                f"ATR_FACEBOOK_PAGE_ID={page_id_hint} not found in /me/accounts response."
                f"{type_hint}\nAvailable pages from token: {available or 'none'}"
                "\nIf you are using a SYSTEM_USER token, use resolve-from-page-token and ensure page asset assignment."
            )
    elif len(pages) == 1:
        selected = pages[0]
    else:
        print("Multiple pages found. Re-run with --page-id <id>.")
        for page in pages:
            print(f"- id={page.get('id')} name={page.get('name')}")
        raise SystemExit(2)

    assert selected is not None
    page_id = str(selected.get("id") or "")
    page_name = str(selected.get("name") or "")
    page_token = str(selected.get("access_token") or "")
    ig_obj = selected.get("instagram_business_account") or {}
    ig_id = str(ig_obj.get("id")) if isinstance(ig_obj, dict) and ig_obj.get("id") else ""

    if not ig_id:
        page_payload = _request_json(
            "GET",
            f"{base}/{page_id}",
            params={
                "fields": "instagram_business_account{id}",
                "access_token": page_token,
            },
        )
        ig_lookup = page_payload.get("instagram_business_account")
        if isinstance(ig_lookup, dict) and ig_lookup.get("id"):
            ig_id = str(ig_lookup["id"])

    print("Page assets resolved.")
    print(f"page_name: {page_name}")
    print(f"facebook_page_id: {page_id}")
    print(f"instagram_business_account_id: {ig_id or 'NOT_FOUND'}")
    print("\nFor system_user mode (.env):")
    print("ATR_META_TOKEN_MODE=system_user")
    print(f"ATR_FACEBOOK_PAGE_ID={page_id}")
    print(f"ATR_FACEBOOK_PAGE_ACCESS_TOKEN={page_token}")
    if ig_id:
        print(f"ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID={ig_id}")
        print(f"ATR_INSTAGRAM_ACCESS_TOKEN={page_token}")
    else:
        print("# ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID=fill manually")


def cmd_verify(args: argparse.Namespace) -> None:
    load_dotenv(args.env_file)
    graph_version = args.graph_version or env("ATR_META_GRAPH_API_VERSION") or "v22.0"
    page_id = args.page_id or env("ATR_FACEBOOK_PAGE_ID")
    page_token = args.page_token or env("ATR_FACEBOOK_PAGE_ACCESS_TOKEN")
    ig_id = args.ig_id or env("ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID")
    ig_token = args.ig_token or env("ATR_INSTAGRAM_ACCESS_TOKEN") or page_token

    if not page_id or not page_token:
        raise SystemExit("Missing page_id/page_token.")
    if not ig_id or not ig_token:
        raise SystemExit("Missing instagram_business_account_id / instagram token.")

    base = f"https://graph.facebook.com/{graph_version}"
    page_payload = _request_json(
        "GET",
        f"{base}/{page_id}",
        params={"fields": "id,name", "access_token": page_token},
    )
    ig_payload = _request_json(
        "GET",
        f"{base}/{ig_id}",
        params={"fields": "id,username", "access_token": ig_token},
    )
    print("Meta credentials verified.")
    print(f"facebook_page: {page_payload.get('name')} ({page_payload.get('id')})")
    print(f"instagram_account: {ig_payload.get('username')} ({ig_payload.get('id')})")


def cmd_resolve_from_page_token(args: argparse.Namespace) -> None:
    load_dotenv(args.env_file)
    graph_version = args.graph_version or env("ATR_META_GRAPH_API_VERSION") or "v22.0"
    page_id = args.page_id or env("ATR_FACEBOOK_PAGE_ID")
    page_token = args.page_token or args.token or env("ATR_FACEBOOK_PAGE_ACCESS_TOKEN")
    if not page_id or not page_token:
        raise SystemExit("Missing page_id/page_token.")

    base = f"https://graph.facebook.com/{graph_version}"
    page_payload = _request_json(
        "GET",
        f"{base}/{page_id}",
        params={
            "fields": "id,name,instagram_business_account{id}",
            "access_token": page_token,
        },
    )
    page_name = str(page_payload.get("name") or "")
    ig_obj = page_payload.get("instagram_business_account")
    ig_id = str(ig_obj.get("id")) if isinstance(ig_obj, dict) and ig_obj.get("id") else ""

    print("Page token resolved.")
    print(f"page_name: {page_name}")
    print(f"facebook_page_id: {page_id}")
    print(f"instagram_business_account_id: {ig_id or 'NOT_FOUND'}")
    print("\nFor system_user mode (.env):")
    print("ATR_META_TOKEN_MODE=system_user")
    print(f"ATR_FACEBOOK_PAGE_ID={page_id}")
    print(f"ATR_FACEBOOK_PAGE_ACCESS_TOKEN={page_token}")
    if ig_id:
        print(f"ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID={ig_id}")
        print(f"ATR_INSTAGRAM_ACCESS_TOKEN={page_token}")
    else:
        print("# ATR_INSTAGRAM_BUSINESS_ACCOUNT_ID=fill manually")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Utilities to acquire and validate Meta tokens for ATR."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exchange = sub.add_parser("exchange-user-token", help="Exchange user token to long-lived token")
    p_exchange.add_argument("--env-file", default=".env")
    p_exchange.add_argument("--app-id")
    p_exchange.add_argument("--app-secret")
    p_exchange.add_argument("--user-token")
    p_exchange.set_defaults(func=cmd_exchange_user_token)

    p_debug = sub.add_parser("debug-token", help="Call /debug_token on a token")
    p_debug.add_argument("--env-file", default=".env")
    p_debug.add_argument("--app-id")
    p_debug.add_argument("--app-secret")
    p_debug.add_argument("--input-token")
    p_debug.set_defaults(func=cmd_debug_token)

    p_resolve = sub.add_parser(
        "resolve-page-assets",
        help="Resolve page id/token and IG business id from a user token",
    )
    p_resolve.add_argument("--env-file", default=".env")
    p_resolve.add_argument("--graph-version")
    p_resolve.add_argument("--user-token")
    p_resolve.add_argument("--page-id")
    p_resolve.set_defaults(func=cmd_resolve_page_assets)

    p_verify = sub.add_parser("verify", help="Verify page + IG IDs/tokens")
    p_verify.add_argument("--env-file", default=".env")
    p_verify.add_argument("--graph-version")
    p_verify.add_argument("--page-id")
    p_verify.add_argument("--page-token")
    p_verify.add_argument("--ig-id")
    p_verify.add_argument("--ig-token")
    p_verify.set_defaults(func=cmd_verify)

    p_page = sub.add_parser(
        "resolve-from-page-token",
        help="Resolve IDs from a page token OR system user token that can access the page",
    )
    p_page.add_argument("--env-file", default=".env")
    p_page.add_argument("--graph-version")
    p_page.add_argument("--page-id")
    p_page.add_argument("--page-token")
    p_page.add_argument(
        "--token",
        help="Alias of --page-token (can be a system user token if page asset access is granted)",
    )
    p_page.set_defaults(func=cmd_resolve_from_page_token)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
