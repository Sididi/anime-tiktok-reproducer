from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.matching import _dedupe_episode_options


def test_episode_options_collapse_extension_variants_to_extensionless_value():
    episodes = _dedupe_episode_options(
        [
            "/library/S-Rank/[Judas] S-Rank Musume - S01E01.mp4",
            "[Judas] S-Rank Musume - S01E01",
            "[Judas] S-Rank Musume - S01E02.mkv",
            "[Judas] S-Rank Musume - S01E02",
        ]
    )

    assert episodes == [
        "[Judas] S-Rank Musume - S01E01",
        "[Judas] S-Rank Musume - S01E02",
    ]


def test_episode_options_ignore_empty_values():
    assert _dedupe_episode_options(["", "  ", "Episode 01.mp4"]) == ["Episode 01"]
