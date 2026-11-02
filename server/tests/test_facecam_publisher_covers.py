from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.services.facebook_publisher import create_facebook_scheduled_post


@pytest.mark.parametrize('rejected', [False, True])
async def test_deferred_facebook_applies_cover_without_rescheduling_on_rejection(tmp_path, monkeypatch, respx_mock, rejected):
    video = tmp_path / 'video.mp4'; video.write_bytes(b'video')
    async def download(*args):
        return video
    monkeypatch.setattr('app.services.facebook_publisher._download_video', download)
    endpoint = respx_mock.post('https://graph.facebook.com/v25.0/page/video_reels').mock(side_effect=[
        httpx.Response(200, json={'video_id':'video', 'upload_url':'https://rupload.example/upload'}),
        httpx.Response(200, json={'success':True}),
    ])
    respx_mock.post('https://rupload.example/upload').mock(return_value=httpx.Response(200, json={'success':True}))
    respx_mock.get('https://example.org/cover.jpg').mock(return_value=httpx.Response(200, content=b'\xff\xd8jpeg'))
    cover = respx_mock.post('https://graph.facebook.com/v25.0/video/thumbnails').mock(return_value=httpx.Response(
        400 if rejected else 200, json={'success': not rejected}))
    result = await create_facebook_scheduled_post(page_id='page', page_access_token='fixture', title='Title', description='Description',
        prepared_video_url='https://example.org/video.mp4', scheduled_at=datetime.now(UTC)+timedelta(days=1), cover_url='https://example.org/cover.jpg')
    assert result.success and result.video_id == 'video'
    assert endpoint.call_count == 2 and cover.call_count == 1
    assert bool(result.detail) == rejected
    assert b'name="source"' in cover.calls[0].request.content
