import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.instagram_immediate_service import InstagramImmediateService


@pytest.mark.parametrize('reject', [False, True])
@pytest.mark.parametrize('method', ['video_url', 'rupload'])
def test_immediate_instagram_receives_cover_and_retries_rejection(reject, method):
    requests = []
    def handle(request):
        requests.append(request.content.decode())
        if reject and len(requests) == 1:
            return httpx.Response(400, json={'error': 'Invalid cover'})
        return httpx.Response(200, json={'id':'container', 'uri':'https://example.org/upload'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = InstagramImmediateService._create_container(client, base='https://graph.example/v1', ig_user_id='user',
            ig_access_token='fixture', caption='Titre', share_to_feed=True, upload_method=method,
            video_url='https://example.org/video.mp4', cover_url='https://example.org/thumbnail.jpg')
    assert result[0] == 'container'
    assert 'cover_url=' in requests[0]
    if reject:
        assert len(requests) == 2 and 'cover_url=' not in requests[1]
