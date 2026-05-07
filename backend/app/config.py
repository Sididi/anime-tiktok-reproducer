import os
from fractions import Fraction
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROCESS_START_ENV = dict(os.environ)


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_prefix="ATR_",
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    data_dir: Path = Path(__file__).parent.parent / "data"
    projects_dir: Path = Path(__file__).parent.parent / "data" / "projects"
    cache_dir: Path = Path(__file__).parent.parent / "data" / "cache"
    library_state_db_path: Path = Path(__file__).parent.parent / "data" / "library_state.db"
    ffmpeg_binary: str | None = None
    ffprobe_binary: str | None = None

    # anime_searcher
    anime_searcher_path: Path = Path(__file__).parent.parent.parent / "modules" / "anime_searcher"
    anime_library_path: Path = Path(__file__).parent.parent.parent / "modules" / "anime_searcher" / "library"
    sscd_model_path: Path | None = None  # User should set this

    # Accounts
    accounts_config_path: Path = PROJECT_ROOT / "config" / "accounts" / "config.yaml"
    voices_config_path: Path = PROJECT_ROOT / "config" / "voices" / "config.yaml"
    music_config_path: Path = PROJECT_ROOT / "config" / "music" / "config.yaml"

    # CORS
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    integration_startup_health_check_enabled: bool = True

    # Video settings
    default_fps: float = 30.0
    source_normalization_profile: str = "h264_mp4_aac"
    match_playback_max_workers: int = 4
    match_playback_max_workers_per_episode: int = 1
    min_playback_speed_factor: float = 0.75

    # TikTok server (VPS) integration — replaces previous Discord webhook
    tiktok_server_base_url: str | None = None
    # Internal API base for /api/internal/jobs/* (planning/reschedule path).
    # May equal tiktok_server_base_url; kept distinct so deployments can route
    # the planning endpoints separately (e.g. behind a different ingress).
    tiktok_server_url: str | None = None
    tiktok_server_internal_token: str | None = None

    cep_trigger_url_template: str = "http://localhost:48653/p/{project_id}"

    # HuggingFace (pyannote diarization for raw scene detection)
    hf_token: str | None = None

    # Script automation (Gemini + ElevenLabs)
    script_automate_enabled: bool = True
    # When False, metadata and video overlay generation are skipped entirely during Automate.
    automate_metadata_overlay_enabled: bool = False
    # When True, /script title generation returns multiple choices and the UI asks
    # the user to pick one instead of auto-selecting the first suggestion.
    script_title_selection_enabled: bool = False
    # When True, /script video overlay "title" is pre-filled statically based on the
    # project library type (no LLM call, no 8-proposition modal).
    static_overlay_title_enabled: bool = False
    scenes_skip_ui_enabled: bool = False
    transcription_full_auto_enabled: bool = False
    gaps_full_auto_enabled: bool = False
    matches_full_auto_enabled: bool = False
    processing_gdrive_full_auto_enabled: bool = False
    elevenlabs_api_key: str | None = None
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_output_format: str = "pcm_44100"

    # OpenRouter (replaces per-provider keys)
    openrouter_api_key: str | None = None
    openrouter_timeout: int = 600  # seconds; generous for thinking models
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Config paths for new feature configs
    llm_config_path: Path = PROJECT_ROOT / "config" / "llm" / "config.yaml"
    templates_config_path: Path = PROJECT_ROOT / "config" / "templates" / "config.yaml"

    # Google OAuth shared credentials
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    google_token_uri: str = "https://oauth2.googleapis.com/token"

    # Google OAuth split refresh tokens (optional; fallback to google_refresh_token)
    google_drive_refresh_token: str | None = None
    google_youtube_refresh_token: str | None = None

    # Google Drive config
    google_drive_parent_folder_id: str | None = None
    drive_upload_max_parallel: int = 4
    drive_delete_max_parallel: int = 8
    drive_upload_chunk_mb: int = 16

    # YouTube upload defaults
    youtube_category_id: str = "22"
    youtube_channel_id: str | None = None
    social_upload_max_parallel: int = 3
    social_upload_http_timeout_seconds: int = 120
    social_upload_binary_timeout_seconds: int = 900
    social_upload_platform_timeout_seconds: int = 1200
    project_upload_max_concurrent: int = 3
    project_manager_platform_phase_timeout_seconds: int = 1260

    # Meta Graph API
    meta_graph_api_version: str = "v25.0"
    # Token strategy:
    # - "system_user": use pre-generated system user/page tokens from env
    # - "long_lived_user": auto-refresh user token and derive page token on server
    meta_token_mode: str = "system_user"
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_user_access_token: str | None = None
    meta_user_access_token_expires_at: str | None = None
    meta_user_token_refresh_lead_seconds: int = 7 * 24 * 3600
    facebook_page_id: str | None = None
    facebook_page_access_token: str | None = None
    instagram_business_account_id: str | None = None
    instagram_access_token: str | None = None
    instagram_publish_poll_interval_seconds: int = 5
    instagram_publish_timeout_seconds: int = 15 * 60

    # qBittorrent
    qbittorrent_url: str = "http://localhost:8080"
    qbittorrent_username: str = "admin"
    qbittorrent_password: str = "adminadmin"
    torrent_complete_dir: Path = Path.home() / "Torrents" / ".complete"

    # Hetzner Storage Box
    storage_box_enabled: bool = False
    storage_box_host: str | None = None
    storage_box_port: int = 22
    storage_box_username: str | None = None
    storage_box_ssh_key_path: Path | None = None
    storage_box_password: str | None = None
    storage_box_root: str = ""
    storage_box_known_hosts_path: Path | None = None
    storage_box_max_connections: int = 8
    storage_box_upload_max_parallel: int = 6
    storage_box_download_max_parallel: int = 6
    storage_box_transfer_mode: str = "auto"
    storage_box_rsync_min_file_size_mb: int = 4
    storage_box_rsync_timeout_seconds: int = 7200
    # Retry settings for transient SFTP/network errors (VPN flaps, NAT timeouts,
    # brief Hetzner reachability dips). Per-operation retry — the retried
    # operation re-acquires a fresh SSH session, so a half-dead pooled
    # connection is replaced.
    storage_box_retry_max_attempts: int = 5
    storage_box_retry_base_delay_seconds: float = 1.0
    storage_box_retry_max_delay_seconds: float = 30.0
    # lftp settings (explicit lftp mode and directory mirror flows)
    storage_box_lftp_segments: int = 4
    storage_box_lftp_min_file_size_mb: int = 50

    @property
    def drive_google_client_id(self) -> str | None:
        return self.google_client_id

    @property
    def drive_google_client_secret(self) -> str | None:
        return self.google_client_secret

    @property
    def drive_google_refresh_token(self) -> str | None:
        return self.google_drive_refresh_token or self.google_refresh_token

    @property
    def drive_google_token_uri(self) -> str:
        return self.google_token_uri

    @property
    def youtube_google_client_id(self) -> str | None:
        return self.google_client_id

    @property
    def youtube_google_client_secret(self) -> str | None:
        return self.google_client_secret

    @property
    def youtube_google_refresh_token(self) -> str | None:
        return self.google_youtube_refresh_token or self.google_refresh_token

    @property
    def youtube_google_token_uri(self) -> str:
        return self.google_token_uri

    @property
    def min_playback_speed_fraction(self) -> Fraction:
        return Fraction(str(self.min_playback_speed_factor)).limit_denominator(100000)

    @property
    def matcher_min_speed_fraction(self) -> Fraction:
        return self.min_playback_speed_fraction - Fraction(1, 10)

    @property
    def matcher_min_speed_factor(self) -> float:
        return float(self.matcher_min_speed_fraction)

    @field_validator("cep_trigger_url_template")
    @classmethod
    def _validate_cep_trigger_url_template(cls, value: str) -> str:
        if "{project_id}" not in value:
            raise ValueError("ATR_CEP_TRIGGER_URL_TEMPLATE must contain '{project_id}'")
        return value

    @field_validator("min_playback_speed_factor")
    @classmethod
    def _validate_min_playback_speed_factor(cls, value: float) -> float:
        if value <= 0.10 or value > 1.0:
            raise ValueError(
                "ATR_MIN_PLAYBACK_SPEED_FACTOR must be greater than 0.10 and at most 1.0"
            )
        return value

    @field_validator("ffmpeg_binary", "ffprobe_binary")
    @classmethod
    def _empty_binary_override_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("storage_box_host", "storage_box_username", "storage_box_root")
    @classmethod
    def _trim_storage_box_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("storage_box_transfer_mode")
    @classmethod
    def _normalize_storage_box_transfer_mode(cls, value: str) -> str:
        normalized = str(value or "").strip().lower() or "auto"
        if normalized not in {"auto", "sftp", "rsync", "lftp"}:
            raise ValueError("ATR_STORAGE_BOX_TRANSFER_MODE must be one of auto, sftp, rsync, lftp")
        return normalized

    @field_validator("library_state_db_path")
    @classmethod
    def _resolve_library_state_db_path(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if expanded.is_absolute():
            return expanded
        return (PROJECT_ROOT / expanded).resolve()

    @field_validator("drive_upload_max_parallel")
    @classmethod
    def _clamp_drive_upload_max_parallel(cls, value: int) -> int:
        return max(1, min(16, value))

    @field_validator("drive_delete_max_parallel")
    @classmethod
    def _clamp_drive_delete_max_parallel(cls, value: int) -> int:
        return max(1, min(32, value))

    @field_validator("drive_upload_chunk_mb")
    @classmethod
    def _clamp_drive_upload_chunk_mb(cls, value: int) -> int:
        return max(4, min(64, value))

    @field_validator("match_playback_max_workers")
    @classmethod
    def _clamp_match_playback_max_workers(cls, value: int) -> int:
        return max(1, min(8, value))

    @field_validator("project_upload_max_concurrent")
    @classmethod
    def _clamp_project_upload_max_concurrent(cls, value: int) -> int:
        return max(1, min(8, value))

    @field_validator(
        "social_upload_http_timeout_seconds",
        "social_upload_binary_timeout_seconds",
        "social_upload_platform_timeout_seconds",
        "project_manager_platform_phase_timeout_seconds",
    )
    @classmethod
    def _clamp_social_upload_timeouts(cls, value: int) -> int:
        return max(1, min(24 * 3600, value))

    @field_validator("storage_box_max_connections")
    @classmethod
    def _clamp_storage_box_max_connections(cls, value: int) -> int:
        return max(1, min(16, value))

    @field_validator("storage_box_upload_max_parallel", "storage_box_download_max_parallel")
    @classmethod
    def _clamp_storage_box_parallelism(cls, value: int) -> int:
        return max(1, min(16, value))

    @field_validator("storage_box_rsync_min_file_size_mb")
    @classmethod
    def _clamp_storage_box_rsync_min_file_size_mb(cls, value: int) -> int:
        return max(1, min(1024, value))

    @field_validator("storage_box_retry_max_attempts")
    @classmethod
    def _clamp_storage_box_retry_max_attempts(cls, value: int) -> int:
        return max(1, min(20, value))

    @field_validator("storage_box_retry_base_delay_seconds")
    @classmethod
    def _clamp_storage_box_retry_base_delay_seconds(cls, value: float) -> float:
        return max(0.1, min(60.0, value))

    @field_validator("storage_box_retry_max_delay_seconds")
    @classmethod
    def _clamp_storage_box_retry_max_delay_seconds(cls, value: float) -> float:
        return max(1.0, min(600.0, value))

    @field_validator("storage_box_rsync_timeout_seconds")
    @classmethod
    def _clamp_storage_box_rsync_timeout_seconds(cls, value: int) -> int:
        return max(30, min(24 * 3600, value))

    @field_validator("match_playback_max_workers_per_episode")
    @classmethod
    def _clamp_match_playback_max_workers_per_episode(cls, value: int) -> int:
        return max(1, min(4, value))

settings = Settings()

# Warn about legacy env vars that are now ignored.
import logging as _logging

_legacy_env_keys = (
    "ATR_LLM_PROVIDER",
    "ATR_GEMINI_API_KEY",
    "ATR_GEMINI_MODEL",
    "ATR_GEMINI_LIGHT_MODEL",
    "ATR_GEMINI_TIMEOUT",
    "ATR_ANTHROPIC_API_KEY",
    "ATR_ANTHROPIC_MODEL",
    "ATR_ANTHROPIC_LIGHT_MODEL",
    "ATR_ANTHROPIC_TIMEOUT",
    "ATR_GRAND_MODE_ENABLED",
)
_logger = _logging.getLogger("app.config")
for _key in _legacy_env_keys:
    if _key in os.environ:
        _logger.warning(
            "%s is set but ignored. Configure LLM models via "
            "config/llm/config.yaml and templates via "
            "config/templates/config.yaml. Use ATR_OPENROUTER_API_KEY for "
            "the API key.",
            _key,
        )

# Ensure directories exist
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.projects_dir.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)
settings.library_state_db_path.parent.mkdir(parents=True, exist_ok=True)
