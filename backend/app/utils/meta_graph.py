from __future__ import annotations

import requests


def extract_graph_error(response: requests.Response) -> str:
    """Extract a readable error message from a Meta Graph API error response."""
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
