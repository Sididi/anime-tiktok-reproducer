import { test, expect, type BrowserContext, type Page } from '@playwright/test';
import type { FacecamState } from '../src/api/facecam';

async function fixture(context: BrowserContext, options: { failSave?: boolean; empty?: boolean; otherStory?: boolean } = {}) {
  let saves = 0;
  let state: FacecamState = {
    revision: 1, approved_revision: null, hooks: [], opinions: [], selected_hook: 'Imagine ce héros.', selected_opinion: '',
    blocks: [
      { id: 'hook', kind: 'hook', text: 'Imagine ce héros.', scene_indices: [] },
      { id: 'body', kind: 'body', text: 'Il revient dans sa ville après un long voyage. '.repeat(8), scene_indices: [0] },
      { id: 'raw', kind: 'raw', text: 'Cette réplique originale ne doit jamais être affichée.', scene_indices: [1] },
      { id: 'ending', kind: 'ending', text: 'Le nom de l’anime c’est :', scene_indices: [] },
    ],
    findings: [], fact_revision: null, fact_status: 'unchecked', fact_error: null,
    recording: null, prepared: null, cover: null, job: null,
  };
  if (options.empty) state = { ...state, blocks: [] };
  if (options.otherStory) state.stories = [{ id: 'other', title: 'Une autre histoire', premise: 'Un autre récit.', ending: 'La fin.', steps: [], omitted: [], clips: [], raw_duration_seconds: 0 }];
  await context.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/projects/prompter-test') return route.fulfill({ json: {
      id: 'prompter-test', anime_name: 'Naruto', project_mode: 'facecam', library_type: 'anime', phase: 'script_restructure', source_paths: [],
    } });
    if (path.endsWith('/facecam')) return route.fulfill({ json: state });
    if (path.endsWith('/facecam/settings')) return route.fulfill({ json: { llm_preset: 'gpt', music_key: null, models: [{ key: 'gpt', label: 'GPT' }], music: [] } });
    if (path.endsWith('/metadata')) return route.fulfill({ json: { metadata: null } });
    if (path.endsWith('/facecam/story')) {
      state = { ...state, revision: state.revision + 1, blocks: [], selected_story_id: 'other', selected_hook: '', selected_opinion: '' };
      return route.fulfill({ json: state });
    }
    if (path.endsWith('/facecam/draft')) {
      if (options.failSave) return route.fulfill({ status: 409, json: { detail: 'Le script a changé.' } });
      const data = route.request().postDataJSON();
      expect(data.revision).toBe(state.revision);
      state = { ...state, ...data, revision: state.revision + 1 };
      saves++;
      return route.fulfill({ json: state });
    }
    return route.fulfill({ status: 404, json: { detail: 'Unused fixture endpoint' } });
  });
  return { get saves() { return saves; }, get state() { return state; } };
}

async function open(page: Page) {
  await page.goto('/project/prompter-test/script');
  const popupPromise = page.waitForEvent('popup');
  await page.getByRole('button', { name: 'Ouvrir le prompteur' }).click();
  const reader = await popupPromise;
  await expect(reader.locator('.prompter-script')).toContainText('Imagine ce héros.');
  await expect(page.getByRole('region', { name: 'Prompteur' }).getByRole('button', { name: 'Lire', exact: true })).toBeEnabled();
  return reader;
}

const position = (page: Page) => page.locator('.prompter-viewport').evaluate(el => el.scrollTop);

test('reads narration and cues at 1024×600, remotely controls playback, and reconnects', async ({ page, context }) => {
  await fixture(context);
  const reader = await open(page);
  await reader.setViewportSize({ width: 1024, height: 600 });
  const panel = page.getByRole('region', { name: 'Prompteur' });
  await expect(reader.locator('.prompter-script')).toContainText('Extrait original · ne pas lire');
  await expect(reader.locator('.prompter-script')).not.toContainText('Cette réplique originale');
  await expect(reader.locator('.prompter-script')).toContainText('Le nom de l’anime c’est :');
  await expect(reader.locator('.prompter-script')).not.toContainText('Naruto');
  expect(await reader.locator('body').evaluate(el => el.scrollWidth)).toBe(1024);

  await panel.getByRole('button', { name: 'Lire', exact: true }).click();
  await expect.poll(() => position(reader)).toBeGreaterThan(8);
  await panel.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(reader.getByRole('button', { name: 'Lire', exact: true })).toBeVisible();
  const paused = await position(reader);
  await page.waitForTimeout(350);
  expect(await position(reader)).toBeCloseTo(paused, 0);

  await panel.getByLabel('Vitesse de lecture').fill('65');
  await expect(reader.getByText('65 px/s', { exact: true })).toBeVisible();
  await panel.getByLabel('Taille du texte', { exact: true }).fill('62');
  await expect(reader.locator('.prompter-script')).toHaveCSS('font-size', '62px');
  await panel.getByLabel('Effet miroir horizontal').check();
  await expect(reader.locator('.prompter-script')).toHaveClass(/is-mirrored/);

  await panel.getByLabel('Passage à lire').selectOption('ending');
  await expect.poll(() => position(reader)).toBeGreaterThan(500);
  await expect(reader.getByRole('button', { name: 'Lire', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Recommencer' }).click();
  await expect.poll(() => position(reader)).toBe(0);
  const windows = context.pages().length;
  await panel.getByRole('button', { name: 'Afficher le prompteur' }).click();
  expect(context.pages()).toHaveLength(windows);
  await page.reload();
  await expect(panel.getByRole('button', { name: 'Lire', exact: true })).toBeEnabled();
  await reader.close();
  await expect(panel.getByRole('button', { name: 'Lire', exact: true })).toBeDisabled();
  const reopened = await open(page);
  await expect(reopened.locator('.prompter-script')).toHaveCSS('font-size', '62px');
  await expect(reopened.locator('.prompter-script')).toHaveClass(/is-mirrored/);
});

test('saves before opening and pauses at the start when a newer draft arrives', async ({ page, context }) => {
  const f = await fixture(context);
  await page.goto('/project/prompter-test/script');
  const body = page.locator('section[data-facecam="body"] p');
  await body.fill('Une nouvelle narration enregistrée.');
  const popupPromise = page.waitForEvent('popup');
  await page.getByRole('button', { name: 'Ouvrir le prompteur' }).click();
  const reader = await popupPromise;
  await expect(reader.locator('.prompter-script')).toContainText('Une nouvelle narration enregistrée.');
  expect(f.saves).toBe(1);
  const panel = page.getByRole('region', { name: 'Prompteur' });
  await panel.getByRole('button', { name: 'Lire', exact: true }).click();
  await expect.poll(() => position(reader)).toBeGreaterThan(8);
  await body.fill('Le texte corrigé arrive aussi sur le prompteur.');
  await expect(reader.locator('.prompter-script')).toContainText('Le texte corrigé arrive aussi sur le prompteur.');
  await expect(reader.getByRole('button', { name: 'Lire', exact: true })).toBeVisible();
  await expect.poll(() => position(reader)).toBe(0);
  await expect(reader.getByRole('status')).toContainText('Texte actualisé');
  expect(f.saves).toBe(2);
});

test('standalone reader supports keyboard, stops at the end and reloads the saved text', async ({ page, context }) => {
  await fixture(context);
  await page.setViewportSize({ width: 1024, height: 600 });
  await page.goto('/project/prompter-test/prompter');
  await expect(page.locator('.prompter-script')).toContainText('Imagine ce héros.');
  await page.keyboard.press('ArrowUp');
  await expect(page.getByText('37 px/s', { exact: true })).toBeVisible();
  await page.keyboard.press('Space');
  await expect.poll(() => position(page)).toBeGreaterThan(8);
  await page.keyboard.press('Space');
  await page.keyboard.press('PageDown');
  await expect.poll(() => position(page)).toBeGreaterThan(20);
  await page.keyboard.press('Home');
  await expect.poll(() => position(page)).toBe(0);
  await page.locator('.prompter-viewport').evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.keyboard.press('Space');
  await expect(page.getByRole('button', { name: 'Relire', exact: true })).toBeVisible();
  await expect(page.getByRole('progressbar')).toHaveAttribute('value', '1');
  await page.reload();
  await expect(page.locator('.prompter-script')).toContainText('Imagine ce héros.');
  await expect(page.getByText('37 px/s', { exact: true })).toBeVisible();
  await expect.poll(() => position(page)).toBe(0);
});

test('blocked popups and save conflicts do not launch a stale draft', async ({ page, context }) => {
  await fixture(context, { failSave: true });
  await page.goto('/project/prompter-test/script');
  await page.evaluate(() => { window.open = () => null; });
  await page.getByRole('button', { name: 'Ouvrir le prompteur' }).click();
  await expect(page.getByRole('alert')).toContainText('Autorisez les fenêtres contextuelles');
  await page.reload();
  await page.locator('section[data-facecam="body"] p').fill('Texte non enregistré.');
  const popupPromise = page.waitForEvent('popup');
  await page.getByRole('button', { name: 'Ouvrir le prompteur' }).click();
  const reader = await popupPromise;
  await expect.poll(() => reader.isClosed()).toBe(true);
  await expect(page.getByRole('region', { name: 'Prompteur' }).getByRole('alert')).toContainText('Le texte n’a pas pu être préparé');
});

test('empty scripts and unavailable projects cannot start playback', async ({ page, context }) => {
  await fixture(context, { empty: true });
  await page.goto('/project/prompter-test/prompter');
  await expect(page.getByText('Aucun texte à lire. Préparez votre script Facecam.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Lire', exact: true })).toBeDisabled();
  await context.route('**/facecam', route => route.fulfill({ status: 404, json: { detail: 'Projet introuvable.' } }));
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('Projet introuvable.');
  await expect(page.getByRole('button', { name: 'Réessayer' })).toBeVisible();
});

test('choosing another story stops the reader and clears the previous narration', async ({ page, context }) => {
  await fixture(context, { otherStory: true });
  const reader = await open(page);
  await page.getByRole('region', { name: 'Prompteur' }).getByRole('button', { name: 'Lire', exact: true }).click();
  await expect.poll(() => position(reader)).toBeGreaterThan(8);
  await page.getByRole('button', { name: 'Repartir de cette histoire' }).click();
  await expect(reader.getByText('Aucun texte à lire. Préparez votre script Facecam.')).toBeVisible();
  await expect(reader.getByRole('button', { name: 'Lire', exact: true })).toBeDisabled();
  await expect(reader.locator('.prompter-script')).not.toContainText('Imagine ce héros.');
});
