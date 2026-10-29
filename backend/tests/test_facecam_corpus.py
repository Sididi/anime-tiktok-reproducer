"""Corpus integrity: latest publications and branded intro exclusion."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from facecam_corpus import analysis_text, latest
import facecam_corpus


def test_latest_deduplicates_and_sorts_pins_by_publication_date():
    entries = [{'id': str(i), 'timestamp': i} for i in range(90)]
    entries = [entries[0], entries[12], *entries[::-1], entries[89]]
    result = latest(entries, 75)
    assert [e['id'] for e in result] == [str(i) for i in range(89, 14, -1)]


def test_latest_fails_instead_of_silently_using_an_incomplete_or_undated_corpus():
    with pytest.raises(ValueError):
        latest([{'id': '123', 'timestamp': 1}], 75)
    with pytest.raises(ValueError):
        latest([{'id': '123'}], 1)


@pytest.mark.parametrize('intro', ["Je te présente un nouvel anime tous les jours. ",
    "J'te présente un nouvel animé tous les jours ! ",
    "Je vous présente un nouvel animé tous les jours, ",
    "Je vous présente un nouvel animé, tous les jours. ",
    "Je vous présente un nouveau animé tous les jours. "])
def test_branded_opening_is_excluded_without_losing_story(intro):
    story = 'Dans cette école, les règles sont très strictes.'
    cleaned, removed = analysis_text('theomcx', intro + story)
    assert cleaned == story and removed.strip()
    assert analysis_text('adhnime', intro + story) == (intro + story, '')


def test_reference_to_brand_later_in_account_news_is_not_cut_with_story():
    text = "C'est fini. Je vous présente un nouvel animé tous les jours. Nouvelle intro !"
    assert analysis_text('theomcx', text) == (text, '')


@pytest.mark.parametrize('cached_silent', [True, False])
def test_silent_download_retries_same_publication_with_audio(tmp_path, monkeypatch, cached_silent):
    entry = {'id': '123', 'url': 'https://www.tiktok.com/@theomcx/video/123'}
    video = tmp_path / 'videos/123/video.mp4'
    video.parent.mkdir(parents=True)
    if cached_silent:
        video.write_bytes(b'silent')
    attempts = []
    def run(command, **kwargs):
        assert command[-1] == entry['url']
        attempts.append(command[command.index('-f') + 1])
        video.write_bytes(b'audio' if attempts[-1] == 'download' else b'silent')
    def probe(path):
        if path.read_bytes() != b'audio':
            raise ValueError('Missing audio')
    monkeypatch.setattr(facecam_corpus.subprocess, 'run', run)
    monkeypatch.setattr(facecam_corpus, 'probe', probe)
    assert facecam_corpus.download(tmp_path, entry) == video
    assert len(attempts) == 2 and attempts[-1] == 'download'
