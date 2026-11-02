"""Facecam publication orchestration; transports and media work are isolated."""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from test_additional_upload_flow import flow, _execute, FUTURE  # noqa: F401
from app.config import Settings
from app.services.account_service import AccountService, AccountTikTokConfig
from app.services.facecam_publication_service import FacecamPublicationService
from app.services.facecam_metadata_service import FacecamMetadataService
from app.services.post_for_me_client import PfmPublishOutcome, PostForMeClient, build_post_body
from app.services.platform_reschedule_service import PlatformRescheduleService, NotificationResult
from app.services.discord_service import DiscordService


@pytest.fixture
def facecam(flow, tmp_path, monkeypatch):
    flow.project.project_mode = 'facecam'
    flow.account.supported_types = [flow.project.publishing_type]
    flow.account.tiktok = AccountTikTokConfig(post_for_me_account_id='spc_1', slots=['14:00'])
    flow.metadata.tiktok.description = FacecamMetadataService.TIKTOK_DESCRIPTION
    cover = tmp_path / 'cover.jpg'
    cover.write_bytes(b'cover')
    monkeypatch.setattr(FacecamPublicationService, 'require_ready', lambda _: cover)
    monkeypatch.setattr('app.services.upload_phase.settings.tiktok_description_suffix', '#Campaign')
    flow.create_post = MagicMock(return_value='draft-post')
    monkeypatch.setattr(PostForMeClient, 'stage_media', MagicMock(return_value='https://pfm.example/video'))
    monkeypatch.setattr(PostForMeClient, 'create_post', flow.create_post)
    monkeypatch.setattr(PostForMeClient, 'poll_outcome', MagicMock(return_value=PfmPublishOutcome(success=True)))
    return flow


@pytest.mark.parametrize('connector', ['tiktok', 'tiktok_business'])
@pytest.mark.parametrize('immediate', [False, True])
def test_facecam_sends_draft_now_but_retains_manual_reminder_slot(facecam, connector, immediate):
    facecam.account.tiktok.post_for_me_platform = connector
    chosen_time = FUTURE + timedelta(hours=5)
    result = _execute(facecam, immediate_platforms=['tiktok'] if immediate else [],
                      reserved_slots={'tiktok': (chosen_time, chosen_time)})
    body = facecam.create_post.call_args.args[0]
    assert body['platform_configurations'][connector]['is_draft'] is True
    assert 'scheduled_at' not in body and 'isDraft' not in body
    assert body['caption'] == 'Recommendation anime #animerecommendations #Campaign'
    state = facecam.project.tiktok_pfm
    assert state.is_draft and state.scheduled_at is None
    assert state.stage == ('draft_uploaded' if immediate else 'post_created')
    assert state.url is None
    job = facecam.create_job.call_args.kwargs
    assert job['tiktok_manual'] is True and job['tiktok'] is None
    if not immediate:
        assert job['platform_scheduled_at']['tiktok'] == chosen_time
    row = next(r for r in result['platform_results'] if r['platform'] == 'tiktok')
    assert row['status'] == 'pending' and row['url'] is None
    assert facecam.project.upload_last_result['tiktok_reminder_registered'] is True
    # Retrying after a transport/reminder problem reuses the existing TikTok draft.
    _execute(facecam)
    facecam.create_post.assert_called_once()


def test_draft_builder_never_schedules_even_if_given_a_date():
    body = build_post_body(social_account_id='spc', media_url='https://example.org/video',
                           caption='caption', scheduled_at=FUTURE, is_draft=True)
    assert 'scheduled_at' not in body
    assert body['platform_configurations']['tiktok']['is_draft'] is True


def test_facecam_rejects_anime_only_account_before_upload(facecam):
    from app.publishing_types import PublishingType
    facecam.account.supported_types = [PublishingType.ANIME]
    with pytest.raises(ValueError, match='supported types'):
        _execute(facecam)
    facecam.share.assert_not_called()
    facecam.create_post.assert_not_called()


def test_facecam_never_falls_back_to_global_credentials(facecam, monkeypatch):
    from app.services.upload_phase import UploadPhaseService
    monkeypatch.setattr(AccountService, 'list_accounts', lambda: [])
    with pytest.raises(ValueError, match='account_id is required'):
        UploadPhaseService.execute_upload(facecam.project.id)
    facecam.share.assert_not_called()


def test_draft_reminder_failure_is_visible_and_retry_does_not_duplicate(facecam):
    facecam.create_job.return_value = None
    result = _execute(facecam)
    row = next(r for r in result['platform_results'] if r['platform'] == 'tiktok')
    assert row['status'] == 'failed' and 'Rappel Discord' in row['detail']
    assert facecam.project.upload_last_result['tiktok_reminder_registered'] is False
    facecam.create_job.return_value = {'discord_message_id':'message'}
    _execute(facecam)
    facecam.create_post.assert_called_once()
    assert facecam.project.upload_last_result['tiktok_reminder_registered'] is True


@pytest.mark.parametrize('message_created', [False, True])
def test_facecam_draft_reschedule_only_moves_reminder_and_cancel_keeps_draft(facecam, monkeypatch, message_created):
    _execute(facecam)
    if not message_created:
        facecam.project.final_upload_discord_message_id = None
    patch_slot = MagicMock(return_value=NotificationResult(status='ok'))
    monkeypatch.setattr(PlatformRescheduleService, '_patch_server_slot', patch_slot)
    update = MagicMock()
    delete = MagicMock()
    monkeypatch.setattr(PostForMeClient, 'update_post', update)
    monkeypatch.setattr(PostForMeClient, 'delete_post', delete)
    result = PlatformRescheduleService.notify(facecam.project, 'tiktok', FUTURE)
    assert result.status == 'ok'
    patch_slot.assert_called_once_with(facecam.project, 'tiktok', FUTURE)
    update.assert_not_called()
    assert PlatformRescheduleService._cancel_tiktok(facecam.project).status == 'ok'
    delete.assert_not_called()
    assert facecam.project.tiktok_pfm.stage == 'draft_cancelled'
    DiscordService.update_job_platform.assert_called_with(facecam.project.id, 'tiktok', status='skipped', detail='Annulé')


@pytest.mark.parametrize('stage', ['post_created', 'draft_uploaded', 'published'])
def test_retry_other_platforms_keeps_acknowledged_tiktok_published(facecam, stage):
    _execute(facecam)
    facecam.project.tiktok_pfm.stage = stage
    for entry in facecam.project.upload_last_result['platforms']:
        if entry['platform'] == 'tiktok':
            entry['status'] = 'uploaded'
            entry['source'] = 'vps'
    result = _execute(facecam)
    facecam.create_post.assert_called_once()
    assert next(row for row in result['platform_results'] if row['platform'] == 'tiktok')['status'] == 'uploaded'


def test_trial_reels_default_and_environment(monkeypatch):
    monkeypatch.delenv('ATR_INSTAGRAM_TRIAL_REELS', raising=False)
    assert Settings(_env_file=None).instagram_trial_reels is False
    monkeypatch.setenv('ATR_INSTAGRAM_TRIAL_REELS', 'true')
    assert Settings(_env_file=None).instagram_trial_reels is True
