from __future__ import annotations

import json
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.social_upload_service import SocialUploadService


def _response(status_code: int, payload: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


def test_facebook_caption_retries_fresh_object_subcode_33() -> None:
    response = _response(
        400,
        {
            "error": {
                "message": "Unsupported post request. Object does not exist.",
                "code": 100,
                "error_subcode": 33,
            }
        },
    )

    assert SocialUploadService._is_facebook_caption_retryable(response) is True


def test_facebook_caption_does_not_retry_other_graph_method_errors() -> None:
    response = _response(
        400,
        {
            "error": {
                "message": "Invalid parameter.",
                "code": 100,
                "error_subcode": 99,
            }
        },
    )

    assert SocialUploadService._is_facebook_caption_retryable(response) is False


def test_facebook_caption_retries_and_uses_default_locale(
    monkeypatch,
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "video.fr_FR.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBonjour\n",
        encoding="utf-8",
    )
    responses = [
        _response(
            400,
            {
                "error": {
                    "message": "Unsupported post request. Object does not exist.",
                    "code": 100,
                    "error_subcode": 33,
                }
            },
        ),
        _response(200, {"success": True}),
    ]
    payloads: list[dict[str, str]] = []

    class Session:
        def post(self, _url, *, data, files, timeout):
            del files, timeout
            payloads.append(data)
            return responses.pop(0)

    sleeps: list[float] = []
    monkeypatch.setattr(
        SocialUploadService,
        "_sleep_with_deadline",
        classmethod(lambda cls, seconds, **kwargs: sleeps.append(seconds)),
    )

    response = SocialUploadService._upload_facebook_caption_with_wait(
        session=Session(),
        base="https://graph.facebook.com/v25.0",
        video_id="28154744824154095",
        token="page-token",
        subtitle_path=subtitle,
        subtitle_locale="fr_FR",
    )

    assert response.status_code == 200
    assert sleeps == [5]
    assert payloads == [
        {"access_token": "page-token", "default_locale": "fr_FR"},
        {"access_token": "page-token", "default_locale": "fr_FR"},
    ]
