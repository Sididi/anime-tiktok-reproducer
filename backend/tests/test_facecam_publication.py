from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from test_facecam import source, draft  # noqa: F401
from facecam_workflow_helpers import publication_candidates, complete_publication
from app.services.facecam_service import FacecamService, StaleFacecamRevision
from app.services.facecam_publication_service import FacecamPublicationService as Publication
from app.services.facecam_thumbnail_service import FacecamThumbnailService as Thumbnails, portrait, quality
from app.services.metadata import MetadataService
from app.services.llm_service import LLMService
from app.services.project_service import ProjectService
from app.services.anime_library import AnimeLibraryService


@pytest.fixture
def project(source):
    project, state = draft(source)
    FacecamService.approve(project.id, state.revision)
    return project


@pytest.fixture
def client():
    from app.api.routes.facecam import router
    from app.api.routes.processing import router as processing_router
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.include_router(processing_router, prefix='/api')
    with TestClient(app) as client:
        yield client


def generate(project, monkeypatch):
    monkeypatch.setattr(LLMService, 'generate_json', lambda *a, **kw: publication_candidates())
    Publication.generate(project.id, FacecamService.load(project.id).revision)
    return Publication.status(project.id)


def test_same_prompt_schema_tier_and_explicit_title_choice(project, monkeypatch):
    def llm(prompt, **kwargs):
        assert prompt == MetadataService.build_prompt_from_script_payload('Naruto', Publication.script(project.id), 'fr')
        assert 'Dialogue original.' in prompt and 'Naruto poursuit son rêve.' in prompt
        assert kwargs == {'preset_key': 'gpt', 'tier': 'light', 'response_schema': MetadataService.candidate_response_schema(), 'schema_name': 'metadata'}
        return publication_candidates()
    monkeypatch.setattr(LLMService, 'generate_json', llm)
    Publication.generate(project.id, 1)
    status = Publication.status(project.id)
    assert not status['ready'] and status['metadata'] is None
    assert len(status['candidates']['title_candidates']) == 8
    assert not ProjectService.get_metadata_file(project.id).exists()
    title = status['candidates']['title_candidates'][6]
    chosen = Publication.select_title(project.id, 1, status['generation_id'], title)
    assert chosen['ready']
    metadata = chosen['metadata']
    assert metadata['youtube']['title'] == metadata['facebook']['title'] == title
    assert metadata['instagram']['caption'].startswith(title)
    assert metadata['tiktok']['description'].startswith(title)
    assert Publication.status(project.id) == chosen  # Disk reload retains choice.
    metadata['youtube']['description'] = 'Ma description manuelle.'
    Publication.save_metadata(project.id, 1, status['generation_id'], metadata)
    assert Publication.status(project.id)['metadata']['youtube']['description'] == 'Ma description manuelle.'


def test_failed_generation_and_stale_tabs_do_not_overwrite_choices(project, monkeypatch):
    old = generate(project, monkeypatch)
    Publication.select_title(project.id, 1, old['generation_id'], old['candidates']['title_candidates'][0])
    monkeypatch.setattr(LLMService, 'generate_json', lambda *a, **kw: {'title_candidates': ['incomplete']})
    with pytest.raises(ValueError):
        Publication.generate(project.id, 1)
    assert Publication.status(project.id)['ready']
    new = generate(project, monkeypatch)
    with pytest.raises(StaleFacecamRevision):
        Publication.select_title(project.id, 1, old['generation_id'], old['candidates']['title_candidates'][0])
    assert not new['ready']
    with pytest.raises(ValueError, match='huit'):
        Publication.select_title(project.id, 1, new['generation_id'], 'Un titre inconnu')


def test_script_change_invalidates_metadata_even_after_reapproval(project, monkeypatch):
    status = generate(project, monkeypatch)
    Publication.select_title(project.id, 1, status['generation_id'], status['candidates']['title_candidates'][0])
    state = FacecamService.load(project.id)
    state.blocks[0].text = 'Une nouvelle accroche qui change le sens.'
    FacecamService.save_draft(project.id, 1, state.blocks, '', '')
    FacecamService.approve(project.id, 2)
    assert Publication.status(project.id)['candidates'] is None
    assert not Publication.status(project.id)['ready']
    with pytest.raises(StaleFacecamRevision):
        Publication.save_metadata(project.id, 1, status['generation_id'], MetadataService.load(project.id).model_dump())


def test_script_change_during_generation_discards_result(project, monkeypatch):
    def llm(*args, **kwargs):
        state = FacecamService.load(project.id)
        state.blocks[0].text = 'Une autre accroche.'
        FacecamService.save_draft(project.id, 1, state.blocks, '', '')
        return publication_candidates()
    monkeypatch.setattr(LLMService, 'generate_json', llm)
    with pytest.raises(StaleFacecamRevision):
        Publication.generate(project.id, 1)
    assert not FacecamService.load(project.id).metadata_generation


def test_processing_rejects_missing_metadata_then_missing_thumbnail(project, monkeypatch, client):
    import app.services.facecam_auto_editor as auto
    monkeypatch.setattr(auto, 'prepared_is_current', lambda _: True)
    FacecamService.update(project.id, {'placement': {'key':'p', 'revision':1, 'fingerprint':'f', 'status':'complete'},
        'prepared': {'preview_version':'v', 'preview_music_key':project.resolved_music_key()}})
    response = client.post(f'/api/projects/{project.id}/process')
    assert response.status_code == 400 and 'métadonnées' in response.json()['detail']
    status = generate(project, monkeypatch)
    Publication.select_title(project.id, 1, status['generation_id'], status['candidates']['title_candidates'][0])
    monkeypatch.setattr(Thumbnails, 'status', lambda _: {'selected':None})
    response = client.post(f'/api/projects/{project.id}/process')
    assert response.status_code == 400 and 'miniature' in response.json()['detail']


@pytest.fixture
def episode(project, tmp_path, monkeypatch):
    path = tmp_path / 'episode.mp4'; path.write_bytes(b'video')
    monkeypatch.setattr(AnimeLibraryService, 'resolve_episode_path', lambda *a, **kw: path)
    monkeypatch.setattr(Thumbnails, '_probe', lambda *a: {'duration': 300, 'fps': 24})
    return path


def test_local_selection_is_bounded_diverse_cached_and_free(project, episode, monkeypatch):
    calls = []
    def extract(source, timestamp, **kwargs):
        calls.append(timestamp)
        # Simulate both a recurring shot and genuinely different scene compositions.
        seed = 1 if timestamp < 25 else round(timestamp)
        random = np.random.default_rng(seed)
        return Image.fromarray(random.integers(20, 235, (18, 32, 3), dtype=np.uint8)).resize((640, 360))
    monkeypatch.setattr(Thumbnails, 'extract', extract)
    monkeypatch.setattr(LLMService, 'generate_json', lambda *a, **kw: pytest.fail('Paid thumbnail call'))
    Thumbnails.generate(project.id)
    status = Thumbnails.status(project.id)
    assert len(status['candidates']) == 10 and not status['ready'] and status['cost_eur'] == 0
    assert len(calls) <= Thumbnails.MAX_SAMPLES
    assert len({(c['episode'], c['timestamp']) for c in status['candidates']}) == 10
    before = len(calls)
    Thumbnails.generate(project.id)
    assert len(calls) == before and Thumbnails.status(project.id) == status


def test_fades_rejected_and_portrait_preserves_proportions():
    assert quality(Image.new('RGB', (640, 360), 'black')) == 0
    assert quality(Image.new('RGB', (640, 360), 'white')) == 0
    image = Image.new('RGB', (640, 360), 'blue')
    ImageDraw.Draw(image).rectangle((0, 0, 200, 360), fill='red')
    assert portrait(image, 0).getpixel((540, 960)) == (255, 0, 0)
    assert portrait(image, 1).getpixel((540, 960)) == (0, 0, 255)


def test_manual_selection_security_staleness_and_zip_invalidation(project, episode, monkeypatch, client):
    monkeypatch.setattr(Thumbnails, 'extract', lambda *a, **kw: Image.new('RGB', (640,360), 'blue'))
    context = Thumbnails.context(project.id)
    request = {'version':context['version'], 'episode':context['sources'][0]['id'], 'timestamp':21, 'crop_x':.2}
    endpoint = f'/api/projects/{project.id}/facecam/thumbnail'
    assert client.put(endpoint, json={**request, 'episode':'../../private'}).status_code == 400
    assert client.put(endpoint, json={**request, 'timestamp':500}).status_code == 400
    assert client.put(endpoint, json={**request, 'crop_x':2}).status_code == 422
    assert client.put(endpoint, json={**request, 'version':'old'}).status_code == 409
    from app.services.export_service import ExportService
    bundle = ProjectService.get_project_dir(project.id) / f'{ExportService.output_folder_name(project)}.zip'
    bundle.write_bytes(b'previous zip')
    response = client.put(endpoint, json=request)
    assert response.status_code == 200 and response.json()['ready']
    assert not bundle.exists()
    image_response = client.get(endpoint)
    assert image_response.status_code == 200
    with Image.open(io.BytesIO(image_response.content)) as image:
        assert image.size == (1080,1920) and image.format == 'JPEG' and image.info.get('icc_profile')
    assert len(image_response.content) < 2_000_000
    episode.write_bytes(b'replaced source')
    assert not Thumbnails.status(project.id)['ready']
    assert client.put(endpoint, json=request).status_code == 409


def test_verified_audio_remux_preserves_thumbnail(project, episode, monkeypatch):
    from app.services.facecam_visual_index import file_identity
    from app.services.facecam_placement_service import FacecamPlacementService
    complete_publication(project.id, monkeypatch, episode)
    before = file_identity(episode)
    selected = Thumbnails.status(project.id)['selected']
    episode.write_bytes(b'same-video-new-audio')
    FacecamPlacementService.preserve_audio_normalization(before, file_identity(episode))
    assert Thumbnails.status(project.id)['selected'] == selected


def test_real_ffmpeg_frame_extraction_and_vertical_export(tmp_path):
    from app.services.facecam_media_service import ffmpeg
    video = tmp_path / 'synthetic.mp4'
    ffmpeg(['-f','lavfi','-i','testsrc2=size=640x360:rate=24','-t','1', '-c:v','libx264','-pix_fmt','yuv420p',str(video)])
    source = {'path': str(video)}
    frame = Thumbnails.extract(source, .5, width=320)
    assert frame.size == (320,180)
    full = Thumbnails.extract(source, .5)
    path = tmp_path / 'thumbnail.jpg'
    Thumbnails.save_jpeg(portrait(full, .3), path)
    with Image.open(path) as image:
        assert image.size == (1080,1920) and image.format == 'JPEG'
