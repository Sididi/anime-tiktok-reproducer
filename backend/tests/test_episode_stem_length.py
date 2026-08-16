"""Import-time filename-length guard.

ext4 refuses any single path component over 255 bytes. Episode stems embedding a
full light-novel title (~200+ chars) used to pass normalization untouched, then
fail much later — sidecar suffixes pushed the derived names over the limit and
whatever touched the path first died with a raw ``OSError: File name too long``
(hydration, in the case that surfaced this).
"""

from app.services.anime_library import AnimeLibraryService

LONG_TITLE_STEM = (
    "Rakudai Kenja no Gakuin Musou Nidome no Tensei S-Rank Cheat Majutsushi "
    "Boukenroku (From Overshadowed to Overpowered Second Reincarnation of a "
    "Talentless Sage) - S01E01 Le sage de la reincarnation et son epreuve "
    "ultime dans l'academie des heros [1080p][HEVC][Multi-Sub][ABCD1234]"
)


def test_long_stem_is_capped_below_name_max():
    stem = AnimeLibraryService.normalize_indexed_episode_stem(LONG_TITLE_STEM)
    assert len(stem) <= AnimeLibraryService.MAX_EPISODE_STEM_CHARS
    # The stem must leave room for every suffix the importer derives from it:
    # collision suffix (10) + the longest sidecar suffix (20).
    worst_component = f"{stem}__0123abcd.mp4.atr_source.json"
    assert len(worst_component.encode("utf-8")) <= 255
    # Truncation must not leave dangling separators.
    assert stem == stem.strip(" ._")


def test_short_stems_are_untouched_by_the_cap():
    stem = "Aho-Girl - S01E01 [1080p]"
    assert AnimeLibraryService.normalize_indexed_episode_stem(stem) == stem


def test_truncated_twins_stay_distinguishable():
    """Two long titles identical up to the cap must not collapse silently.

    The collision suffix hashes the *raw* stem, so the unique allocator keeps
    them apart even after truncation makes their normalized stems equal.
    """
    twin_a = LONG_TITLE_STEM + " part un"
    twin_b = LONG_TITLE_STEM + " part deux"
    stem_a = AnimeLibraryService.normalize_indexed_episode_stem(twin_a)
    unique_b = AnimeLibraryService.normalize_indexed_episode_stem_unique(
        twin_b, reserved_stems={stem_a}
    )
    assert unique_b != stem_a
    assert len(f"{unique_b}.mp4.atr_source.json".encode("utf-8")) <= 255
