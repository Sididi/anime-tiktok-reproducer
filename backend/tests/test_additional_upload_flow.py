"""Run the real upload orchestration with isolated media and transport boundaries."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.models import Project, VideoMetadataPayload
from app.services.account_service import (
    ADDITIONAL_PLATFORMS,
    AccountService,
    AccountTikTokConfig,
)
from app.services.discord_service import DiscordService
from app.services.export_service import ExportService
from app.services.google_drive_service import GoogleDriveService
from app.services.metadata import MetadataService
from app.services.post_for_me_client import PfmPublishOutcome, PostForMeClient
from app.services.project_duplication_service import UploadRestrictionService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService
from app.services.social_upload_service import MediaProbe, SocialUploadService
from app.services.upload_phase import UploadPhaseService, UploadReadiness

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 10, tzinfo=UTC)
FUTURE = NOW + timedelta(days=2)
DIRECT_URL = "https://drive.example/download/video-1"


def _account(mode="manual", disabled=()):
    configs = {
        "dailymotion": {
            "client_id": "dm-client",
            "client_secret": "dm-secret",
            "profile_id": "dm-profile",
        },
        "snapchat": {
            "client_id": "snap-client",
            "client_secret": "snap-secret",
            "profile_id": "snap-profile",
            "refresh_token": "snap-refresh",
            "locale": "fr_FR",
        },
        "bilibili": {
            "client_id": "bili-client",
            "client_secret": "bili-secret",
            "openid": "bili-openid",
            "access_token": "bili-access",
            "expires_at": 4102444800,
            "category_id": 27,
            "copyright": 1,
        },
    }
    return AccountService._parse_account(
        "account",
        {
            "name": "Account",
            "language": "fr",
            "slots": [],
            **{
                platform: {
                    "mode": mode,
                    "slots": [] if platform in disabled else ["14:00"],
                    **(credentials if mode == "api" else {}),
                }
                for platform, credentials in configs.items()
            },
        },
    )


@pytest.fixture
def flow(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    project = Project(id="project-1", anime_name="Anime", output_language="fr")
    metadata = VideoMetadataPayload.model_validate(
        {
            "youtube": {
                "title": "A title " * 30,
                "description": "Full description",
                "tags": ["anime"],
            },
            "facebook": {
                "title": "Facebook title",
                "description": "Facebook description",
                "tags": ["anime"],
            },
            "instagram": {"caption": "Instagram caption"},
            "tiktok": {"description": "TikTok caption"},
        }
    )
    readiness = UploadReadiness(
        status="green",
        metadata_exists=True,
        drive_video_count=1,
        drive_video_id="video-1",
        drive_video_name="output.mp4",
        drive_video_web_url="https://drive.example/view/video-1",
        reasons=[],
        drive_folder_id="folder-1",
        drive_folder_url="https://drive.example/folder-1",
    )
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n")
    context = SimpleNamespace(
        account=_account(),
        project=project,
        metadata=metadata,
        callbacks=[],
        create_job=MagicMock(
            return_value={"project_id": project.id, "discord_message_id": "msg-1"}
        ),
        save=MagicMock(),
        share=MagicMock(),
        probe=MagicMock(
            return_value=(MediaProbe(duration_seconds=30, has_audio=True), None)
        ),
        next_slot=MagicMock(return_value=(FUTURE, FUTURE + timedelta(minutes=2))),
    )
    monkeypatch.setattr("app.services.upload_phase.datetime", FixedDatetime)
    monkeypatch.setattr(
        "app.services.account_service.time",
        SimpleNamespace(time=lambda: NOW.timestamp()),
    )
    monkeypatch.setattr(ProjectService, "load", lambda project_id: project)
    monkeypatch.setattr(ProjectService, "save", context.save)
    monkeypatch.setattr(AccountService, "list_accounts", lambda: [context.account])
    monkeypatch.setattr(
        AccountService, "get_account", lambda account_id: context.account
    )
    monkeypatch.setattr(MetadataService, "load", lambda project_id: metadata)
    monkeypatch.setattr(
        UploadPhaseService, "compute_readiness", lambda project: readiness
    )
    monkeypatch.setattr(ExportService, "subtitle_path", lambda project: subtitle)
    monkeypatch.setattr(UploadRestrictionService, "validate_upload", MagicMock())
    monkeypatch.setattr(
        SchedulingService, "find_next_slot_for_platform", context.next_slot
    )
    monkeypatch.setattr(SocialUploadService, "_probe_media", context.probe)
    monkeypatch.setattr(GoogleDriveService, "set_public_read", context.share)
    monkeypatch.setattr(
        GoogleDriveService,
        "get_direct_download_url",
        lambda file_id: f"https://drive.example/download/{file_id}",
    )
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video", lambda project_id: None
    )
    monkeypatch.setattr(
        UploadPhaseService,
        "_stage_source_video",
        lambda project_id, ready, destination: destination.write_bytes(
            b"fake video for orchestration; no platform API is called"
        ),
    )
    monkeypatch.setattr(UploadPhaseService, "cleanup_facebook_prep", MagicMock())
    monkeypatch.setattr(UploadPhaseService, "cleanup_youtube_prep", MagicMock())
    monkeypatch.setattr(DiscordService, "is_configured", lambda: True)
    monkeypatch.setattr(DiscordService, "create_job", context.create_job)
    monkeypatch.setattr(DiscordService, "update_job_platform", MagicMock())
    monkeypatch.setattr(DiscordService, "delete_message", MagicMock())
    monkeypatch.setattr(DiscordService, "delete_job", MagicMock())
    # Any missed transport boundary fails the test instead of using live credentials.
    monkeypatch.setattr(
        "httpx.Client.request", MagicMock(side_effect=AssertionError("unexpected HTTP"))
    )
    monkeypatch.setattr(
        "requests.Session.request",
        MagicMock(side_effect=AssertionError("unexpected HTTP")),
    )
    return context


def _execute(flow, **kwargs):
    return UploadPhaseService.execute_upload(
        flow.project.id,
        account_id="account",
        platform_result_callback=flow.callbacks.append,
        **kwargs,
    )


@pytest.mark.parametrize("facecam", [False, True])
@pytest.mark.parametrize("immediate", [False, True])
def test_facecam_thumbnail_reaches_publishers_without_enabling_standard_covers(flow, monkeypatch, tmp_path, facecam, immediate):
    from app.services.facecam_publication_service import FacecamPublicationService
    from app.services.account_service import AccountYouTubeConfig, AccountMetaConfig, AccountFacebookConfig, AccountInstagramConfig
    from app.services.social_upload_service import PlatformUploadResult
    flow.project.project_mode = 'facecam' if facecam else 'standard'
    flow.account.supported_types = [flow.project.publishing_type]
    flow.account.youtube = AccountYouTubeConfig(refresh_token='fixture', slots=['14:00'])
    flow.account.facebook = AccountFacebookConfig(slots=['14:00'])
    flow.account.instagram = AccountInstagramConfig(slots=['14:00'])
    flow.account.meta = AccountMetaConfig(facebook_page_id='page', facebook_page_access_token='token',
        instagram_business_account_id='ig', instagram_access_token='token')
    cover = tmp_path / 'saved-choice.jpg'; cover.write_bytes(b'validated facecam image')
    guard = MagicMock(return_value=cover)
    monkeypatch.setattr(FacecamPublicationService, 'require_ready', guard)
    monkeypatch.setattr(AccountService, 'get_youtube_credentials', lambda _: None)
    monkeypatch.setattr(AccountService, 'get_meta_credentials', lambda _: SimpleNamespace(page_id='page', facebook_page_access_token='token'))
    monkeypatch.setattr(UploadPhaseService, '_youtube_prep_dir', lambda _: tmp_path / 'yt')
    monkeypatch.setattr(UploadPhaseService, '_facebook_prep_dir', lambda _: tmp_path / 'fb')
    monkeypatch.setattr(UploadPhaseService, '_prepare_instagram_drive_video', lambda **kw: (None, {'instagram_drive_video_url':DIRECT_URL}))
    uploaded = MagicMock(return_value={'id':'cover-1'})
    monkeypatch.setattr(GoogleDriveService, 'upsert_local_file', uploaded)
    calls = {}
    def publish(platform, **kwargs):
        image = kwargs.get('thumbnail_image_path')
        calls[platform] = image.read_bytes() if image else None
        return PlatformUploadResult(platform=platform, status='uploaded', url=f'https://example.org/{platform}')
    monkeypatch.setattr(SocialUploadService, 'upload_youtube', lambda **kw: publish('youtube', **kw))
    monkeypatch.setattr(SocialUploadService, 'upload_facebook', lambda **kw: publish('facebook', **kw))
    ig_now = MagicMock(return_value=PlatformUploadResult(platform='instagram',status='uploaded',url='https://example.org/ig'))
    monkeypatch.setattr(UploadPhaseService, '_publish_instagram_immediate', ig_now)
    _execute(flow, platforms=['youtube','facebook','instagram'], immediate_platforms=['instagram'] if immediate else [],
             thumbnail_candidate_index=1, thumbnail_timestamp_ms=1000)
    assert calls == {'youtube':b'validated facecam image' if facecam else None, 'facebook':b'validated facecam image' if facecam else None}
    assert guard.call_count == int(facecam)
    if immediate:
        payload = ig_now.call_args.kwargs['payload']
    else:
        payload = flow.create_job.call_args.kwargs['instagram']
    if facecam:
        assert payload['cover_url'] == 'https://drive.example/download/cover-1' and payload['use_custom_cover']
        assert uploaded.call_args.kwargs['filename'] == 'facecam_thumbnail_saved-choice.jpg'
    else:
        assert 'cover_url' not in payload
        uploaded.assert_not_called()


@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.parametrize("suffix", ["#WhatToWatch", "#NewCampaign", ""])
def test_tiktok_campaign_suffix_reaches_publish_and_reminder(
    flow, monkeypatch, immediate, suffix
):
    monkeypatch.setattr(
        "app.services.upload_phase.settings.tiktok_description_suffix", suffix
    )
    flow.account.tiktok = AccountTikTokConfig(
        post_for_me_account_id="spc_1", slots=["14:00"]
    )
    original_metadata = flow.metadata.model_dump()
    create_post = MagicMock(return_value="post-1")
    monkeypatch.setattr(
        PostForMeClient, "stage_media", MagicMock(return_value="https://pfm/media/1")
    )
    monkeypatch.setattr(PostForMeClient, "create_post", create_post)
    monkeypatch.setattr(
        PostForMeClient,
        "poll_outcome",
        MagicMock(return_value=PfmPublishOutcome(
            success=True, url="https://www.tiktok.com/@anime/video/123"
        )),
    )

    _execute(flow, immediate_platforms=["tiktok"] if immediate else [])

    expected = "TikTok caption" + (f" {suffix}" if suffix else "")
    create_post.assert_called_once()
    body = create_post.call_args.args[0]
    assert body["caption"] == expected
    if immediate:
        assert "scheduled_at" not in body
    else:
        assert body["scheduled_at"] == (FUTURE + timedelta(minutes=2)).isoformat()
    assert flow.project.tiktok_pfm.caption == expected
    sent = flow.create_job.call_args.kwargs
    assert sent["description"] == expected
    assert sent["publishers"]["dailymotion"]["description"] == "Full description"
    assert sent["publishers"]["bilibili"]["description"] == "Full description"
    assert sent["publishers"]["snapchat"]["description"] == flow.metadata.youtube.title[:160]
    assert flow.metadata.model_dump() == original_metadata


def test_manual_tiktok_reminder_uses_campaign_suffix(flow, monkeypatch):
    monkeypatch.setattr(
        "app.services.upload_phase.settings.tiktok_description_suffix", "#WhatToWatch"
    )
    flow.account.tiktok = AccountTikTokConfig(slots=["14:00"])
    _execute(flow)
    sent = flow.create_job.call_args.kwargs
    assert sent["tiktok_manual"] is True
    assert sent["description"] == "TikTok caption #WhatToWatch"
    assert flow.metadata.tiktok.description == "TikTok caption"


@pytest.mark.parametrize("mode", ["manual", "api"])
def test_full_upload_queues_each_opted_in_platform_and_persists_pending(flow, mode):
    flow.account = _account(mode)
    result = _execute(flow)
    sent = flow.create_job.call_args.kwargs
    assert set(sent["publishers"]) == set(ADDITIONAL_PLATFORMS)
    assert sent["account_id"] == "account"
    for platform in ADDITIONAL_PLATFORMS:
        payload = sent["publishers"][platform]
        assert payload["mode"] == mode
        assert payload["video_url"] == DIRECT_URL
        assert payload["credentials"]["title"] == flow.metadata.youtube.title
        assert sent["platform_statuses"][platform]["status"] == "pending"
        assert sent["platform_scheduled_at"][platform] == FUTURE + timedelta(minutes=2)
        assert "title" not in getattr(flow.account, platform).credentials
    assert (
        sent["publishers"]["snapchat"]["description"]
        == flow.metadata.youtube.title[:160]
    )
    assert sent["publishers"]["dailymotion"]["description"] == "Full description"
    assert sent["publishers"]["bilibili"]["description"] == "Full description"
    rows = {row["platform"]: row for row in result["platform_results"]}
    assert all(
        rows[platform]["status"] == "pending" for platform in ADDITIONAL_PLATFORMS
    )
    assert all(rows[platform]["url"] is None for platform in ADDITIONAL_PLATFORMS)
    assert flow.project.upload_last_result["platforms"] == result["platform_results"]
    assert flow.project.final_upload_discord_message_id == "msg-1"
    assert flow.project.upload_completed_at == NOW
    assert flow.save.called


@pytest.mark.parametrize("disabled", ADDITIONAL_PLATFORMS)
def test_slots_empty_do_not_auto_enroll_even_when_api_credentials_exist(flow, disabled):
    flow.account = _account("api", disabled=[disabled])
    result = _execute(flow)
    sent = flow.create_job.call_args.kwargs
    assert disabled not in sent["publishers"]
    assert disabled not in sent["platforms_requested"]
    assert disabled not in sent["platform_scheduled_at"]
    assert disabled not in result["requested_platforms"]
    assert disabled not in {row["platform"] for row in result["platform_results"]}


def test_explicit_platform_selection_does_not_enqueue_other_publishers(flow):
    result = _execute(flow, platforms=["snapchat"])
    sent = flow.create_job.call_args.kwargs
    assert set(sent["publishers"]) == {"snapchat"}
    assert sent["platforms_requested"] == ["snapchat"]
    assert [row["platform"] for row in result["platform_results"]] == ["snapchat"]


@pytest.mark.parametrize("mode", ["manual", "api"])
def test_immediate_upload_has_due_now_times_and_preserves_pending_state(flow, mode):
    flow.account = _account(mode)
    _execute(
        flow,
        platforms=list(ADDITIONAL_PLATFORMS),
        immediate_platforms=list(ADDITIONAL_PLATFORMS),
    )
    sent = flow.create_job.call_args.kwargs
    assert sent["platform_scheduled_at"] == {
        platform: NOW for platform in ADDITIONAL_PLATFORMS
    }
    assert all(
        sent["platform_statuses"][platform]["status"] == "pending"
        for platform in ADDITIONAL_PLATFORMS
    )
    flow.next_slot.assert_not_called()


@pytest.mark.parametrize("failure", [None, RuntimeError("VPS not reachable")])
def test_vps_create_failure_is_reported_and_persisted_as_failed(flow, failure):
    if failure is None:
        flow.create_job.return_value = None
    else:
        flow.create_job.side_effect = failure
    result = _execute(flow, platforms=list(ADDITIONAL_PLATFORMS))
    assert all(row["status"] == "failed" for row in result["platform_results"])
    assert all(
        "no schedule confirmed" in row["detail"] for row in result["platform_results"]
    )
    assert flow.project.upload_last_result["platforms"] == result["platform_results"]
    assert flow.project.final_upload_discord_message_id is None
    last_callbacks = {row["platform"]: row for row in flow.callbacks}
    assert all(row["status"] == "failed" for row in last_callbacks.values())


def test_missing_vps_configuration_fails_before_any_media_work(flow, monkeypatch):
    monkeypatch.setattr(DiscordService, "is_configured", lambda: False)
    with pytest.raises(ValueError, match="VPS server must be configured"):
        _execute(flow)
    flow.share.assert_not_called()
    flow.create_job.assert_not_called()


def test_invalid_api_credentials_fail_before_sharing_or_queuing(flow):
    flow.account = _account("api")
    flow.account.snapchat.credentials.pop("refresh_token")
    with pytest.raises(ValueError, match="refresh_token"):
        _execute(flow)
    flow.share.assert_not_called()
    flow.create_job.assert_not_called()


def test_bilibili_token_must_remain_valid_beyond_scheduled_dispatch(flow):
    flow.account = _account("api")
    flow.account.bilibili.credentials["expires_at"] = (
        NOW + timedelta(days=1)
    ).timestamp()
    with pytest.raises(
        ValueError, match="expires before the selected publication time"
    ):
        _execute(flow)
    flow.share.assert_not_called()
    flow.create_job.assert_not_called()


@pytest.mark.parametrize("duration", [5.9, 60.01, float("nan"), float("inf"), None])
def test_snapchat_invalid_duration_fails_before_dispatch_and_other_platforms_continue(
    flow, duration
):
    flow.account = _account("api")
    flow.probe.return_value = (
        MediaProbe(duration_seconds=duration, has_audio=True),
        None,
    )
    result = _execute(flow)
    sent = flow.create_job.call_args.kwargs
    assert "snapchat" not in sent["publishers"]
    assert set(sent["publishers"]) == {"dailymotion", "bilibili"}
    assert sent["platform_statuses"]["snapchat"]["status"] == "failed"
    assert "snapchat" in sent["platforms_requested"]
    rows = {row["platform"]: row for row in result["platform_results"]}
    assert rows["snapchat"]["status"] == "failed"
    assert rows["dailymotion"]["status"] == rows["bilibili"]["status"] == "pending"
    assert flow.project.upload_last_result["platforms"] == result["platform_results"]


def test_snapchat_unprobeable_video_is_reported_before_dispatch(flow):
    flow.account = _account("api")
    flow.probe.return_value = (None, "ffprobe failed to read the media")
    result = _execute(flow)
    snapchat = next(
        row for row in result["platform_results"] if row["platform"] == "snapchat"
    )
    assert snapchat["status"] == "failed"
    assert "ffprobe failed" in snapchat["detail"]
    assert "snapchat" not in flow.create_job.call_args.kwargs["publishers"]


@pytest.mark.parametrize("duration", [6, 60])
def test_snapchat_exact_duration_boundaries_remain_queued(flow, duration):
    flow.account = _account("api")
    flow.probe.return_value = (
        MediaProbe(duration_seconds=duration, has_audio=True),
        None,
    )
    _execute(flow)
    assert "snapchat" in flow.create_job.call_args.kwargs["publishers"]
    assert (
        flow.create_job.call_args.kwargs["platform_statuses"]["snapchat"]["status"]
        == "pending"
    )


def test_manual_snapchat_upload_is_not_subject_to_api_preflight(flow):
    flow.probe.side_effect = AssertionError(
        "Manual mode must not probe API constraints"
    )
    _execute(flow)
    flow.probe.assert_not_called()
    assert "snapchat" in flow.create_job.call_args.kwargs["publishers"]


def test_copyright_replacement_is_hosted_and_used_by_all_publishers(
    flow, tmp_path, monkeypatch
):
    replacement_audio = tmp_path / "replacement.wav"
    replacement_audio.write_bytes(b"test audio")
    monkeypatch.setattr(
        UploadPhaseService,
        "_replace_video_audio",
        lambda source, audio, destination: destination.write_bytes(
            b"replaced audio video"
        ),
    )
    upload_artifact = MagicMock(return_value={"id": "prepared-publishers"})
    monkeypatch.setattr(GoogleDriveService, "upsert_local_file", upload_artifact)
    _execute(
        flow,
        platforms=list(ADDITIONAL_PLATFORMS),
        copyright_audio_path=str(replacement_audio),
    )
    sent = flow.create_job.call_args.kwargs
    assert upload_artifact.call_args.kwargs["filename"] == "output_publishers.mp4"
    assert all(
        payload["video_url"] == "https://drive.example/download/prepared-publishers"
        for payload in sent["publishers"].values()
    )
    # Other destinations, including TikTok, still receive the original video URL.
    assert sent["drive_video_url"] == DIRECT_URL
