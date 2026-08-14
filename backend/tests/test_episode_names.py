from app.services.episode_names import (
    canonical_episode_stem,
    normalize_episode_whitelist,
    strip_known_media_extension,
)


def test_strip_known_media_extension():
    assert strip_known_media_extension("S01E01.mkv") == "S01E01"
    assert strip_known_media_extension("S01E01.MKV") == "S01E01"
    assert strip_known_media_extension("S01E01.txt") == "S01E01.txt"
    assert strip_known_media_extension("") == ""


def test_canonical_episode_stem_reduces_all_shapes_to_one_value():
    assert canonical_episode_stem("Show/S01E01.mkv") == "S01E01"
    assert canonical_episode_stem("/abs/path/Show/S01E01.mp4") == "S01E01"
    assert canonical_episode_stem("dir\\S01E01.mkv") == "S01E01"
    assert canonical_episode_stem("S01E01") == "S01E01"
    assert canonical_episode_stem("  S01E01.mkv  ") == "S01E01"
    assert canonical_episode_stem("") == ""
    assert canonical_episode_stem(None) == ""  # type: ignore[arg-type]


def test_normalize_episode_whitelist():
    assert normalize_episode_whitelist(None) is None
    assert normalize_episode_whitelist([]) is None
    assert normalize_episode_whitelist(["", "  "]) is None
    assert normalize_episode_whitelist(["a.mkv", "dir/b.mp4", "a"]) == frozenset(
        {"a", "b"}
    )


def test_find_matches_request_accepts_optional_episode_subset():
    from app.api.routes.matching import FindMatchesRequest

    default = FindMatchesRequest()
    assert default.episodes is None

    explicit = FindMatchesRequest(episodes=["E01.mkv", "E02"])
    assert explicit.episodes == ["E01.mkv", "E02"]
