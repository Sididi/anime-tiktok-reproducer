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
