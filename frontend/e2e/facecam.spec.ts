import {test,expect,type Page} from '@playwright/test';
import {DEFAULT_FACECAM_AUTO_EDITOR, type FacecamState, type FacecamStory} from '../src/api/facecam';

async function fixture(page:Page, options?: {empty?: boolean; stories?: FacecamStory[]; failAnalysis?: boolean; recording?: boolean; failPreview?: boolean; legacyPreview?: boolean}) {
  const id='facecam-browser';
  let checks=0,saves=0,analyses=0;
  let preset='gpt';
  const analysisPresets:string[]=[];
  const previews: {revision: number; auto_editor: typeof DEFAULT_FACECAM_AUTO_EDITOR}[]=[];
  let state:FacecamState={revision:1,approved_revision:null,selected_hook:'Imagine ce ninja.',selected_opinion:'Ce choix est intéressant.',
    hooks:Array.from({length:8},(_,i)=>({id:`hook-${i}`,text:`Accroche ${i+1}`,original:i===0})),opinions:Array.from({length:8},(_,i)=>({id:`opinion-${i}`,text:`Avis ${i+1}`,original:false})),
    blocks:[{id:'hook',kind:'hook',text:'Imagine ce ninja.',scene_indices:[]},{id:'body-0',kind:'body',text:'Naruto poursuit son rêve.',scene_indices:[0]},
      {id:'raw-1',kind:'raw',text:'',scene_indices:[1]},{id:'body-2',kind:'body',text:'Il devient plus fort.',scene_indices:[2]},
      {id:'opinion',kind:'opinion',text:'Ce choix est intéressant.',scene_indices:[]},{id:'ending',kind:'ending',text:'Le nom de l’anime c’est :',scene_indices:[]}],
    fact_revision:1,fact_status:'complete',fact_error:null,findings:[{quote:'Naruto',start:18,end:24,kind:'uncertain',explanation:'Identité à vérifier.',correction:'Vérifiez le prénom.',sources:[{url:'https://example.org/official',title:'Site officiel'}]}],recording:null,prepared:null,cover:null,job:null};
  if(options?.empty) state={...state,revision:0,blocks:[],selected_hook:'',selected_opinion:'',hooks:[],opinions:[],findings:[],fact_revision:null};
  if(options?.recording) state={...state,approved_revision:state.revision,auto_editor:{...DEFAULT_FACECAM_AUTO_EDITOR},
    recording:{fingerprint:'take-1',duration:77,transcript:'Imagine ce ninja.',differences:[],reviewed_revision:state.revision},
    prepared:{revision:state.revision,fingerprint:'take-1',preview_music_key:'music',preview_version:'original',
      ...(options.legacyPreview?{}:{auto_editor:{...DEFAULT_FACECAM_AUTO_EDITOR},auto_editor_signature:'signature-0'})}};
  await page.route(url=>url.pathname.startsWith('/api/'),async route=>{
    const path=new URL(route.request().url()).pathname.replace(`/api/projects/${id}`,'');
    const json=(body:unknown)=>route.fulfill({json:body});
    if(path==='')return json({id,anime_name:'Naruto',project_mode:'facecam',library_type:'anime',phase:'script_restructure',source_paths:[]});
    if(path==='/facecam')return json(state);
    if(path==='/facecam/settings'){
      if(route.request().method()==='PUT') preset=route.request().postDataJSON().llm_preset??preset;
      return json({llm_preset:preset,music_key:'music',models:[{key:'gpt',label:'GPT'},{key:'gemini',label:'Gemini'}],music:[{key:'music',label:'Musique'}],auto_editor_defaults:DEFAULT_FACECAM_AUTO_EDITOR});
    }
    if(path==='/facecam/preview'&&route.request().method()==='POST'){
      const data=route.request().postDataJSON();previews.push(data);
      if(options?.failPreview) return route.fulfill({status:503,json:{detail:'Analyse audio indisponible. Réessayez.'}});
      expect(data.revision).toBe(state.revision);
      state={...state,auto_editor:data.auto_editor,prepared:{revision:state.revision,fingerprint:'take-1',
        auto_editor:data.auto_editor,auto_editor_signature:`signature-${previews.length}`,preview_music_key:'music',preview_version:`preview-${previews.length}`,
        cut_report:{removed_seconds:13.4,cut_count:26,protected_word_count:221,clipped_word_count:0}}};
      return json(state);
    }
    if(path==='/metadata')return json({metadata:null});
    if(path==='/facecam/stories/analyze'){
      analyses++; analysisPresets.push(preset);
      if(options?.failAnalysis && analyses===1) {state={...state,job:{status:'error',message:'Analyse indisponible.'}};return json(state);}
      const stories=options?.stories??[];
      state={...state,stories,job:{status:'complete'}};
      if(stories.length===1&&!state.blocks.length) state={...state,selected_story_id:stories[0].id,revision:state.revision+1};
      return json(state);
    }
    if(path==='/facecam/story'){
      const data=route.request().postDataJSON();expect(data.revision).toBe(state.revision);
      state={...state,selected_story_id:data.story_id,revision:state.revision+1,blocks:[],hooks:[],opinions:[],selected_hook:'',selected_opinion:'',approved_revision:null,prepared:null};
      return json(state);
    }
    if(path==='/facecam/draft'){const data=route.request().postDataJSON();expect(data.revision).toBe(state.revision);state={...state,...data,revision:state.revision+1,approved_revision:null,prepared:null};saves++;return json(state);}
    if(path==='/facecam/selection'){const data=route.request().postDataJSON();state={...state,selected_hook:data.hook,selected_opinion:data.opinion};return json(state);}
    if(path==='/facecam/generate'){const data=route.request().postDataJSON();state={...state,selected_hook:data.hook,selected_opinion:data.opinion,revision:state.revision+1,blocks:state.blocks.filter(b=>b.kind!=='opinion'||data.opinion.trim())};return json(state);}
    if(path==='/facecam/fact-check'){checks++;state={...state,fact_revision:state.revision};return json(state);}
    return route.fulfill({status:404,json:{detail:'Unused fixture endpoint'}});
  });
  await page.goto(`/project/${id}/script`);
  await expect(page.getByRole('heading',{name:'Naruto',exact:true})).toBeVisible();
  return {analysisPresets,previews,get state(){return state;},get saves(){return saves;},get checks(){return checks;},get analyses(){return analyses;}};
}

test('autoeditor tweaks regenerate preview and persist without invalidating script or recording',async({page})=>{
  const f=await fixture(page,{recording:true});
  const process=page.getByRole('button',{name:'Passer au traitement'});
  await expect(process).toBeEnabled();
  const initialAudio=await page.getByLabel('Aperçu audio Facecam').getAttribute('src');
  const controls=[['Seuil de silence','-24'],['Pause minimale à couper','160'],['Marge avant la parole','40'],['Marge après la parole','70']];
  for(const [label,value] of controls) await page.getByRole('slider',{name:label,exact:true}).fill(value);
  await expect(process).toBeDisabled();
  await expect(page.getByText('Réglages modifiés :',{exact:false})).toBeVisible();
  await page.getByRole('button',{name:'Préparer l’aperçu audio'}).click();
  await expect(process).toBeEnabled();
  expect(f.previews).toEqual([{revision:1,auto_editor:{noise_threshold_db:-24,min_silence_ms:160,margin_before_ms:40,margin_after_ms:70}}]);
  expect(f.state.revision).toBe(1);
  expect(f.state.approved_revision).toBe(1);
  expect(f.state.recording?.reviewed_revision).toBe(1);
  expect(await page.getByLabel('Aperçu audio Facecam').getAttribute('src')).not.toBe(initialAudio);
  await expect(page.getByText('221 mots protégés.',{exact:false})).toBeVisible();
  await page.reload();
  for(const [label,value] of controls) await expect(page.getByRole('slider',{name:label,exact:true})).toHaveValue(value);
  await expect(process).toBeEnabled();
  await page.getByRole('button',{name:'Rétablir le rythme rapide'}).click();
  await expect(process).toBeDisabled();
  await page.getByRole('button',{name:'Préparer l’aperçu audio'}).click();
  await expect(process).toBeEnabled();
  expect(f.previews[1].auto_editor).toEqual(DEFAULT_FACECAM_AUTO_EDITOR);
  await expect(page.getByLabel('Aperçu audio Facecam')).toHaveAttribute('src',/preview-2$/);
});

test('failed preview keeps pending tuning available for retry',async({page})=>{
  const f=await fixture(page,{recording:true,failPreview:true});
  await page.getByRole('slider',{name:'Seuil de silence',exact:true}).fill('-30');
  await page.getByRole('button',{name:'Préparer l’aperçu audio'}).click();
  await expect(page.getByRole('alert').filter({visible:true})).toContainText('Analyse audio indisponible. Réessayez.');
  await expect(page.getByRole('slider',{name:'Seuil de silence',exact:true})).toHaveValue('-30');
  await expect(page.getByRole('button',{name:'Passer au traitement'})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Préparer l’aperçu audio'})).toBeEnabled();
  expect(f.previews).toHaveLength(1);
});

test('legacy preview requires regeneration with protected word cuts',async({page})=>{
  await fixture(page,{recording:true,legacyPreview:true});
  await expect(page.getByRole('button',{name:'Passer au traitement'})).toBeDisabled();
  await page.getByRole('button',{name:'Préparer l’aperçu audio'}).click();
  await expect(page.getByRole('button',{name:'Passer au traitement'})).toBeEnabled();
});

const storyOptions: FacecamStory[] = [
  {id:'story-1',title:'Le retour du héros',premise:'Il retrouve une ville qui le croit mort.',ending:'Il choisit une nouvelle identité.',steps:['Il découvre sa statue.','Il cache son rang.'],omitted:['La mission des novices.'],clips:[{scene_index:0,start_word:null,end_word:null}],raw_duration_seconds:0},
  {id:'story-2',title:'Le faux débutant',premise:'Un héros accompagne des novices.',ending:'Il décide de les protéger.',steps:['On lui impose un groupe.','Il repère les traces truquées.'],omitted:['Les détails du retour.'],clips:[{scene_index:2,start_word:0,end_word:2}],raw_duration_seconds:0},
];

test('new facecam waits for model selection and explicit analysis',async({page})=>{
  const f=await fixture(page,{empty:true,stories:storyOptions});
  await expect(page.getByLabel('Modèle',{exact:true})).toHaveValue('gpt');
  expect(f.analyses).toBe(0);
  await page.getByLabel('Modèle',{exact:true}).selectOption('gemini');
  await expect(page.getByLabel('Modèle',{exact:true})).toHaveValue('gemini');
  expect(f.analyses).toBe(0);
  await page.getByRole('button',{name:'Analyser les histoires'}).click();
  expect(f.analysisPresets).toEqual(['gemini']);
  await expect(page.getByText('2 histoires détectées.',{exact:false})).toBeVisible();
  expect(f.analyses).toBe(1);
  const angle=page.getByRole('button',{name:'Choisir l’accroche et le point de vue'});
  await expect(angle).toBeDisabled();
  const story=page.getByRole('article',{name:'Le faux débutant'});
  await expect(story).toContainText('Il décide de les protéger.');
  await story.getByText('Ce récit laisse de côté').click();
  await expect(story).toContainText('Les détails du retour.');
  await story.getByRole('button',{name:'Choisir cette histoire'}).click();
  await expect(angle).toBeEnabled();
  expect(f.state.selected_story_id).toBe('story-2');
  await page.reload();
  await expect(story.getByRole('button',{name:'Histoire retenue'})).toBeDisabled();
  expect(f.analyses).toBe(1);
});

test('a single story is automatically retained without a redundant confirmation',async({page})=>{
  const f=await fixture(page,{empty:true,stories:storyOptions.slice(0,1)});
  await page.getByRole('button',{name:'Analyser les histoires'}).click();
  await expect(page.getByText('Une seule histoire détectée : elle est retenue automatiquement.')).toBeVisible();
  await expect(page.getByRole('button',{name:'Choisir l’accroche et le point de vue'})).toBeEnabled();
  expect(f.state.selected_story_id).toBe('story-1');
  await page.reload();
  expect(f.analyses).toBe(1);
});

test('failed detection is retryable and cannot unlock the hook step',async({page})=>{
  const f=await fixture(page,{empty:true,stories:storyOptions,failAnalysis:true});
  await page.getByRole('button',{name:'Analyser les histoires'}).click();
  await expect(page.getByRole('button',{name:'Réessayer l’analyse'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Choisir l’accroche et le point de vue'})).toBeDisabled();
  expect(f.analyses).toBe(1);
  await page.getByRole('button',{name:'Réessayer l’analyse'}).click();
  await expect(page.getByRole('article')).toHaveCount(2);
  expect(f.analyses).toBe(2);
});

test('existing drafts are preserved until the user selects an analyzed story',async({page})=>{
  const f=await fixture(page,{stories:storyOptions});
  expect(f.analyses).toBe(0);
  await page.getByRole('button',{name:'Analyser les histoires'}).click();
  await expect(page.locator('section[data-facecam="body-0"]')).toBeVisible();
  await page.getByRole('article',{name:'Le faux débutant'}).getByRole('button',{name:'Repartir de cette histoire'}).click();
  await expect(page.locator('section[data-facecam="body-0"]')).toHaveCount(0);
  expect(f.state.blocks).toEqual([]);
  expect(f.state.selected_hook).toBe('');
  await expect(page.locator('.facecam-choice-summary')).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('article',{name:'Le faux débutant'}).getByRole('button',{name:'Histoire retenue'})).toBeDisabled();
});

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

test('optional opinion stays skipped after reload and allows generation and editing',async({page})=>{
  const f=await fixture(page);
  async function openOpinions() {
    await page.getByRole('button',{name:'Choisir l’accroche et le point de vue'}).click();
    await page.getByRole('button',{name:'Choisir le point de vue',exact:true}).click();
  }
  await openOpinions();
  const modal=page.locator('dialog[open]');
  const skip=modal.getByRole('button',{name:'Sans point de vue',exact:false});
  await expect(modal.locator('.facecam-candidate-grid button')).toHaveCount(8);
  await skip.click();
  await expect(skip).toHaveAttribute('aria-pressed','true');
  await expect(modal.locator('textarea')).toHaveValue('');
  await modal.getByRole('button',{name:/OPTION 1 Avis 1/}).click();
  await expect(skip).toHaveAttribute('aria-pressed','false');
  await skip.click();
  await modal.getByRole('button',{name:'Fermer',exact:true}).click();
  expect(f.state.selected_opinion).toBe('');
  await page.reload();
  await expect(page.locator('.facecam-choice-summary')).toContainText('Sans point de vue');
  await expect(page.getByRole('button',{name:'Régénérer le script',exact:true})).toBeEnabled();
  await openOpinions();
  await expect(skip).toHaveAttribute('aria-pressed','true');
  const generation=page.waitForRequest(r=>r.url().endsWith('/facecam/generate'));
  await modal.getByRole('button',{name:'Générer mon script',exact:true}).click();
  expect((await generation).postDataJSON().opinion).toBe('');
  await expect(page.locator('section[data-facecam="opinion"]')).toHaveCount(0);
  await expect(page.getByText('Extrait audio original',{exact:true})).toBeVisible();
  const body=page.locator('section[data-facecam="body-0"] p');
  await body.click();await page.keyboard.press('End');await page.keyboard.type(' Sans avis.');
  await expect.poll(()=>f.saves).toBe(1);
  await page.reload();
  await expect(body).toContainText('Sans avis.');
  await expect(page.locator('section[data-facecam="opinion"]')).toHaveCount(0);
});

test('processing shows the backend correction and a return to Facecam',async({page})=>{
  await fixture(page);
  const detail='Choisissez la bonne couverture dans Facecam, ou importez une image, puis relancez le traitement.';
  await page.route('**/api/projects/facecam-browser/process',route=>route.fulfill({status:400,json:{detail}}));
  await page.goto('/project/facecam-browser/processing');
  await expect(page.getByText(detail,{exact:true})).toBeVisible();
  await expect(page.getByText('SSE request failed with status 400',{exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'Retour à Facecam',exact:true}).click();
  await expect(page).toHaveURL(/\/project\/facecam-browser\/script$/);
});

test('fact review shows episode timestamps, unavailable subtitles, and cited dialogue',async({page})=>{
  const f=await fixture(page);
  const url='/api/projects/facecam-browser/facecam/fact-evidence/cue1';
  f.state.fact_context={padding_seconds:30,episodes:[
    {episode:'Episode 06',status:'available',language:'fr',note:null,windows:[{start:'00:01:30.000',end:'00:06:30.000'}],
      cues:[{id:'cue1',start:'00:02:00.000',end:'00:02:03.000',text:'Naruto est ici.',source_url:url}]},
    {episode:'Episode 07',status:'unavailable',language:null,note:'Aucun sous-titre textuel disponible.',windows:[],cues:[]}]};
  f.state.findings[0].sources=[{url,title:'Episode 06 · 00:02:00.000–00:02:03.000',kind:'subtitle',text:'Naruto est ici.'}];
  await page.reload();
  await page.getByText('Sous-titres transmis à la vérification',{exact:true}).click();
  await page.locator('summary').filter({hasText:'Episode 06'}).click();
  await expect(page.getByText('00:01:30.000–00:06:30.000',{exact:true})).toBeVisible();
  await expect(page.getByRole('link',{name:'00:02:00.000–00:02:03.000',exact:true})).toHaveAttribute('href',url);
  await page.locator('summary').filter({hasText:'Episode 07'}).click();
  await expect(page.getByText('Aucun sous-titre textuel disponible.',{exact:true})).toBeVisible();
  await page.locator('.facecam-finding').click();
  await expect(page.locator('.facecam-fact-detail blockquote')).toHaveText('Naruto est ici.');
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
