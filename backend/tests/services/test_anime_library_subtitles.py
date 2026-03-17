import json

import pytest

from app.services.anime_library import AnimeLibraryService, SubtitleSidecarEntry


@pytest.mark.parametrize(
    ("raw_language", "title", "handler_name", "expected"),
    [
        ("por", None, None, "pt"),
        ("pt-BR", None, None, "pt"),
        ("pt_BR", None, None, "pt"),
        ("deu", None, None, "de"),
        ("ger", None, None, "de"),
        ("ita", None, None, "it"),
        ("rus", None, None, "ru"),
        ("es-MX", None, None, "es"),
        (None, "Portuguese(Brazil)", None, "pt"),
        (None, None, "German Subtitle", "de"),
        (None, "Italian", None, "it"),
        (None, None, "Russian Closed Captions", "ru"),
    ],
)
def test_normalize_stream_language_handles_extended_language_aliases(
    raw_language,
    title,
    handler_name,
    expected,
):
    assert (
        AnimeLibraryService.normalize_stream_language(
            raw_language,
            title=title,
            handler_name=handler_name,
        )
        == expected
    )


def test_select_preferred_subtitle_entry_prefers_target_language_then_english():
    entries = [
        SubtitleSidecarEntry(
            stream_index=3,
            stream_position=0,
            codec_name="ass",
            language="en",
            raw_language="eng",
            title="English [Full]",
            kind="text",
            asset_filename="subtitle_stream_00_en.srt",
        ),
        SubtitleSidecarEntry(
            stream_index=4,
            stream_position=1,
            codec_name="ass",
            language="pt",
            raw_language="por",
            title="Portuguese",
            kind="text",
            asset_filename="subtitle_stream_01_pt.srt",
        ),
    ]

    preferred_pt = AnimeLibraryService.select_preferred_subtitle_entry(
        entries,
        target_language="pt-BR",
    )
    preferred_fallback = AnimeLibraryService.select_preferred_subtitle_entry(
        entries,
        target_language="de",
    )

    assert preferred_pt is not None
    assert preferred_pt.language == "pt"
    assert preferred_fallback is not None
    assert preferred_fallback.language == "en"


def test_load_subtitle_sidecar_entries_normalizes_legacy_language_values(tmp_path):
    source_path = tmp_path / "episode.mp4"
    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(source_path)
    sidecar_dir.mkdir(parents=True)
    manifest_path = sidecar_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_path": str(source_path),
                "generated_from": str(tmp_path / "episode.mkv"),
                "subtitle_streams": [
                    {
                        "stream_index": 0,
                        "stream_position": 0,
                        "codec_name": "ass",
                        "language": None,
                        "raw_language": "por",
                        "title": "Portuguese(Brazil)",
                        "kind": "text",
                        "asset_filename": "subtitle_stream_00_und.srt",
                        "cue_manifest_filename": None,
                        "status": "ok",
                        "error": None,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    entries = AnimeLibraryService.load_subtitle_sidecar_entries(source_path)

    assert len(entries) == 1
    assert entries[0].language == "pt"
    assert entries[0].raw_language == "por"
    assert entries[0].asset_filename == "subtitle_stream_00_und.srt"
