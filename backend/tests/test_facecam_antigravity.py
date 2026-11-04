"""Wider, subscription-only clip search still obeys review, source and timeline bounds."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config import settings
from app.services.antigravity_illustration_client import AntigravityIllustrationClient as Client
from app.services.facecam_antigravity_search import FacecamAntigravitySearch as Search
from app.services.facecam_episode_overview import FacecamEpisodeOverview as Overview
from app.services.facecam_targeted_search import FacecamTargetedSearch as PaidSearch
from app.services.facecam_visual_candidates import FacecamVisualCandidates as Candidates
from app.services.facecam_visual_index import FacecamVisualIndex as Index, file_identity
from app.services.facecam_window_media import FacecamWindowMedia as Media
from test_facecam_visual_candidates import context, set_timing


@pytest.fixture
def subscription(context, monkeypatch):
    monkeypatch.setattr(settings, 'facecam_illustration_provider', 'antigravity')
    monkeypatch.setattr(PaidSearch, 'select', lambda *a, **kw: pytest.fail('No paid fallback'))
    calls, samples = [], []
    decode = Media.window
    def three_frames(info, start, end, check, *, sample_count):
        assert sample_count == 3
        samples.append(start)
        return decode(info, start, end, check)
    monkeypatch.setattr(Media, 'window', three_frames)
    sheets = Media.contact_sheets
    monkeypatch.setattr(Media, 'contact_sheets', lambda shots, **kw: sheets(shots))
    monkeypatch.setattr(Overview, 'describe', lambda *a: ([{'window_id':'e0w2','visual':'Un dragon court.','usable':True}],
                                                         {'sampled_frames':1,'new_observations':1,'cached_observations':0}))
    def call(self, name, prompt, schema, **kw):
        calls.append(name)
        data = json.loads(prompt.splitlines()[-1])
        if name == 'locations':
            assert data['visual_overview']
            return {'scenes':[{'scene_index':s['scene_index'],'window_ids':['e0w2','e0w0']} for s in data['scenes']]}
        assert kw['images']
        return {'scenes':[{'scene_index':s['scene_index'],'alternatives':[
            {'shot_id':i,'reason':'Le dragon court dans la forêt.'} for i in s['shot_ids'][:s['slots']]]} for s in data['scenes']]}
    monkeypatch.setattr(Client, 'call', call)
    return context, calls, samples


def test_wider_retrieval_is_verified_cached_and_native_speed(subscription):
    context, calls, samples = subscription
    result = Candidates.select('visual')
    assert calls == ['locations','vision-000'] and 24 in samples
    assert result and result[0]['start'] >= 24
    assert all(m.speed_ratio == 1 for m in Candidates.matches(result))
    assert Candidates.select('visual') == result and len(calls) == 2
    report = json.loads(next((context[4].parent/'project').rglob('report.json')).read_text())
    assert report['strategy'] == 'antigravity' and report['credit_overages'] is False
    assert report['deadline_seconds'] < 600 and report['episodes'] == 1


def test_failed_vision_reuses_successful_localization_on_retry(subscription, monkeypatch):
    _, calls, _ = subscription
    original = Client.call
    first = [True]
    def fail_once(self, name, *args, **kw):
        if name.startswith('vision') and first[0]:
            first[0] = False
            raise ValueError('Quota exhausted')
        return original(self, name, *args, **kw)
    monkeypatch.setattr(Client, 'call', fail_once)
    with pytest.raises(ValueError, match='Quota exhausted'):
        Candidates.select('visual')
    assert Candidates.select('visual')
    assert calls == ['locations','vision-000']


def test_review_is_required_even_for_subscription_search(subscription):
    context, calls, _ = subscription
    context[0].recording['reviewed_revision'] = None
    with pytest.raises(ValueError, match='relecture'):
        Candidates.select('visual')
    assert calls == []


def test_invented_location_is_rejected_before_decoding_or_vision(subscription, monkeypatch):
    context, calls, samples = subscription
    monkeypatch.setattr(Client, 'call', lambda *a, **kw: {'scenes':[{'scene_index':0,'window_ids':['e9w999']} ]})
    with pytest.raises(ValueError, match='fenêtre inconnue'):
        Candidates.select('visual')
    assert not samples and not list((context[4].parent/'project').rglob('locations.json'))


def test_provider_deadline_covers_all_episodes_and_stages(subscription, monkeypatch):
    context, calls, _ = subscription
    clock = [10.]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    original = Client.call
    def slow(self, *a, **kw):
        value = original(self, *a, **kw)
        clock[0] += settings.facecam_antigravity_timeout_seconds + 1
        return value
    monkeypatch.setattr(Client, 'call', slow)
    with pytest.raises(TimeoutError, match='global'):
        Candidates.select('visual')
    assert calls == ['locations']


def test_waiting_for_another_search_counts_toward_the_same_deadline(subscription, monkeypatch):
    _, calls, _ = subscription
    monkeypatch.setattr(settings, 'facecam_antigravity_timeout_seconds', .2)
    started = time.monotonic()
    with Index.lock('selection:visual'), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Candidates.select, 'visual')
        with pytest.raises(TimeoutError, match='global'):
            future.result(timeout=2)
    assert time.monotonic() - started < 1 and not calls


@pytest.mark.parametrize('changed', ['reviewed_revision', 'approved_revision'])
def test_review_changes_during_request_discard_the_result(subscription, monkeypatch, changed):
    context, calls, _ = subscription
    original = Client.call
    def stale(self, *a, **kw):
        result = original(self, *a, **kw)
        if changed == 'reviewed_revision':
            context[0].recording[changed] = None
        else:
            context[0].approved_revision = None
        return result
    monkeypatch.setattr(Client, 'call', stale)
    with pytest.raises(ValueError, match='relecture'):
        Candidates.select('visual')
    assert calls == ['locations'] and not list((context[4].parent/'project').rglob('candidates.json'))


def test_retry_preserves_completed_batches_and_scene_order(subscription, monkeypatch):
    from collections import Counter
    context, calls, _ = subscription
    state = context[0]
    state.assignment = [{'scene_index': i, 'text': 'Le dragon court.'} for i in range(17)]
    state.montage_map = [{'output_scene_index': i, 'source_scene_index': 4, 'kind': 'body'} for i in range(17)]
    set_timing(state, [(i * 2, (i + 1) * 2, False) for i in range(17)])
    original = Client.call
    failed = []

    def fail_middle(self, name, *args, **kwargs):
        if name == 'vision-008' and not failed:
            failed.append(name)
            raise ValueError('Temporary provider failure')
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(Client, 'call', fail_middle)
    with pytest.raises(ValueError, match='Temporary provider failure'):
        Candidates.select('visual')
    result = Candidates.select('visual')
    assert Counter(calls) == Counter({'locations': 1, 'vision-000': 1, 'vision-008': 1, 'vision-016': 1})
    assert list(dict.fromkeys(s['output_scene_index'] for s in result)) == list(range(17))
    assert len({s['match_index'] for s in result}) == len(result)


def test_placement_targets_follow_the_selected_search_capacity(subscription):
    context, _, _ = subscription
    state = context[0]
    state.assignment = [{'scene_index':i,'text':'Le dragon court.'} for i in range(50)]
    state.montage_map = [{'output_scene_index':i,'source_scene_index':4,'kind':'body'} for i in range(50)]
    set_timing(state, [(i*2,(i+1)*2,False) for i in range(50)])
    assert len(Candidates.targets(state)[0]['scenes']) == 50
    assert len(PaidSearch.targets(state)[0]['scenes']) == 32


def test_episode_overview_balances_all_episodes_and_reuses_descriptions(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(settings, 'cache_dir', tmp_path/'cache')
    infos = []
    for i in range(5):
        path = tmp_path/f'episode{i}.mp4'; path.write_bytes(f'episode{i}'.encode())
        infos.append({'source':str(path),'identity':file_identity(path),'fps':24,'duration':1440,'frame_count':34560})
    subtitles = [{'language':'fr','cues':[]} for _ in infos]
    samples = Overview.samples(infos)
    assert len(samples) == 96 and {s['episode_index'] for s in samples} == set(range(5))
    for i in range(5):
        seconds = [s['seconds'] for s in samples if s['episode_index'] == i]
        assert min(seconds) < 12 and max(seconds) > 1400
    def extract(info, samples, check):
        for s in samples:
            path=Path(s['image']); path.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(352,198),'blue').save(path)
    monkeypatch.setattr(Overview, 'extract', extract)
    calls = []
    class FakeClient:
        def call(self, name, prompt, schema, **kw):
            calls.append(name)
            data = json.loads(prompt.splitlines()[-1])
            return {'observations':[{'sample_id':s['sample_id'],'visual':'Une forêt enneigée.','usable':True} for s in data['samples']]}
    first, stats = Overview.describe(infos, subtitles, FakeClient(), lambda: None)
    assert stats['new_observations'] == 96 and len(calls) == 2
    second, stats = Overview.describe(infos, subtitles, FakeClient(), lambda: None)
    assert second == first and stats['cached_observations'] == 96 and len(calls) == 2
    subtitles[0]['cues'] = [{'start':0,'end':10,'text':'Le nom correct du personnage.'}]
    _, stats = Overview.describe(infos, subtitles, FakeClient(), lambda: None)
    assert 0 < stats['new_observations'] < 40 and len(calls) == 3


def test_real_overview_frames_and_three_frame_shots_are_bound_to_local_24fps(tmp_path, monkeypatch):
    import cv2
    import av
    import numpy as np
    from PIL import Image
    monkeypatch.setattr(settings, 'cache_dir', tmp_path/'cache')
    source=tmp_path/'colors.mp4'
    writer=cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'mp4v'),24,(160,90))
    assert writer.isOpened()
    for second in range(24):
        for _ in range(24):
            writer.write(np.full((90,160,3), (0,0,255) if second < 12 else (255,0,0), dtype=np.uint8))
    writer.release()
    info=Media.probe(source); samples=Overview.samples([info])
    Overview.extract(info, samples, lambda: None)
    assert [s['frame'] for s in samples] == [144,432]
    with Image.open(samples[0]['image']) as im:
        assert im.getpixel((80,45))[0] > 230
    with Image.open(samples[1]['image']) as im:
        assert im.getpixel((80,45))[2] > 230
    shots=Media.window(info,12,16,lambda:None,sample_count=3)
    assert shots and all(len(s['samples']) == 3 and s['in_frame'] >= 288 for s in shots)
    monkeypatch.setattr(av, 'open', lambda *a,**kw:pytest.fail('Cached images must avoid another decode'))
    Overview.extract(info,samples,lambda:None)
    assert Media.window(info,12,16,lambda:None,sample_count=3) == shots
