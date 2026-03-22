import json
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.anime_library import AnimeLibraryService


class TestAnimeLibraryNormalization(TestCase):
    def test_probe_media_prefers_av_duration_over_longer_data_stream_tail(self) -> None:
        source_path = Path("/tmp/fake-episode.mp4")

        payload = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "pix_fmt": "yuv420p10le",
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                    "duration": "1430.011000",
                    "tags": {"language": "und", "handler_name": "VideoHandler"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "0/0",
                    "duration": "1430.069705",
                    "tags": {"language": "jpn", "handler_name": "SoundHandler"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "data",
                    "codec_name": "bin_data",
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "0/0",
                    "duration": "1430.440000",
                    "tags": {"language": "eng", "handler_name": "SubtitleHandler"},
                    "disposition": {"default": 0},
                },
            ],
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "1430.440000",
            },
        }

        with patch.object(
            subprocess,
            "run",
            side_effect=lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            ),
        ):
            probe = AnimeLibraryService._probe_media_sync(source_path)

        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertEqual(probe.duration, 1430.069705)
