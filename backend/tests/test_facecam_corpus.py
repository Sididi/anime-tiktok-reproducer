"""Corpus integrity: latest publications and branded intro exclusion."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from facecam_corpus import analysis_text, latest


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
