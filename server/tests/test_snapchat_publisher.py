"""Exercise Snap's encryption wire format and safe interrupted-submit behavior."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.services import snapchat_publisher as snap

BASE = "https://businessapi.snapchat.com/v1/public_profiles/profile-1"
UPLOAD = "https://businessapi.snapchat.com/us/v1/public_profiles/profile-1/media/media-1/multipart-upload"
CREDENTIALS = {
    "profile_id": "profile-1",
    "client_id": "client-1",
    "client_secret": "secret-1",
    "refresh_token": "refresh-1",
    "locale": "fr_FR",
}


@pytest.fixture
def video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"a test mp4 payload, replaced ffprobe but real encryption")
    monkeypatch.setattr(snap, "_validate_video", AsyncMock())
    return path


def _mock_api(router: respx.MockRouter, *, rotated: bool = False):
    token = router.post(snap._TOKEN_URL).respond(
        json={
            "access_token": "access-1",
            "refresh_token": "refresh-2" if rotated else "refresh-1",
        }
    )
    media = router.post(f"{BASE}/media").respond(
        json={
            "request_status": "SUCCESS",
            "media_id": "media-1",
            "add_path": UPLOAD.removeprefix(snap._API_BASE),
            "finalize_path": UPLOAD,
        }
    )
    upload = router.post(UPLOAD).respond(json={"request_status": "SUCCESS"})
    submit = router.post(f"{BASE}/spotlights").respond(
        json={
            "request_status": "SUCCESS",
            "spotlight_id": "spotlight-1",
        }
    )
    return token, media, upload, submit


@respx.mock
async def test_publish_encrypts_multipart_and_persists_before_submit(video: Path, monkeypatch):
    token, media, upload, submit = _mock_api(respx, rotated=True)
    monkeypatch.setattr(snap, "_CHUNK_BYTES", 32)
    states, credentials = [], []

    async def record_state(state):
        if state["stage"] == "submitting":
            assert submit.call_count == 0
        states.append(state)

    result = await snap.SnapchatPublisher(CREDENTIALS).publish(
        video,
        "caption" * 40,
        on_state=record_state,
        on_credentials=credentials.append,
    )
    assert result.success is True
    assert result.resource_id == "spotlight-1"
    assert result.url == "https://www.snapchat.com/spotlight/spotlight-1"
    assert [s["stage"] for s in states] == ["media_ready", "submitting", "submitted"]
    assert credentials[0]["refresh_token"] == "refresh-2"
    form = parse_qs(token.calls[0].request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["client_secret"] == ["secret-1"]
    assert media.calls[0].request.headers["authorization"] == "Bearer access-1"
    container = json.loads(media.calls[0].request.content)
    ciphertext = bytearray()
    for number, call in enumerate(upload.calls[:-1], start=1):
        body = call.request.content
        assert f'name="part_number"\r\n\r\n{number}'.encode() in body
        boundary = call.request.headers["content-type"].split("boundary=", 1)[1].encode()
        file_part = next(part for part in body.split(b"--" + boundary) if b'name="file"' in part)
        ciphertext.extend(file_part.split(b"\r\n\r\n", 1)[1][:-2])
    decryptor = Cipher(
        algorithms.AES(base64.b64decode(container["key"])),
        modes.CBC(base64.b64decode(container["iv"])),
    ).decryptor()
    plaintext = decryptor.update(bytes(ciphertext)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    assert unpadder.update(plaintext) + unpadder.finalize() == video.read_bytes()
    assert b"FINALIZE" in upload.calls[-1].request.content
    post = json.loads(submit.calls[0].request.content)
    assert post == {
        "media_id": "media-1",
        "description": ("caption" * 40)[:160],
        "locale": "fr_FR",
        "skip_save_to_profile": False,
    }


@respx.mock
async def test_submit_timeout_cannot_duplicate_on_retry(video: Path):
    _, _, _, submit = _mock_api(respx)
    submit.mock(side_effect=httpx.ReadTimeout("unknown commit outcome"))
    states = []
    publisher = snap.SnapchatPublisher(CREDENTIALS)
    result = await publisher.publish(video, on_state=states.append)
    assert result.success is False
    assert result.uncertain is True
    assert result.state["stage"] == "submitting"
    assert states[-1]["stage"] == "submitting"
    again = await publisher.publish(video, state=result.state)
    assert again.uncertain is True
    assert submit.call_count == 1


@respx.mock
async def test_resume_submitted_does_not_require_credentials_or_network(video: Path):
    result = await snap.SnapchatPublisher({}).publish(
        video,
        state={
            "stage": "submitted",
            "resource_id": "existing",
            "url": "https://example.org/existing",
        },
    )
    assert result.success
    assert result.resource_id == "existing"
    assert len(respx.calls) == 0


@respx.mock
async def test_resume_uploaded_media_skips_file_read_and_upload(tmp_path: Path):
    _, media, upload, submit = _mock_api(respx)
    state = {
        "stage": "media_ready",
        "profile_id": "profile-1",
        "media_id": "existing-media",
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(tmp_path / "gone.mp4", state=state)
    assert result.success
    assert not media.called and not upload.called
    assert json.loads(submit.calls[0].request.content)["media_id"] == "existing-media"


@pytest.mark.parametrize("expires", ["invalid", "2000-01-01T00:00:00+00:00"])
@respx.mock
async def test_expired_media_is_recreated(video: Path, expires: str):
    _, media, _, _ = _mock_api(respx)
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(
        video,
        state={
            "stage": "media_ready",
            "profile_id": "profile-1",
            "media_id": "old",
            "expires_at": expires,
        },
    )
    assert result.success
    assert media.called


@respx.mock
async def test_finalization_failure_prevents_submission(video: Path):
    _, _, upload, submit = _mock_api(respx)
    upload.mock(
        side_effect=[
            httpx.Response(200, json={"request_status": "SUCCESS"}),
            httpx.Response(
                200, json={"request_status": "ERROR", "display_message": "Invalid media"}
            ),
        ]
    )
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(video)
    assert not result.success and not result.uncertain
    assert "Invalid media" in result.detail
    assert not submit.called


@respx.mock
async def test_submit_missing_id_is_uncertain_and_preserves_guard(video: Path):
    _, _, _, submit = _mock_api(respx)
    submit.respond(json={"request_status": "SUCCESS"})
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(video)
    assert not result.success and result.uncertain
    assert result.state["stage"] == "submitting"


@respx.mock
async def test_definitive_submit_rejection_can_safely_retry_existing_media(video: Path):
    _, media, _, submit = _mock_api(respx)
    submit.respond(400, json={"request_status": "ERROR", "display_message": "Invalid locale"})
    states = []
    publisher = snap.SnapchatPublisher(CREDENTIALS)
    result = await publisher.publish(video, on_state=states.append)
    assert not result.success and not result.uncertain
    assert states[-1]["stage"] == "media_ready"
    assert "Invalid locale" in result.detail
    submit.respond(json={"request_status": "SUCCESS", "spotlight_id": "spotlight-1"})
    retry = await publisher.publish(video, state=result.state)
    assert retry.success
    assert media.call_count == 1


@pytest.mark.parametrize("status", [408, 500, 503])
@respx.mock
async def test_ambiguous_submit_error_keeps_duplicate_guard(video: Path, status: int):
    _, _, _, submit = _mock_api(respx)
    submit.respond(status, json={"request_status": "ERROR", "display_message": "Try later"})
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(video)
    assert not result.success and result.uncertain
    assert result.state["stage"] == "submitting"


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/multipart-upload",
        "//evil.example/multipart-upload",
        "http://businessapi.snapchat.com/multipart-upload",
        "/bad-path",
        None,
    ],
)
@respx.mock
async def test_rejects_unsafe_or_invalid_upload_path(video: Path, path: str):
    _, media, upload, submit = _mock_api(respx)
    media.respond(
        json={
            "request_status": "SUCCESS",
            "media_id": "media-1",
            "add_path": path,
            "finalize_path": UPLOAD,
        }
    )
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(video)
    assert not result.success
    assert not upload.called and not submit.called


@respx.mock
async def test_missing_credentials_fail_without_network(video: Path):
    result = await snap.SnapchatPublisher({}).publish(video)
    assert not result.success and "profile_id" in result.detail
    assert len(respx.calls) == 0


@respx.mock
async def test_oauth_failure_does_not_leak_secrets(video: Path):
    token, media, _, _ = _mock_api(respx)
    token.respond(
        401,
        json={
            "error": "invalid_grant",
            "error_description": "refresh-1 secret-1 was revoked",
        },
    )
    result = await snap.SnapchatPublisher(CREDENTIALS).publish(video)
    assert not result.success
    assert "revoked" in result.detail
    assert "refresh-1" not in result.detail and "secret-1" not in result.detail
    assert not media.called


@pytest.mark.parametrize(
    "duration,width,height,format_name,error",
    [
        (5, 1080, 1920, "mov,mp4", "between 6 and 60"),
        (61, 1080, 1920, "mov,mp4", "between 6 and 60"),
        (float("nan"), 1080, 1920, "mov,mp4", "between 6 and 60"),
        (30, 539, 960, "mov,mp4", "540x960"),
        (30, 540, 959, "mov,mp4", "540x960"),
        (30, 1080, 1920, "matroska", "MP4 media"),
    ],
)
async def test_rejects_invalid_video(
    tmp_path,
    monkeypatch,
    duration,
    width,
    height,
    format_name,
    error,
):
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"test")
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (
        json.dumps(
            {
                "format": {"duration": duration, "format_name": format_name},
                "streams": [{"codec_type": "video", "width": width, "height": height}],
            }
        ).encode(),
        b"",
    )
    monkeypatch.setattr(snap.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(snap._SnapchatError, match=error):
        await snap._validate_video(video_path)


@pytest.mark.parametrize("duration", [6, 60])
async def test_accepts_documented_duration_boundaries(tmp_path, monkeypatch, duration):
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"test")
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (
        json.dumps(
            {
                "format": {"duration": duration, "format_name": "mov,mp4"},
                "streams": [{"codec_type": "video", "width": 540, "height": 960}],
            }
        ).encode(),
        b"",
    )
    monkeypatch.setattr(snap.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    await snap._validate_video(video_path)
