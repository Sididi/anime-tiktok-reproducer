import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {test, expect, type Page} from '@playwright/test';
import {DEFAULT_FACECAM_AUTO_EDITOR, type FacecamState, type RecordingCut, type RecordingEdit} from '../src/api/facecam';

let media: Buffer;
test.beforeAll(() => {
  const folder = mkdtempSync(join(tmpdir(), 'facecam-cuts-test-'));
  try {
    const path = join(folder, 'source.mp4');
    execFileSync('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x160:rate=30',
      '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '12', '-c:v', 'libx264', '-preset', 'ultrafast',
      '-c:a', 'aac', '-movflags', '+faststart', path]);
    media = readFileSync(path);
  } finally {rmSync(folder, {recursive: true, force: true});}
});

function editor(id = 'take-1'): RecordingEdit {
  return {id, version: 0, duration: 12, applied: false, analysis_status: 'ready', analysis_error: null, cuts: [], suggestions: [
    {id: 'ai-1', start: 1, end: 3, enabled: true, origin: 'ai', confidence: 'high', reason: 'Phrase interrompue puis reprise.',
      removed_text: 'Il découvre un nouveau pouvoir', retained_text: 'Il découvre un nouveau pouvoir vraiment incroyable.'},
    {id: 'ai-2', start: 5, end: 6, enabled: false, origin: 'ai', confidence: 'review', reason: 'Une reprise possible, à écouter.',
      removed_text: 'Il devient plus fort', retained_text: 'Il devient beaucoup plus fort.'},
  ]};
}

async function fixture(page: Page, options: {analyzing?: boolean; failApply?: boolean; applied?: boolean; dropEvents?: boolean} = {}) {
  const id = 'facecam-cut-browser';
  let state: FacecamState = {revision: 1, approved_revision: 1, selected_hook: 'Un ninja.', selected_opinion: '', hooks: [], opinions: [],
    blocks: [{id: 'hook', kind: 'hook', text: 'Un ninja.', scene_indices: []}, {id: 'body-0', kind: 'body', text: 'Il devient plus fort.', scene_indices: [0]}],
    fact_revision: null, fact_status: 'unchecked', fact_error: null, findings: [], prepared: null, cover: null, job: null,
    recording: {fingerprint: 'take-1', duration: 12, transcript: '', differences: [], reviewed_revision: null}, recording_edit: editor()};
  if (options.applied) state = {...state, recording_edit: {...editor(), applied: true}, recording: {...state.recording!, transcript: 'Ancienne transcription', reviewed_revision: 1}};
  let finishAnalysis = () => {};
  const analysis = new Promise<void>(resolve => {finishAnalysis = resolve;});
  if (options.analyzing) state = {...state, recording_edit: {...editor(), suggestions: [], analysis_status: 'analyzing'}, job: {status: 'processing', step: 'Analyse des reprises'}};
  let applyAttempts = 0, uploads = 0, previews = 0, eventConnections = 0;
  const requests: {cuts: RecordingCut[]; edit_id: string; version: number; revision: number}[] = [];
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname.replace(`/api/projects/${id}`, '');
    const json = (data: unknown) => route.fulfill({json: data});
    if (path === '') return json({id, anime_name: 'Naruto', project_mode: 'facecam', library_type: 'anime', phase: 'script_restructure', source_paths: []});
    if (path === '/facecam') return json(state);
    if (path === '/metadata') return json({metadata: null});
    if (path === '/facecam/settings') return json({llm_preset: 'gpt', music_key: 'music', models: [{key: 'gpt', label: 'GPT'}], music: [{key: 'music', label: 'Musique'}], auto_editor_defaults: DEFAULT_FACECAM_AUTO_EDITOR});
    if (path === '/facecam/recording/cuts') return json({id: state.recording_edit!.id, duration: 12,
      peaks: Array.from({length: 2400}, (_, i) => Math.sin(i / 3) * (i % 200 < 120 ? .65 : .02)),
      words: [{text: 'Il', start: 1, end: 1.2}, {text: 'découvre', start: 1.3, end: 1.7}, {text: 'pouvoir', start: 2.4, end: 2.8}, {text: 'Reprise', start: 3.1, end: 3.5}]});
    if (path === '/facecam/recording/cuts/apply') {
      applyAttempts++;
      const payload = route.request().postDataJSON(); requests.push(payload);
      if (options.failApply && applyAttempts === 1) return route.fulfill({status: 503, json: {detail: 'Transcription indisponible. Réessayez.'}});
      expect(payload.edit_id).toBe(state.recording_edit!.id); expect(payload.version).toBe(state.recording_edit!.version);
      state = {...state, recording_edit: {...state.recording_edit!, cuts: payload.cuts, version: payload.version + 1, applied: true},
        recording: {...state.recording!, fingerprint: `edited-${applyAttempts}`, transcript: 'Transcription définitive après les coupes.', reviewed_revision: 1}, job: {status: 'complete'}};
      return json(state);
    }
    if (path === '/facecam/recording' && route.request().method() === 'POST') {
      uploads++;
      state = {...state, recording_edit: editor(`replacement-${uploads}`), recording: {...state.recording!, fingerprint: `replacement-${uploads}`, transcript: '', reviewed_revision: null}};
      return json(state);
    }
    if (path === '/facecam/recording' || path === '/facecam/recording/source') {
      const range = route.request().headers().range?.match(/bytes=(\d+)-(\d*)/);
      const start = range ? Number(range[1]) : 0, end = range?.[2] ? Number(range[2]) : media.length - 1;
      return route.fulfill({status: range ? 206 : 200, contentType: 'video/mp4', body: media.subarray(start, end + 1),
        headers: {'accept-ranges': 'bytes', ...(range ? {'content-range': `bytes ${start}-${end}/${media.length}`} : {})}});
    }
    if (path === '/facecam/events') {
      eventConnections++;
      if (options.dropEvents && eventConnections === 1) return route.abort();
      await analysis;
      return route.fulfill({contentType: 'text/event-stream', body: `data: ${JSON.stringify(state)}\n\n`});
    }
    if (path === '/facecam/preview') {previews++; return json(state);}
    return route.fulfill({status: 404, json: {detail: 'Unused fixture endpoint'}});
  });
  await page.goto(`/project/${id}/script`);
  if (!options.applied) await expect(page.getByRole('dialog', {name: 'Nettoyer votre prise'})).toBeVisible();
  return {requests, errors, get previews() {return previews;}, get eventConnections() {return eventConnections;}, completeAnalysis() {
    state = {...state, recording_edit: editor(), job: {status: 'complete'}}; finishAnalysis();
  }};
}

test('cleanup opens before final transcription, with two confidence levels and real waveform playback', async ({page}) => {
  const f = await fixture(page);
  const modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  await expect(modal.getByLabel('Retirer le passage 1')).toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 2')).not.toBeChecked();
  await expect(modal.getByRole('button', {name: 'Sélectionner une coupe'})).toBeEnabled();
  await expect(modal.locator('canvas').first()).toBeVisible();
  const video = modal.getByLabel('Prise originale à monter');
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await modal.getByRole('article', {name: 'Passage 1', exact: true}).getByRole('button', {name: 'Écouter', exact: true}).click();
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(1.2);
  expect(await video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeLessThan(3);
  await modal.getByRole('button', {name: 'Mettre en pause'}).click();
  await modal.getByRole('article', {name: 'Passage 1', exact: true}).getByRole('button', {name: 'Raccord', exact: true}).click();
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThanOrEqual(3);
  await modal.getByRole('button', {name: 'Mettre en pause'}).click();
  await expect(modal.getByText('1 coupe · 2.0 s retirées')).toBeVisible();
  await page.screenshot({path: 'test-results/facecam-cuts-desktop.png'});
  expect(f.previews).toBe(0); expect(f.requests).toHaveLength(0); expect(f.errors).toEqual([]);
});

test('manual cuts, precise adjustment, undo and local drafts survive closing and reload', async ({page}) => {
  const f = await fixture(page);
  let modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  const video = modal.getByLabel('Prise originale à monter');
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await video.evaluate((v: HTMLVideoElement) => {v.currentTime = 8;});
  await modal.getByRole('button', {name: 'Début [I]', exact: true}).click();
  await video.evaluate((v: HTMLVideoElement) => {v.currentTime = 9.5;});
  await modal.getByRole('button', {name: 'Fin [O]', exact: true}).click();
  await expect(modal.getByLabel('Retirer le passage 3')).toBeChecked();
  await modal.getByLabel('Fin de la coupe').fill('10');
  await expect(modal.getByLabel('Fin de la coupe')).toHaveValue('10');
  await modal.getByRole('button', {name: 'Annuler la dernière modification'}).click();
  await expect(modal.getByLabel('Fin de la coupe')).toHaveValue('9.5');
  await modal.getByLabel('Retirer le passage 1').uncheck();
  await modal.getByLabel('Retirer le passage 2').check();
  await modal.getByRole('slider', {name: 'Zoom de la forme d’onde'}).fill('4');
  await modal.getByRole('button', {name: 'Fermer le montage'}).click();
  await page.getByRole('button', {name: 'Vérifier les coupes'}).click();
  await expect(modal.getByLabel('Retirer le passage 1')).not.toBeChecked();
  await page.reload();
  modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  await expect(modal.getByLabel('Retirer le passage 1')).not.toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 2')).toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 3')).toBeChecked();
  await modal.getByRole('button', {name: 'Valider les coupes et continuer'}).click();
  await expect(modal).not.toBeVisible();
  await expect(page.getByText('Transcription définitive après les coupes.')).toBeVisible();
  expect(f.requests[0].cuts.filter(c => c.enabled)).toHaveLength(2);
  expect(f.requests[0].cuts.find(c => c.origin === 'manual')).toMatchObject({start: 8, end: 9.5});
  expect(f.errors).toEqual([]);
});

test('drag creates a manual cut, resize adjusts it, and a replacement starts a fresh edit', async ({page}) => {
  await fixture(page, {applied: true});
  await expect(page.getByRole('dialog', {name: 'Nettoyer votre prise'})).not.toBeVisible();
  await page.getByRole('button', {name: 'Modifier les coupes'}).click();
  const modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  await modal.getByRole('button', {name: 'Sélectionner une coupe'}).click();
  const wrapper = modal.locator('.facecam-cuts-wave [part="wrapper"]');
  const bounds = (await wrapper.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * .7, bounds.y + 50);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * .85, bounds.y + 50, {steps: 8});
  await page.mouse.up();
  await expect(modal.getByLabel('Retirer le passage 3')).toBeChecked();
  const before = Number(await modal.getByLabel('Fin de la coupe').inputValue());
  const region = modal.locator('.facecam-cuts-wave [part^="region region-"]');
  const handle = region.locator('[part="region-handle region-handle-right"]');
  const rect = (await handle.boundingBox())!;
  await page.mouse.move(rect.x + rect.width / 2, rect.y + 50);
  await page.mouse.down();
  await page.mouse.move(rect.x + 20, rect.y + 50, {steps: 5});
  await page.mouse.up();
  await expect.poll(async () => Number(await modal.getByLabel('Fin de la coupe').inputValue())).toBeGreaterThan(before);
  await modal.getByRole('button', {name: 'Fermer le montage'}).click();
  await page.locator('.facecam-upload input').setInputFiles({name: 'replacement.mp4', mimeType: 'video/mp4', buffer: media});
  await expect(modal).toBeVisible();
  await expect(modal.getByLabel('Retirer le passage 1')).toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 2')).not.toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 3')).toHaveCount(0);
});

test('manual changes and undo survive new suggestions and an interrupted event stream', async ({page}) => {
  const f = await fixture(page, {analyzing: true, dropEvents: true});
  const modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  await expect(modal.getByText('Analyse des reprises…', {exact: false})).toBeVisible();
  const video = modal.getByLabel('Prise originale à monter');
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await video.evaluate((v: HTMLVideoElement) => {v.currentTime = 8;});
  await modal.getByRole('button', {name: 'Début [I]', exact: true}).click();
  await video.evaluate((v: HTMLVideoElement) => {v.currentTime = 10;});
  await modal.getByRole('button', {name: 'Fin [O]', exact: true}).click();
  await expect(modal.getByRole('button', {name: 'Valider les coupes et continuer'})).toBeDisabled();
  await expect.poll(() => f.eventConnections).toBeGreaterThanOrEqual(2);
  f.completeAnalysis();
  await expect(modal.getByLabel('Retirer le passage 3')).toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 1')).toBeChecked();
  await expect(modal.getByLabel('Retirer le passage 2')).not.toBeChecked();
  await expect(modal.getByRole('button', {name: 'Valider les coupes et continuer'})).toBeEnabled();
  await modal.getByRole('button', {name: 'Annuler la dernière modification'}).click();
  await expect(modal.getByLabel('Retirer le passage 3')).toHaveCount(0);
  await expect(modal.getByLabel('Retirer le passage 1')).toBeChecked();
  expect(f.errors).toEqual([]);
});

test('failed application keeps choices retryable and keeping everything still validates the take', async ({page}) => {
  const f = await fixture(page, {failApply: true});
  const modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  await modal.getByRole('button', {name: 'Tout garder'}).click();
  await modal.getByRole('button', {name: 'Garder toute la prise et continuer'}).click();
  await expect(modal.getByRole('alert')).toContainText('Transcription indisponible');
  await expect(modal.getByLabel('Retirer le passage 1')).not.toBeChecked();
  await modal.getByRole('button', {name: 'Garder toute la prise et continuer'}).click();
  await expect(modal).not.toBeVisible();
  expect(f.requests).toHaveLength(2);
  expect(f.requests[1].cuts.every(c => !c.enabled)).toBe(true);
});

test('cleanup stays usable at a narrow viewport', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await fixture(page);
  const modal = page.getByRole('dialog', {name: 'Nettoyer votre prise'});
  await expect(modal.getByRole('button', {name: 'Valider les coupes et continuer'})).toBeVisible();
  expect(await modal.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({path: 'test-results/facecam-cuts-mobile.png'});
});
