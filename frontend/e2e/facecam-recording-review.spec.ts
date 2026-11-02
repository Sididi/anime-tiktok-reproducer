import { test, expect, type Page } from '@playwright/test';
import { DEFAULT_FACECAM_AUTO_EDITOR, type FacecamState, type RecordingWordChange } from '../src/api/facecam';
import {readyMetadata, readyThumbnails, thumbnailImage} from './helpers/facecamPublication';

async function fixture(page: Page, options: {failSave?: boolean; noChanges?: boolean} = {}) {
  const id = 'recording-review';
  const blocks: FacecamState['blocks'] = [
    {id: 'hook', kind: 'hook', text: 'Imagine ce ninja.', scene_indices: []},
    {id: 'body-0', kind: 'body', text: '🦊 Naruto avance très vite.', scene_indices: [0]},
    {id: 'raw-1', kind: 'raw', text: '', scene_indices: [1]},
    {id: 'body-2', kind: 'body', text: 'Il retrouve ses amis.', scene_indices: [2]},
    {id: 'ending', kind: 'ending', text: 'Le nom de l’anime c’est :', scene_indices: []},
  ];
  function change(id: string, blockId: string, expected: string, spoken: string, replacement: string, point?: number): RecordingWordChange {
    const text = blocks.find(b => b.id === blockId)!.text;
    const start = point ?? Array.from(text.slice(0, text.indexOf(expected))).length;
    return {id, block_id: blockId, kind: !expected ? 'insert' : !spoken ? 'delete' : 'replace', start,
      end: start + Array.from(expected).length, expected, spoken, replacement};
  }
  const changes = options.noChanges ? [] : [
    change('replace', 'body-0', 'avance', 'marche', 'marche'),
    change('delete', 'body-0', 'très ', '', ''),
    change('insert', 'body-2', '', 'enfin', 'enfin ', 12),
    change('keep', 'body-2', 'amis', 'parents', 'parents'),
  ];
  let state: FacecamState = {revision: 1, approved_revision: 1, selected_hook: blocks[0].text, selected_opinion: '',
    blocks, hooks: [], opinions: [], findings: [], fact_revision: 1, fact_status: 'complete', fact_error: null,
    recording: {fingerprint: 'take', duration: 72, transcript: 'Imagine ce ninja. Naruto marche vite. Il retrouve enfin ses parents.', differences: [], reviewed_revision: null},
    recording_review: {id: 'review', revision: 1, fingerprint: 'take', version: 0, blocks: structuredClone(blocks), changes, validated_revision: null},
    placement: null, prepared: null, cover: null, job: null, auto_editor: {...DEFAULT_FACECAM_AUTO_EDITOR}};
  const saves: unknown[] = [], validations: unknown[] = [], errors: string[] = [];
  let previews = 0, release: (() => void) | undefined;
  let job = Promise.resolve();
  page.on('pageerror', error => errors.push(error.message));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname.replace(`/api/projects/${id}`, '');
    const method = route.request().method();
    const json = (data: unknown) => route.fulfill({json: data});
    if(path==='/facecam/metadata')return json(readyMetadata(state.revision));
    if(path==='/facecam/thumbnails')return json(readyThumbnails());
    if(path==='/facecam/thumbnail'||path.startsWith('/facecam/thumbnails/'))return route.fulfill({contentType:'image/svg+xml',body:thumbnailImage});
    if (path === '') return json({id, anime_name: 'Naruto', project_mode: 'facecam', library_type: 'anime', phase: 'script_restructure', source_paths: []});
    if (path === '/facecam') return json(state);
    if (path === '/metadata') return json({metadata: null});
    if (path === '/facecam/settings') return json({llm_preset: 'gpt', music_key: 'music', models: [{key: 'gpt', label: 'GPT'}], music: [{key: 'music', label: 'Musique'}], auto_editor_defaults: DEFAULT_FACECAM_AUTO_EDITOR});
    if (path === '/facecam/recording/review') {
      if (method === 'GET') return json(state);
      const data = route.request().postDataJSON();
      expect(data.revision).toBe(state.revision); expect(data.review_id).toBe('review'); expect(data.version).toBe(state.recording_review!.version);
      if (method === 'PUT') {
        saves.push(data);
        if (options.failSave && saves.length === 1) return route.fulfill({status: 503, json: {detail: 'Enregistrement indisponible. Réessayez.'}});
        const pending = state.recording_review!.changes.filter(c => !data.resolved_ids.includes(c.id)).flatMap(c => {
          const text = data.blocks.find((b: {id: string}) => b.id === c.block_id).text;
          if (!c.expected) return [c];
          const index = text.indexOf(c.expected);
          return index < 0 ? [] : [{...c, start: Array.from(text.slice(0, index)).length, end: Array.from(text.slice(0, index) + c.expected).length}];
        });
        state = {...state, recording_review: {...state.recording_review!, blocks: data.blocks, changes: pending,
          version: data.version + 1, validated_revision: null}, recording: {...state.recording!, reviewed_revision: null}, placement: null, prepared: null};
        return json(state);
      }
      validations.push(data);
      const revision = state.revision + Number(JSON.stringify(state.blocks) !== JSON.stringify(state.recording_review!.blocks));
      state = {...state, revision, approved_revision: revision, blocks: structuredClone(state.recording_review!.blocks),
        recording_review: {...state.recording_review!, revision, validated_revision: revision}, recording: {...state.recording!, reviewed_revision: revision},
        placement: {key: 'placement', revision, fingerprint: 'take', status: 'running', assignment_complete: false, error: null},
        job: {status: 'processing', step: 'Placement des scènes', message: 'Placement des scènes'}, preview_enabled: true};
      job = new Promise<void>(resolve => {release = resolve;});
      return json(state);
    }
    if (path === '/facecam/events') {await job; return route.fulfill({contentType: 'text/event-stream', body: `data: ${JSON.stringify(state)}\n\n`});}
    if (path === '/facecam/preview' && method === 'POST') {previews++; return json(state);}
    return json({});
  });
  await page.goto(`/project/${id}/script`);
  return {saves, validations, errors, get state() {return state;}, get previews() {return previews;},
    complete() {
      state = {...state, placement: {...state.placement!, status: 'complete', assignment_complete: true}, job: {status: 'complete'},
        prepared: {duration_seconds: 70, cover_end: 70, revision: state.revision, fingerprint: 'take', preview_version: 'done', preview_music_key: 'music',
          auto_editor: {...DEFAULT_FACECAM_AUTO_EDITOR}, auto_editor_signature: 'settings'}};
      release?.();
    }};
}

test('full script accepts word changes, permits direct editing, and launches only on validation', async ({page}) => {
  const f = await fixture(page);
  const editor = page.getByLabel('Relecture du texte enregistré', {exact: true});
  await expect(editor).toContainText('Imagine ce ninja.');
  await expect(editor).toContainText('Extrait audio original');
  await expect(editor).toContainText('Le nom de l’anime c’est :');
  expect(f.validations).toHaveLength(0);
  await page.getByRole('button', {name: 'Accepter le remplacement : avance → marche', exact: true}).click();
  await page.getByRole('button', {name: 'Accepter la suppression : très', exact: true}).click();
  await page.getByRole('button', {name: 'Accepter l’ajout : enfin', exact: true}).click();
  const paragraph = editor.locator('section[data-facecam="body-2"] p');
  await paragraph.click(); await page.keyboard.press('End'); await page.keyboard.type(' Il protège le village.');
  await expect.poll(() => f.state.recording_review!.blocks.find(b => b.id === 'body-2')!.text).toBe('Il retrouve enfin ses amis. Il protège le village.');
  expect(f.state.blocks[1].text).toBe('🦊 Naruto avance très vite.');
  expect(f.validations).toHaveLength(0); expect(f.previews).toBe(0);
  await page.reload();
  await expect(editor).toContainText('🦊 Naruto marche vite.');
  await expect(editor).toContainText('Il retrouve enfin ses amis');
  await expect(editor).toContainText('Il protège le village.');
  await expect(page.getByRole('button', {name: 'Accepter le remplacement : amis → parents', exact: true})).toBeVisible();
  await page.getByRole('region', {name: 'Vérification du texte enregistré'}).screenshot({path: 'test-results/facecam-recording-review.png'});
  const validate = page.getByRole('button', {name: 'Valider le texte et placer les scènes', exact: true});
  await validate.click();
  await expect(validate).toBeDisabled();
  expect(f.validations).toHaveLength(1);
  expect(f.state.blocks[1].text).toBe('🦊 Naruto marche vite.');
  expect(f.state.blocks.find(b => b.id === 'body-2')!.text).toContain('ses amis');
  f.complete();
  await expect(page.getByRole('button', {name: 'Passer au traitement'})).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('button', {name: 'Texte validé', exact: true})).toBeDisabled();
  expect(f.validations).toHaveLength(1); expect(f.previews).toBe(0); expect(f.errors).toEqual([]);
});

test('no detected differences still waits for explicit final validation', async ({page}) => {
  const f = await fixture(page, {noChanges: true});
  await expect(page.getByText('0 suggestion(s) restante(s)', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Préparer l’aperçu audio', exact: true})).toBeDisabled();
  await page.waitForTimeout(700);
  expect(f.validations).toHaveLength(0); expect(f.previews).toBe(0);
  await page.getByRole('button', {name: 'Valider le texte et placer les scènes'}).click();
  expect(f.validations).toHaveLength(1); f.complete();
});

test('failed draft save keeps edits, blocks placement, and can be retried', async ({page}) => {
  const f = await fixture(page, {failSave: true});
  const editor = page.getByLabel('Relecture du texte enregistré', {exact: true});
  await page.getByRole('button', {name: 'Accepter le remplacement : avance → marche', exact: true}).click();
  await page.getByRole('button', {name: 'Valider le texte et placer les scènes'}).click();
  await expect(page.getByRole('alert')).toContainText('Enregistrement indisponible');
  await expect(editor).toContainText('🦊 Naruto marche');
  expect(f.validations).toHaveLength(0);
  await page.getByRole('button', {name: 'Valider le texte et placer les scènes'}).click();
  await expect.poll(() => f.validations.length).toBe(1);
  expect(f.saves).toHaveLength(2); f.complete();
});

test('manual edits can remove undetected words while raw markers stay protected', async ({page}) => {
  const f = await fixture(page, {noChanges: true});
  const editor = page.getByLabel('Relecture du texte enregistré', {exact: true});
  const paragraph = editor.locator('section[data-facecam="body-2"] p');
  await paragraph.click(); await page.keyboard.press('Home'); await page.keyboard.press('Shift+End'); await page.keyboard.type('Il revient.');
  await expect.poll(() => f.state.recording_review!.blocks.find(b => b.id === 'body-2')!.text).toBe('Il revient.');
  await expect(editor.locator('[data-facecam-marker="raw-1"]')).toHaveAttribute('contenteditable', 'false');
  await page.getByRole('button', {name: 'Valider le texte et placer les scènes'}).click();
  expect(f.state.blocks.find(b => b.kind === 'raw')!.text).toBe(''); f.complete();
});

test('full-script review and validation fit a narrow screen', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  const f = await fixture(page);
  const review = page.getByRole('region', {name: 'Vérification du texte enregistré'});
  await review.scrollIntoViewIfNeeded();
  await page.getByRole('button', {name: 'Accepter l’ajout : enfin', exact: true}).click();
  await expect.poll(() => f.state.recording_review!.blocks.find(b => b.id === 'body-2')!.text).toBe('Il retrouve enfin ses amis.');
  expect(await review.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await review.screenshot({path: 'test-results/facecam-recording-review-mobile.png'});
  await page.getByRole('button', {name: 'Valider le texte et placer les scènes'}).click();
  await expect.poll(() => f.validations.length).toBe(1); f.complete();
});
