"""Lazy exports for app.services.

Avoid importing every service eagerly at package import time. Some services
pull in heavyweight optional runtime dependencies such as OpenCV/scenedetect,
which should not be required just to import unrelated modules like
AnimeLibraryService in tests.
"""

from __future__ import annotations

import importlib


_EXPORTS = {
    "AccountService": (".account_service", "AccountService"),
    "ProjectService": (".project_service", "ProjectService"),
    "DownloaderService": (".downloader", "DownloaderService"),
    "SceneDetectorService": (".scene_detector", "SceneDetectorService"),
    "AnimeMatcherService": (".anime_matcher", "AnimeMatcherService"),
    "AnimeLibraryService": (".anime_library", "AnimeLibraryService"),
    "TranscriberService": (".transcriber", "TranscriberService"),
    "ProcessingService": (".processing", "ProcessingService"),
    "SubtitleVideoService": (".subtitle_video", "SubtitleVideoService"),
    "SubtitleFrameRenderer": (".subtitle_renderer", "SubtitleFrameRenderer"),
    "get_style": (".subtitle_styles", "get_style"),
    "list_styles": (".subtitle_styles", "list_styles"),
    "GapResolutionService": (".gap_resolution", "GapResolutionService"),
    "SceneMergerService": (".scene_merger", "SceneMergerService"),
    "MetadataService": (".metadata", "MetadataService"),
    "GoogleDriveService": (".google_drive_service", "GoogleDriveService"),
    "DiscordService": (".discord_service", "DiscordService"),
    "ExportService": (".export_service", "ExportService"),
    "SocialUploadService": (".social_upload_service", "SocialUploadService"),
    "PlatformUploadResult": (".social_upload_service", "PlatformUploadResult"),
    "SchedulingService": (".scheduling_service", "SchedulingService"),
    "UploadPhaseService": (".upload_phase", "UploadPhaseService"),
    "MetaTokenService": (".meta_token_service", "MetaTokenService"),
    "IntegrationHealthService": (".integration_health_service", "IntegrationHealthService"),
    "GeminiService": (".gemini_service", "GeminiService"),
    "ElevenLabsService": (".elevenlabs_service", "ElevenLabsService"),
    "VoiceConfigService": (".voice_config_service", "VoiceConfigService"),
    "ScriptAutomationService": (".script_automation_service", "ScriptAutomationService"),
    "ScriptPayloadService": (".script_payload_service", "ScriptPayloadService"),
    "ScriptPhasePromptService": (".script_phase_prompt_service", "ScriptPhasePromptService"),
    "MusicConfigService": (".music_config_service", "MusicConfigService"),
    "AudioSpeedService": (".audio_speed_service", "AudioSpeedService"),
    "TitleImageGeneratorService": (".title_image_generator", "TitleImageGeneratorService"),
    "PremiereSubtitleBakerService": (".premiere_subtitle_baker", "PremiereSubtitleBakerService"),
    "RawSceneDetectorService": (".raw_scene_detector", "RawSceneDetectorService"),
    "SourceChunkStreamingService": (".source_chunk_streaming_service", "SourceChunkStreamingService"),
    "indexation_queue": (".indexation_queue", "indexation_queue"),
    "IndexationQueueService": (".indexation_queue", "IndexationQueueService"),
    "TorrentLinkerService": (".torrent_linker", "TorrentLinkerService"),
    "DeferredDownloadService": (".deferred_download", "DeferredDownloadService"),
    "TikTokUrlDbService": (".tiktok_url_db_service", "TikTokUrlDbService"),
    "StorageBoxSftpClient": (".storage_box_sftp_client", "StorageBoxSftpClient"),
    "StorageBoxRepository": (".storage_box_repository", "StorageBoxRepository"),
    "LibraryHydrationService": (".library_hydration_service", "LibraryHydrationService"),
    "LibraryStateDb": (".library_state_db", "LibraryStateDb"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
