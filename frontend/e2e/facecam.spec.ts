import {test,expect,type Page} from '@playwright/test';
import type {FacecamState} from '../src/api/facecam';

async function fixture(page:Page) {
  const id='facecam-browser';
  let checks=0,saves=0;
  let state:FacecamState={revision:1,approved_revision:null,selected_hook:'Imagine ce ninja.',selected_opinion:'Ce choix est intéressant.',
    hooks:Array.from({length:8},(_,i)=>({id:`hook-${i}`,text:`Accroche ${i+1}`,original:i===0})),opinions:Array.from({length:8},(_,i)=>({id:`opinion-${i}`,text:`Avis ${i+1}`,original:false})),
    blocks:[{id:'hook',kind:'hook',text:'Imagine ce ninja.',scene_indices:[]},{id:'body-0',kind:'body',text:'Naruto poursuit son rêve.',scene_indices:[0]},
      {id:'raw-1',kind:'raw',text:'',scene_indices:[1]},{id:'body-2',kind:'body',text:'Il devient plus fort.',scene_indices:[2]},
      {id:'opinion',kind:'opinion',text:'Ce choix est intéressant.',scene_indices:[]},{id:'ending',kind:'ending',text:'Le nom de l’anime c’est :',scene_indices:[]}],
    fact_revision:1,fact_status:'complete',fact_error:null,findings:[{quote:'Naruto',start:18,end:24,kind:'uncertain',explanation:'Identité à vérifier.',correction:'Vérifiez le prénom.',sources:[{url:'https://example.org/official',title:'Site officiel'}]}],recording:null,prepared:null,cover:null,job:null};
  await page.route(url=>url.pathname.startsWith('/api/'),async route=>{
    const path=new URL(route.request().url()).pathname.replace(`/api/projects/${id}`,'');
    const json=(body:unknown)=>route.fulfill({json:body});
    if(path==='')return json({id,anime_name:'Naruto',project_mode:'facecam',library_type:'anime',phase:'script_restructure',source_paths:[]});
    if(path==='/facecam')return json(state);
    if(path==='/facecam/settings')return json({llm_preset:'gpt',music_key:'music',models:[{key:'gpt',label:'GPT'}],music:[{key:'music',label:'Musique'}]});
    if(path==='/metadata')return json({metadata:null});
    if(path==='/facecam/draft'){const data=route.request().postDataJSON();expect(data.revision).toBe(state.revision);state={...state,...data,revision:state.revision+1,approved_revision:null,prepared:null};saves++;return json(state);}
    if(path==='/facecam/selection'){const data=route.request().postDataJSON();state={...state,selected_hook:data.hook,selected_opinion:data.opinion};return json(state);}
    if(path==='/facecam/fact-check'){checks++;state={...state,fact_revision:state.revision};return json(state);}
    return route.fulfill({status:404,json:{detail:'Unused fixture endpoint'}});
  });
  await page.goto(`/project/${id}/script`);
  await expect(page.getByRole('heading',{name:'Naruto',exact:true})).toBeVisible();
  return {get state(){return state;},get saves(){return saves;},get checks(){return checks;}};
}

test('whole-script editing autosaves, keeps raw markers, and only rechecks on request',async({page})=>{
  const f=await fixture(page);
  await expect(page.locator('.facecam-finding')).toHaveCount(1);
  await page.locator('.facecam-finding').click();
  await expect(page.getByRole('link',{name:'Site officiel'})).toBeVisible();
  const body=page.locator('section[data-facecam="body-0"] p');
  await body.click();await page.keyboard.press('End');await page.keyboard.type(' Il avance.');
  await expect.poll(()=>f.saves).toBe(1);
  await expect(page.locator('.facecam-finding')).toHaveCount(0);
  expect(f.checks).toBe(0);
  await page.reload();
  await expect(body).toContainText('Il avance.');
  await expect(page.getByText('Extrait audio original',{exact:true})).toBeVisible();
  expect(f.state.blocks.map(b=>b.kind)).toEqual(['hook','body','raw','body','opinion','ending']);
  await page.getByRole('button',{name:'Vérifier les faits',exact:true}).click();
  expect(f.checks).toBe(1);
  await expect(page.locator('.facecam-finding')).toHaveCount(1);
  expect(f.state.blocks.at(-1)?.text).toBe('Le nom de l’anime c’est :');
});

test('candidate modal has eight choices per step and editable original hook',async({page})=>{
  const f=await fixture(page);
  await page.getByRole('button',{name:'Choisir l’accroche et le point de vue'}).click();
  const modal=page.locator('dialog[open]');
  await expect(modal.locator('.facecam-candidate-grid button')).toHaveCount(8);
  await modal.getByRole('button',{name:/ORIGINALE/}).click();
  await modal.locator('textarea').fill('Ma version de l’accroche.');
  await modal.getByRole('button',{name:'Choisir le point de vue',exact:true}).click();
  await expect(modal.locator('.facecam-candidate-grid button')).toHaveCount(8);
  expect(f.state.selected_hook).toBe('Ma version de l’accroche.');
  await modal.getByRole('button',{name:'Retour aux accroches'}).click();
  await expect(modal.locator('textarea')).toHaveValue('Ma version de l’accroche.');
});

test('ordinary anime creation needs only confirmation and opens an independent tab',async({page})=>{
  let creations=0;
  page.on('pageerror', error => {throw error;});
  await page.route(url=>url.pathname.startsWith('/api/'),async route=>{
    const path=new URL(route.request().url()).pathname.replace('/api/projects/source','');
    const json=(body:unknown)=>route.fulfill({json:body});
    if(path==='')return json({id:'source',anime_name:'Naruto',project_mode:'standard',library_type:'anime',phase:'script_restructure',source_paths:[],video_duration:6,video_fps:30});
    if(path==='/transcription')return json({transcription:{language:'en',scenes:[{scene_index:0,text:'Naruto dreams.',start_time:0,end_time:6,is_raw:false,words:[]}]}});
    if(path==='/scenes')return json({scenes:[{index:0,start_time:0,end_time:6}]});
    if(path==='/facecam'&&route.request().method()==='POST'){creations++;return json({id:'new-facecam',project_mode:'facecam'});}
    if(path==='/script/automation/config')return json({enabled:true,llm:{configured:true,preset_key:'gpt',preset_label:'GPT',big_model:'gpt',light_model:'light'},elevenlabs:{configured:false,model_id:'eleven_v3',output_format:'mp3_44100_128'},voices:[],musics:[],templates:[],llm_presets:[],current:{llm_preset:'gpt',template:'zoomed',min_playback_speed:0.75},defaults:{llm_preset:'gpt',template:'zoomed',min_playback_speed:0.75}});
    if(path==='/script/settings')return json({});
    return route.fulfill({status:404,json:{detail:'Unused fixture'}});
  });
  await page.goto('/project/source/script');
  await page.getByRole('button',{name:'Facecam',exact:true}).click();
  const modal=page.locator('dialog[open]');
  await expect(modal).toContainText('Créer une version Facecam');
  await expect(modal.locator('select,input')).toHaveCount(0);
  expect(creations).toBe(0);
  const popup=page.waitForEvent('popup');
  await modal.getByRole('button',{name:'Créer le projet'}).click();
  const tab=await popup;
  await expect(tab).toHaveURL(/\/project\/new-facecam\/script$/);
  expect(creations).toBe(1);
  await expect(page).toHaveURL(/\/project\/source\/script$/);
  await tab.close();
});
