import os
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
    )

    # Paths
    data_dir: Path = Path(__file__).parent.parent / "data"
    projects_dir: Path = Path(__file__).parent.parent / "data" / "projects"
    cache_dir: Path = Path(__file__).parent.parent / "data" / "cache"
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

    # Video settings
    default_fps: float = 30.0
    match_playback_max_workers: int = 4
    match_playback_max_workers_per_episode: int = 1

    # Discord webhook integration
    discord_webhook_url: str | None = None
    cep_trigger_url_template: str = "http://localhost:48653/p/{project_id}"

    # Script automation (Gemini + ElevenLabs)
    script_automate_enabled: bool = True
    # When False, metadata and video overlay generation are skipped entirely during Automate.
    automate_metadata_overlay_enabled: bool = False
    # When True, "grand" mode: uses White border 10px mogrt and V3 scale 75%.
    # When False (default), uses White border 5px and V3 scale 68%.
    grand_mode_enabled: bool = False
    scenes_skip_ui_enabled: bool = False
    transcription_full_auto_enabled: bool = False
    gaps_full_auto_enabled: bool = False
    processing_gdrive_full_auto_enabled: bool = False
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-pro-preview"
    gemini_timeout: int = 300  # seconds (read timeout for Gemini API; connect=10)
    elevenlabs_api_key: str | None = None
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_output_format: str = "mp3_44100_128"
    gemini_light_model: str = "gemini-2.5-flash"

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

    # n8n webhook for deferred Instagram publishing at scheduled time
    n8n_webhook_url: str | None = None

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

    @field_validator("cep_trigger_url_template")
    @classmethod
    def _validate_cep_trigger_url_template(cls, value: str) -> str:
        if "{project_id}" not in value:
            raise ValueError("ATR_CEP_TRIGGER_URL_TEMPLATE must contain '{project_id}'")
        return value

    @field_validator("ffmpeg_binary", "ffprobe_binary")
    @classmethod
    def _empty_binary_override_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

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

    @field_validator("match_playback_max_workers_per_episode")
    @classmethod
    def _clamp_match_playback_max_workers_per_episode(cls, value: int) -> int:
        return max(1, min(4, value))

settings = Settings()

# Ensure directories exist
settings.projects_dir.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)
