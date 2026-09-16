import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.facecam_delivery import FacecamDelivery, FacecamDrive

@pytest.fixture
def delivery(tmp_path):
    settings=SimpleNamespace(data_dir=tmp_path,facecam_drive_client_id='client',facecam_drive_client_secret='secret',facecam_drive_refresh_token='refresh',facecam_channel_id='channel',discord=SimpleNamespace(guild_id='guild'))
    discord=AsyncMock(); discord.post_message.return_value='message'
    service=FacecamDelivery(settings,lambda:discord); service.drive=AsyncMock()
    service.save({'project_id':'p1','anime_title':'Naruto','folder_id':'folder','zip_id':'zip','zip_name':'project.zip','channel_id':'channel','message_id':'message','status':'awaiting'})
    return service,discord

@pytest.mark.asyncio
@pytest.mark.parametrize('files,reason',[([], 'Aucun MP4'),([{'name':'one.mp4'},{'name':'two.MP4'}],'Plusieurs MP4'),([{'id':'v','name':'empty.mp4','size':'0'}],'vide')])
async def test_bad_upload_is_private_pending_and_unchanged(delivery,files,reason):
    service,discord=delivery; service.drive.children.return_value=files
    interaction=SimpleNamespace(data={'custom_id':'facecam:confirm:p1'},guild_id='guild',channel_id='channel',message=SimpleNamespace(id='message'),response=AsyncMock(),followup=AsyncMock())
    await service.interaction(interaction)
    assert reason in interaction.followup.send.await_args.args[0]
    assert interaction.followup.send.await_args.kwargs['ephemeral']
    assert service.get('p1')['status']=='awaiting'
    service.drive.rename.assert_not_awaited(); discord.edit_message.assert_not_awaited()

@pytest.mark.asyncio
async def test_concurrent_confirmation_survives_restart(delivery):
    service,discord=delivery; service.drive.children.return_value=[{'id':'video','name':'Montage.MP4','size':'100'}]
    await asyncio.gather(service.confirm('p1'),service.confirm('p1'))
    service.drive.rename.assert_awaited_once_with('video','folder')
    restarted=FacecamDelivery(service.settings,lambda:discord); restarted.drive=AsyncMock()
    assert restarted.get('p1')['status']=='complete'
    await restarted.confirm('p1'); restarted.drive.rename.assert_not_awaited()
    content,components=service.message(restarted.get('p1'))
    assert 'Montage terminé' in content and components[0]['components'][-1]['disabled']

@pytest.mark.asyncio
async def test_discord_failure_recovers_without_second_rename(delivery):
    service,discord=delivery; service.drive.children.return_value=[{'id':'video','name':'montage.mp4','size':'100'}]
    discord.edit_message.side_effect=RuntimeError('Discord offline')
    with pytest.raises(RuntimeError): await service.confirm('p1')
    assert service.get('p1')['status']=='complete'
    discord.edit_message.side_effect=None
    await service.confirm('p1'); service.drive.rename.assert_awaited_once()

@pytest.mark.asyncio
async def test_retry_after_rename_before_save_is_idempotent(delivery,monkeypatch):
    service,_=delivery; service.drive.children.return_value=[{'id':'video','name':'montage.mp4','size':'100'}]
    save=service.save
    def fail(task):
        if task['status']=='complete': raise OSError('disk full')
        save(task)
    monkeypatch.setattr(service,'save',fail)
    with pytest.raises(OSError): await service.confirm('p1')
    assert service.get('p1')['status']=='awaiting'
    monkeypatch.setattr(service,'save',save)
    service.drive.children.return_value=[{'id':'video','name':'output.mp4','size':'100'}]
    assert (await service.confirm('p1'))['status']=='complete'
    assert service.drive.rename.await_args.args==('video','folder')

@pytest.mark.asyncio
async def test_reexport_preserves_completion_explicit_reopen_archives(delivery):
    service,_=delivery; task=service.get('p1'); task.update(status='complete',output_id='video'); service.save(task)
    await service.register('p1',{'folder_id':'folder','zip_id':'new-zip','zip_name':'project.zip','anime_title':'Naruto'})
    assert service.get('p1')['status']=='complete'; service.drive.archive.assert_not_awaited()
    await service.reopen('p1'); await service.reopen('p1')
    service.drive.archive.assert_awaited_once_with('video','folder')
    assert service.get('p1')['status']=='awaiting'
    assert not service.message(service.get('p1'))[1][0]['components'][-1]['disabled']

@pytest.mark.asyncio
async def test_rejects_copied_button(delivery):
    service,_=delivery
    interaction=SimpleNamespace(data={'custom_id':'facecam:confirm:p1'},guild_id='guild',channel_id='elsewhere',message=SimpleNamespace(id='message'),response=AsyncMock(),followup=AsyncMock())
    await service.interaction(interaction); service.drive.children.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs['ephemeral']

@pytest.mark.asyncio
async def test_drive_root_and_parent_guard(delivery):
    service,_=delivery; drive=FacecamDrive(service.settings)
    drive.request=AsyncMock(side_effect=[{'files':[]},{'id':'v','parents':['different-folder']}])
    assert await drive.children('folder')==[]
    assert drive.request.await_args.kwargs['params']['q']=="'folder' in parents and trashed=false"
    with pytest.raises(ValueError): await drive.rename('v','folder')
    assert drive.request.await_count==2
