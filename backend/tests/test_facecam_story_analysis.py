"""Model proposals must become deterministic source cuts before they reach the writer."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import MatchList, SceneMatch, SceneTranscription, Transcription
from app.models.transcription import Word
from app.services.facecam_story_service import FacecamStoryService as Stories


@pytest.fixture
def source():
    scene = SceneTranscription(
        scene_index=42, text='des gobelins. Mais elle refuse.', start_time=20, end_time=30,
        words=[Word(text=w, start=20 + i, end=20.8 + i)
               for i, w in enumerate('des gobelins. Mais elle refuse.'.split())],
    )
    match = SceneMatch(scene_index=42, episode='episode', start_time=140, end_time=160,
                       confidence=1, speed_ratio=.5)
    return Transcription(language='fr', scenes=[scene]), MatchList(matches=[match])


def proposal(**clip):
    return {'stories': [{'title': 'Une mission refusée', 'premise': 'Un héros demande une mission.',
                        'ending': 'Elle refuse.', 'steps': ['La réceptionniste refuse.'], 'omitted': [],
                        'clips': [{'scene_index': 42, **clip}]}]}


@pytest.mark.parametrize('clip,text,times,episode_times,canonical', [
    ({'start_word': None, 'end_word': 2}, 'des gobelins.', (20, 21.8), (140, 143.6), (0, 2)),
    ({'start_word': 2, 'end_word': None}, 'Mais elle refuse.', (22, 30), (144, 160), (2, 5)),
    ({'end_word': 2}, 'des gobelins.', (20, 21.8), (140, 143.6), (0, 2)),
    ({'start_word': 2}, 'Mais elle refuse.', (22, 30), (144, 160), (2, 5)),
    ({'start_word': 1, 'end_word': 4}, 'gobelins. Mais elle', (21, 23.8), (142, 147.6), (1, 4)),
    ({'start_word': 0}, 'des gobelins. Mais elle refuse.', (20, 30), (140, 160), (None, None)),
    ({'end_word': 5}, 'des gobelins. Mais elle refuse.', (20, 30), (140, 160), (None, None)),
    ({'start_word': 0, 'end_word': 5}, 'des gobelins. Mais elle refuse.', (20, 30), (140, 160), (None, None)),
    ({}, 'des gobelins. Mais elle refuse.', (20, 30), (140, 160), (None, None)),
])
def test_open_bounds_resolve_identically_for_words_source_time_and_episode_time(
        source, clip, text, times, episode_times, canonical):
    reference, matches = source
    before = reference.model_dump_json(), matches.model_dump_json()
    value = proposal(**clip)
    original_proposal = deepcopy(value)
    story, = Stories.validate(value, reference, matches)
    assert (story.clips[0].start_word, story.clips[0].end_word) == canonical
    projected, selected, mapping = Stories.project(reference, matches, story)
    scene, = projected.scenes
    match, = selected.matches
    assert scene.text == text
    assert (scene.start_time, scene.end_time) == pytest.approx(times)
    assert (match.start_time, match.end_time) == pytest.approx(episode_times)
    assert match.speed_ratio == pytest.approx(.5)
    assert (mapping[0]['start_word'], mapping[0]['end_word']) == canonical
    assert (mapping[0]['source_start'], mapping[0]['source_end']) == pytest.approx(times)
    assert value == original_proposal  # Correction feedback retains the actual model response.
    assert before == (reference.model_dump_json(), matches.model_dump_json())
    assert Stories.validate({'stories': [story.model_dump()]}, reference, matches) == [story]


@pytest.mark.parametrize('clip', [
    {'end_word': 0}, {'start_word': 5}, {'start_word': -1}, {'end_word': 6},
    {'start_word': 3, 'end_word': 2}, {'start_word': 2, 'end_word': 2},
    {'start_word': True}, {'end_word': False}, {'start_word': '2'}, {'end_word': 2.0},
])
def test_invalid_bounds_are_never_guessed_or_clamped(source, clip):
    with pytest.raises(ValueError):
        Stories.validate(proposal(**clip), *source)


@pytest.mark.parametrize('clip', [{'start_word': 1}, {'end_word': 2}, {'start_word': 0, 'end_word': 5}])
@pytest.mark.parametrize('raw', [False, True])
def test_raw_or_unaligned_scenes_cannot_be_trimmed(source, clip, raw):
    reference, matches = source
    if raw:
        # Keep a narration scene so this specifically exercises the raw-cut protection.
        reference.scenes.append(reference.scenes[0].model_copy(deep=True, update={'scene_index': 60, 'is_raw': True}))
        matches.matches.append(matches.matches[0].model_copy(update={'scene_index': 60}))
        value = proposal()
        value['stories'][0]['clips'].append({'scene_index': 60, **clip})
    else:
        reference.scenes[0].words = []
        value = proposal(**clip)
    with pytest.raises(ValueError, match='plan (42|60)'):
        Stories.validate(value, reference, matches)
    for entry in value['stories'][0]['clips']:
        entry.update(start_word=None, end_word=None)
    assert Stories.validate(value, reference, matches)


def test_corrective_retry_preserves_all_stories_and_reports_the_failed_scene(source, caplog):
    calls = []
    invalid = proposal(end_word=99)
    invalid['stories'].insert(0, proposal()['stories'][0])
    def generate(prompt, **kwargs):
        payload = json.loads(prompt.splitlines()[-1])
        calls.append(payload)
        schema = kwargs['response_schema']
        assert schema['additionalProperties'] is False and kwargs['schema_name'] == 'facecam_stories'
        assert set(schema['properties']['stories']['items']['required']) == {
            'title', 'premise', 'ending', 'steps', 'omitted', 'clips'}
        assert payload['scenes'][0]['word_count'] == 5 and payload['scenes'][0]['can_trim'] is True
        if len(calls) == 1:
            return invalid
        correction = payload['correction']
        assert correction['previous_response'] == invalid
        assert 'Histoire 2' in correction['error'] and 'plan 42' in correction['error']
        assert 'end_word <= 5' in correction['error']
        fixed = deepcopy(invalid)
        fixed['stories'][1]['clips'][0]['end_word'] = 2
        return fixed
    stories = Stories.analyze(*source, anime='Example', generate=generate, context='project=test')
    assert len(calls) == 2 and len(stories) == 2
    assert [s.id for s in stories] == ['story-1', 'story-2']
    assert stories[0].clips[0].end_word is None and stories[1].clips[0].end_word == 2
    assert 'project=test' in caplog.text


def test_malformed_proposal_gets_structural_feedback(source):
    calls = []
    def generate(prompt, **kwargs):
        payload = json.loads(prompt.splitlines()[-1])
        calls.append(payload)
        if len(calls) == 1:
            return proposal(end_word='2')
        assert 'clips.0.end_word' in payload['correction']['error']
        return proposal(end_word=2)
    assert Stories.analyze(*source, anime='Example', generate=generate, context='test')
    assert len(calls) == 2


def test_exhausted_retry_never_substitutes_a_different_story_or_full_scene(source):
    calls = []
    def generate(*args, **kwargs):
        calls.append(args)
        return proposal(end_word=99)
    with pytest.raises(ValueError, match='3 tentatives.*plan 42.*projet est conservé'):
        Stories.analyze(*source, anime='Example', generate=generate, context='test')
    assert len(calls) == 3


def test_provider_failure_is_not_retried_as_an_editorial_error(source):
    calls = []
    def generate(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError, match='provider unavailable'):
        Stories.analyze(*source, anime='Example', generate=generate, context='test')
    assert len(calls) == 1
