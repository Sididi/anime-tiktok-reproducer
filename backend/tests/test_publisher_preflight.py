"""Incomplete direct-publisher setup fails before jobs or reservations exist."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project
from app.services.account_service import (
    ADDITIONAL_PLATFORMS,
    AccountPublisherConfig,
    AccountService,
    validate_account_publishers,
    validate_publisher_config,
)
from app.services.project_service import ProjectService
from app.services.project_upload_service import ProjectUploadService
from app.services.scheduling_service import SchedulingService
from app.services.upload_phase import UploadPhaseService


def configured_account(platform: str, mode="api", **credentials):
    return AccountService._parse_account("account", {
        "name": "Account", "language": "fr", "slots": ["14:00"],
        platform: {"mode": mode, **credentials},
    })


def bilibili_credentials(**overrides):
    return {
        "client_id": "app", "client_secret": "secret", "access_token": "token",
        "openid": "creator", "expires_at": time.time() + 86400,
        "category_id": 1, "copyright": 1, **overrides,
    }


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_manual_mode_needs_no_oauth_values(platform):
    validate_publisher_config(platform, AccountPublisherConfig(mode="manual"))
    validate_account_publishers(configured_account(platform, mode="manual"))


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_incomplete_api_mode_has_actionable_secret_free_error(platform):
    config = AccountPublisherConfig(mode="api", credentials={"client_secret": "do-not-print"})
    with pytest.raises(ValueError) as exc:
        validate_publisher_config(platform, config)
    assert "client_id" in str(exc.value)
    assert f"docs/setup-{platform}.md" in str(exc.value)
    assert "mode: manual" in str(exc.value)
    assert "do-not-print" not in str(exc.value)


@pytest.mark.parametrize("platform,credentials", [
    ("dailymotion", {"client_id": "app", "client_secret": "secret", "profile_id": "profile"}),
    ("snapchat", {
        "client_id": "app", "client_secret": "secret", "profile_id": "profile",
        "refresh_token": "refresh",
    }),
    ("bilibili", bilibili_credentials()),
])
def test_complete_api_configuration_is_accepted(platform, credentials):
    validate_account_publishers(configured_account(platform, **credentials))


@pytest.mark.parametrize("expires_at", [None, 0, "bad", float("nan"), float("inf"), 1])
def test_bilibili_missing_invalid_or_expired_token_is_rejected(expires_at):
    config = AccountPublisherConfig(mode="api", credentials=bilibili_credentials(expires_at=expires_at))
    with pytest.raises(ValueError, match="expires_at"):
        validate_publisher_config("bilibili", config)


@pytest.mark.parametrize("field,value", [
    ("category_id", 0), ("category_id", True), ("category_id", "1.5"),
    ("copyright", 0), ("copyright", 3), ("copyright", False),
])
def test_bilibili_requires_valid_category_and_originality(field, value):
    config = AccountPublisherConfig(mode="api", credentials=bilibili_credentials(**{field: value}))
    with pytest.raises(ValueError, match=field):
        validate_publisher_config("bilibili", config)


def test_bilibili_repost_requires_source():
    config = AccountPublisherConfig(mode="api", credentials=bilibili_credentials(copyright=2))
    with pytest.raises(ValueError, match="source"):
        validate_publisher_config("bilibili", config)
    config.credentials["source"] = "https://original.example/video"
    validate_publisher_config("bilibili", config)


def test_dailymotion_rejects_quoted_boolean():
    account = configured_account("dailymotion", client_id="app", client_secret="secret",
                                 profile_id="profile", is_for_kids="false")
    with pytest.raises(ValueError, match="YAML boolean"):
        validate_account_publishers(account)


def test_broken_opt_in_does_not_hide_existing_account_or_block_other_platforms():
    account = configured_account("dailymotion")
    assert account.id == "account"
    validate_account_publishers(account, ["youtube"])
    account.dailymotion.slots = []
    validate_account_publishers(account)
    with pytest.raises(ValueError, match="Dailymotion"):
        validate_account_publishers(account, ["dailymotion"])


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
async def test_queue_rejects_missing_api_config_before_persistence_or_reservation(
    monkeypatch, tmp_path, platform,
):
    account = configured_account(platform)
    monkeypatch.setattr(AccountService, "get_account", lambda _: account)
    monkeypatch.setattr(ProjectService, "aload", AsyncMock(return_value=Project(id="p1")))
    reserve = Mock(side_effect=AssertionError("must not reserve"))
    monkeypatch.setattr(SchedulingService, "reserve_all_platform_slots", reserve)
    queue = ProjectUploadService(jobs_path=tmp_path / "jobs.json")
    persist = AsyncMock()
    monkeypatch.setattr(queue, "_publish_job", persist)
    with pytest.raises(ValueError, match="API configuration is incomplete"):
        await queue.enqueue_upload(project_id="p1", account_id="account", platforms=[platform])
    persist.assert_not_awaited()
    reserve.assert_not_called()
    assert not queue._tasks
    assert not queue._requests


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_direct_execute_rechecks_config_before_readiness_or_network(monkeypatch, platform):
    account = configured_account(platform)
    monkeypatch.setattr(AccountService, "get_account", lambda _: account)
    monkeypatch.setattr(AccountService, "list_accounts", lambda: [account])
    monkeypatch.setattr(ProjectService, "load", lambda _: Project(id="p1"))
    readiness = Mock(side_effect=AssertionError("must fail before source inspection"))
    monkeypatch.setattr(UploadPhaseService, "compute_readiness", readiness)
    with pytest.raises(ValueError, match="API configuration is incomplete"):
        UploadPhaseService.execute_upload("p1", account_id="account", platforms=[platform])
    readiness.assert_not_called()
