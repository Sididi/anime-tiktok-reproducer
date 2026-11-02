"""Reach the post-recording assignment boundary without encoding a test video."""
from app.services.facecam_service import FacecamService
from app.services.facecam_recording_review import FacecamRecordingReview


def validate_recording(project_id, revision):
    state = FacecamService.approve(project_id, revision)
    FacecamService.update(project_id, {"recording": {
        **(state.recording or {}), "fingerprint": "test-take", "transcript": state.spoken_text(),
        "reviewed_revision": None,
    }})
    state = FacecamRecordingReview.ensure(project_id)
    review = state.recording_review
    return FacecamRecordingReview.validate(project_id, revision, review.id, review.version)


def approve_and_assign(project_id, revision):
    validate_recording(project_id, revision)
    return FacecamService.assign_scenes(project_id, revision)


def publication_candidates():
    return {"title_candidates": [f"Ce ninja cache un secret {i}" for i in range(8)],
            "facebook": {"description": "Une découverte inattendue.", "tags": ["anime"]},
            "instagram": {"hashtags": ["#Anime"]},
            "youtube": {"description": "Une histoire de ninja.", "tags": ["anime", "Naruto"]}}


def complete_publication(project_id, monkeypatch, episode):
    from PIL import Image
    from app.services.facecam_publication_service import FacecamPublicationService as Publication
    from app.services.facecam_thumbnail_service import FacecamThumbnailService as Thumbnails
    from app.services.anime_library import AnimeLibraryService
    from app.services.llm_service import LLMService
    monkeypatch.setattr(LLMService, 'generate_json', lambda *a, **kw: publication_candidates())
    monkeypatch.setattr(AnimeLibraryService, 'resolve_episode_path', lambda *a, **kw: episode)
    monkeypatch.setattr(Thumbnails, '_probe', lambda *a: {'duration': 300, 'fps': 24})
    monkeypatch.setattr(Thumbnails, 'extract', lambda *a, **kw: Image.new('RGB', (640, 360), 'steelblue'))
    state = FacecamService.load(project_id)
    Publication.generate(project_id, state.revision)
    status = Publication.status(project_id)
    Publication.select_title(project_id, state.revision, status['generation_id'], status['candidates']['title_candidates'][2])
    context = Thumbnails.context(project_id)
    Thumbnails.select(project_id, context['version'], context['sources'][0]['id'], 21)
