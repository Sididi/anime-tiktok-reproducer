import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Check, ChevronRight, Loader2, Video, RefreshCw, ShieldCheck, Upload, ExternalLink, X, Play, Scissors } from 'lucide-react';
import { Button } from '@/components/ui';
import { ProjectManagedVideoPlayer } from '@/components/video/ProjectManagedVideoPlayer';
import type { ManagedVideoPlayerHandle } from '@/components/video/ManagedVideoPlayer';
import { FacecamEditor } from '@/components/script/FacecamEditor';
import { FacecamStories } from '@/components/script/FacecamStories';
import { FacecamPrompter } from '@/components/script/FacecamPrompter';
import { FacecamAutoEditor } from '@/components/script/FacecamAutoEditor';
import { MetadataEditorModal } from '@/components/script/MetadataEditorModal';
import { DEFAULT_FACECAM_AUTO_EDITOR, FULL_STORY_ID, facecamRequest, uploadFacecam, type FacecamAutoEditorSettings, type FacecamBlock, type FacecamState, type Finding, type RecordingCut } from '@/api/facecam';
import { useFacecamPreview } from '@/hooks/useFacecamPreview';
import type { PlatformMetadata, Project } from '@/types';
import './facecam.css';

const FacecamCuts = lazy(() => import('@/components/script/FacecamCuts').then(module => ({default: module.FacecamCuts})));

interface Settings {llm_preset: string; music_key: string | null; models: {key: string; label: string}[]; music: {key: string; label: string}[]; auto_editor_defaults?: FacecamAutoEditorSettings}
interface Cover {id: number; title: {romaji: string; english: string | null}; coverImage: {extraLarge: string}}

export function FacecamScriptPage({project}: {project: Project}) {
  const id = project.id, navigate = useNavigate();
  const [state, setState] = useState<FacecamState | null>(null), stateRef = useRef<FacecamState | null>(null);
  const [error, setError] = useState(''), [working, setWorking] = useState(false), [saved, setSaved] = useState(true);
  const [hook, setHook] = useState(''), [opinion, setOpinion] = useState(''), [stage, setStage] = useState<'hooks' | 'opinions'>('hooks');
  const [finding, setFinding] = useState<Finding | null>(null), [covers, setCovers] = useState<Cover[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null), [upload, setUpload] = useState<number | null>(null);
  const [metadata, setMetadata] = useState<PlatformMetadata | null>(null), [metadataOpen, setMetadataOpen] = useState(false);
  const [delivery, setDelivery] = useState<{status: string} | null>(project.facecam_delivery ?? null);
  const [sourceOpen, setSourceOpen] = useState(false), [sourceLoaded, setSourceLoaded] = useState(false);
  const [cutsOpen, setCutsOpen] = useState(false);
  const closeCuts = useCallback(() => setCutsOpen(false), []);
  const recordingPlayer = useRef<HTMLVideoElement>(null);
  const sourcePlayer = useRef<ManagedVideoPlayerHandle>(null);
  const sourceToggle = useRef<HTMLButtonElement | null>(null);
  function toggleSource(trigger?: HTMLButtonElement) {
    if (sourceOpen) {
      sourcePlayer.current?.pause();
      setSourceOpen(false);
      (trigger ?? sourceToggle.current)?.focus({preventScroll: true});
    } else {
      sourceToggle.current = trigger ?? null;
      setSourceLoaded(true);
      setSourceOpen(true);
    }
  }
  const dialog = useRef<HTMLDialogElement>(null), draft = useRef<FacecamBlock[]>([]), editVersion = useRef(0), savedVersion = useRef(0);
  const savePromise = useRef<Promise<void> | null>(null), saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const currentRevision = state?.revision ?? 0;
  const approved = state?.approved_revision === currentRevision && !!state?.blocks.length && saved;
  const apply = useCallback((next: FacecamState) => {
    const dirty = editVersion.current !== savedVersion.current;
    if (stateRef.current && next.revision < stateRef.current.revision) return;
    if (next.recording_edit && !next.recording_edit.applied && next.recording_edit.id !== stateRef.current?.recording_edit?.id) setCutsOpen(true);
    const merged = dirty ? {...next, blocks: draft.current, findings: stateRef.current?.findings ?? []} : next;
    stateRef.current = merged; draft.current = merged.blocks; setState(merged);
    setFinding(current => current ? merged.findings.find(f => f.id === current.id) ?? null : null);
  }, []);
  const saveMusic = useCallback((music_key: string | null) => setSettings(previous => previous ? {...previous, music_key} : previous), []);
  const preview = useFacecamPreview({id, state, musicKey: settings?.music_key ?? null, settingsLoaded: !!settings,
    approved, working: working || cutsOpen, apply, saveMusic, onError: setError});
  const busy = working || preview.requestBusy || state?.job?.status === 'processing';
  const loadMetadata = useCallback(() => fetch(`/api/projects/${id}/metadata`).then(async response => {
    if (response.ok) {const result = await response.json(); setMetadata(result.metadata);}
  }), [id]);
  useEffect(() => {let cancelled = false;
    facecamRequest(id).then(next => {if (!cancelled) {apply(next); setHook(next.selected_hook); setOpinion(next.selected_opinion);}}).catch(e => setError(e.message));
    facecamRequest<Settings>(id, '/settings').then(setSettings).catch(e => setError(e.message));
    void loadMetadata();
    return () => {cancelled = true;};
  }, [id, apply, loadMetadata]);
  useEffect(() => {
    if (state?.job?.status !== 'processing') return;
    let source: EventSource | undefined, retry: ReturnType<typeof setTimeout> | undefined, stopped = false;
    function connect() {
      if (stopped) return;
      source = new EventSource(`/api/projects/${id}/facecam/events`);
      source.onmessage = event => {if (stopped) return; const next = JSON.parse(event.data) as FacecamState; apply(next); if (next.job?.status !== 'processing') {source?.close(); void loadMetadata();}};
      source.onerror = () => {
        source?.close();
        facecamRequest(id).then(next => {
          if (stopped) return;
          apply(next);
          if (next.job?.status === 'processing') retry = setTimeout(connect, 1000);
        }).catch(() => {if (!stopped) retry = setTimeout(connect, 1000);});
      };
    }
    connect();
    return () => {stopped = true; source?.close(); clearTimeout(retry);};
  }, [id, state?.job?.status, apply, loadMetadata]);
  useEffect(() => {const warn = (e: BeforeUnloadEvent) => {if (editVersion.current !== savedVersion.current) {e.preventDefault(); e.returnValue = '';}}; window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);}, []);
  useEffect(() => {if (cutsOpen) {recordingPlayer.current?.pause(); sourcePlayer.current?.pause();}}, [cutsOpen]);

  async function analyzeCuts() {
    setWorking(true);
    try {apply(await facecamRequest(id, '/recording/cuts/analyze', 'POST'));} finally {setWorking(false);}
  }
  async function openCuts() {
    setWorking(true);
    try {
      const latest = await facecamRequest(id); apply(latest); setCutsOpen(true);
      if (!latest.recording_edit) await analyzeCuts();
    } catch (e) {setError((e as Error).message); setCutsOpen(false);}
    finally {setWorking(false);}
  }
  async function applyCuts(cuts: RecordingCut[]) {
    const snapshot = stateRef.current;
    if (!snapshot?.recording_edit) return;
    setWorking(true);
    try {apply(await facecamRequest(id, '/recording/cuts/apply', 'POST', {revision: snapshot.revision,
      edit_id: snapshot.recording_edit.id, version: snapshot.recording_edit.version, cuts}));}
    finally {setWorking(false);}
  }

  async function flush(): Promise<void> {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    if (savePromise.current) await savePromise.current;
    if (editVersion.current === savedVersion.current || !stateRef.current) return;
    const version = editVersion.current, snapshot = draft.current, revision = stateRef.current.revision;
    const findings = stateRef.current.findings;
    const operation = (async () => {
      const next = await facecamRequest(id, '/draft', 'PUT', {revision, blocks: snapshot, selected_hook: hook, selected_opinion: opinion,
        finding_ranges: findings.every(f => f.id) ? findings.map(({id, start, end}) => ({id, start, end})) : undefined});
      savedVersion.current = version; apply(next); setSaved(editVersion.current === version);
    })();
    savePromise.current = operation;
    try {await operation;} finally {savePromise.current = null;}
    if (editVersion.current !== savedVersion.current) await flush();
  }
  function edit(blocks: FacecamBlock[], findings: Finding[]) {
    draft.current = blocks; editVersion.current++; setSaved(false);
    setFinding(current => current ? findings.find(f => f.id === current.id) ?? null : null);
    if (stateRef.current) {
      stateRef.current = {...stateRef.current, blocks, findings};
      setState(stateRef.current);
    }
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {void flush().catch(e => setError(e.message));}, 700);
  }
  async function action(path: string, body?: Record<string, unknown>) {
    setWorking(true); setError('');
    try {await flush(); const next = await facecamRequest(id, path, 'POST', body ? {...body, revision: stateRef.current?.revision} : undefined); apply(next);}
    catch (e) {setError((e as Error).message);} finally {setWorking(false);}
  }
  async function persistChoices() {apply(await facecamRequest(id, '/selection', 'PUT', {revision: stateRef.current?.revision, hook, opinion}));}
  async function closeChoices() {try {await persistChoices(); dialog.current?.close();} catch(e) {setError((e as Error).message);}}
  async function chooseNext() {setError(''); try {await persistChoices(); setStage('opinions'); if (!state?.opinions.length) await action('/candidates/opinions');} catch(e) {setError((e as Error).message);}}
  async function generate() {dialog.current?.close(); await action('/generate', {hook, opinion});}
  async function selectStory(storyId: string) {
    setWorking(true); setError('');
    try {
      await flush();
      const next = await facecamRequest(id, '/story', 'PUT', {revision: stateRef.current?.revision, story_id: storyId});
      apply(next); setHook(next.selected_hook); setOpinion(next.selected_opinion); setFinding(null);
    } catch (e) {setError((e as Error).message);} finally {setWorking(false);}
  }
  async function importFile(file: File, path: string) {setError(''); setUpload(0); setWorking(true); try {await flush(); apply(await uploadFacecam(id, file, path, setUpload));} catch(e) {setError((e as Error).message);} finally {setUpload(null); setWorking(false);}}
  async function updateSetting(key: 'llm_preset' | 'music_key', value: string) {
    setError(''); setWorking(true);
    try {setSettings(await facecamRequest<Settings>(id, '/settings', 'PUT', {[key]: value}));}
    catch(e) {setError((e as Error).message);} finally {setWorking(false);}
  }
  if (!state) return <div className="p-8">{error || 'Chargement de Facecam…'}</div>;
  const visibleFindings = state.findings;
  const options = stage === 'hooks' ? state.hooks : state.opinions;
  const selection = stage === 'hooks' ? hook : opinion;
  const select = stage === 'hooks' ? setHook : setOpinion;
  const previewReady = preview.previewReady;
  const cutsPending = !!state.recording_edit && !state.recording_edit.applied;
  const storyReady = !!state.selected_story_id || (!state.stories?.length && state.blocks.length > 0);
  const wordCount = state.blocks.filter(b => b.kind !== 'raw').reduce((n, b) => n + (b.text.trim() ? b.text.trim().split(/\s+/).length : 0), 0);
  return <main className="facecam-page">
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div><div className="facecam-eyebrow"><Video size={15}/> FACECAM · FRANÇAIS · MINIMAL</div><h1 className="text-3xl font-semibold mt-2">{project.anime_name}</h1><p className="text-sm opacity-60 mt-2">Votre texte, votre voix, votre regard.</p></div>
      <div className="flex flex-wrap gap-2"><Button variant="outline" aria-expanded={sourceOpen} aria-controls="facecam-source-video" onClick={e => toggleSource(e.currentTarget)}><Play size={16} className="mr-2"/>Vidéo originale</Button><Button disabled={busy || !previewReady} onClick={() => navigate(`/project/${id}/processing`)}>Passer au traitement<ChevronRight size={16} className="ml-2"/></Button></div>
    </header>
    <ol className="facecam-steps">{['Histoire', 'Angle', 'Script', 'Enregistrement', 'Traitement'].map((label, i) => <li key={label} className={i < (previewReady ? 4 : approved ? 3 : state.blocks.length ? 2 : storyReady ? 1 : 0) ? 'done' : ''}><span>{i + 1}</span>{label}</li>)}</ol>
    {error && <div role="alert" className="facecam-error">{error}<button onClick={() => setError('')} aria-label="Fermer l’erreur">×</button></div>}
    {state.job && <div className={`facecam-status ${state.job.status === 'error' ? 'facecam-error' : ''}`} role="status">{state.job.status === 'processing' ? <Loader2 size={17} className="animate-spin"/> : state.job.status === 'complete' ? <Check size={17}/> : null}{state.job.message || state.job.step}</div>}
    <div className={`facecam-workspace ${sourceOpen ? 'facecam-workspace-with-video' : ''}`}>
      <aside id="facecam-source-video" className="facecam-source" aria-label="Vidéo originale" hidden={!sourceOpen}>
        <header><h2>Vidéo originale</h2><Button variant="ghost" size="icon" aria-label="Masquer la vidéo originale" onClick={() => toggleSource()}><X size={18}/></Button></header>
        {sourceLoaded && <ProjectManagedVideoPlayer ref={sourcePlayer} projectId={id} className="facecam-source-player" muted={false} controls/>}
        <p>Gardez la vidéo sous les yeux pendant l’écriture.</p>
      </aside>
      <div className="facecam-workspace-content">
    <section className="facecam-card"><h2>Modèle de rédaction</h2><p>Choisissez le modèle, puis lancez l’analyse des histoires. Ce choix s’applique aux prochaines générations.</p><div className="facecam-settings">{settings && <label>Modèle<select aria-label="Modèle" disabled={busy} value={settings.llm_preset} onChange={e => void updateSetting('llm_preset', e.target.value)}>{settings.models.map(m => <option key={m.key} value={m.key}>{m.label}</option>)}</select></label>}</div></section>
    <FacecamStories state={state} busy={!!busy || !settings} onAnalyze={() => void action('/stories/analyze', {})} onSelect={storyId => void selectStory(storyId)}/>
    <section className="facecam-card">
      <div className="flex flex-wrap justify-between items-center gap-3"><div><h2>2. Choisir votre angle</h2><p>{storyReady ? 'Une accroche pour cette histoire, un point de vue facultatif.' : 'Choisissez d’abord l’histoire à raconter.'}</p></div><Button variant="outline" disabled={busy || !storyReady} onClick={() => {setStage('hooks'); dialog.current?.showModal();}}>Choisir l’accroche et le point de vue</Button></div>
      {(hook || opinion) && <div className="facecam-choice-summary"><blockquote><small>ACCROCHE</small>{hook || 'À choisir'}</blockquote><blockquote><small>POINT DE VUE</small>{opinion.trim() || 'Sans point de vue'}</blockquote></div>}
      {!!hook.trim() && <Button disabled={busy || !storyReady} onClick={() => {if (state.blocks.length && !window.confirm('Remplacer le brouillon actuel par une nouvelle génération ?')) return; void generate();}}>{state.blocks.length ? 'Régénérer le script' : 'Générer mon script'}</Button>}
    </section>
    {state.blocks.length > 0 && <section className="facecam-card">
      <div className="flex flex-wrap justify-between items-center gap-3 mb-5"><div><h2>3. Affiner votre script</h2><p>{saved ? 'Brouillon enregistré' : 'Enregistrement…'} · Révision {state.revision}</p></div><div className="flex flex-wrap gap-2"><Button variant="outline" aria-expanded={sourceOpen} aria-controls="facecam-source-video" onClick={e => toggleSource(e.currentTarget)}><Play size={16} className="mr-2"/>Vidéo originale</Button><Button variant="outline" disabled={busy || !saved} onClick={() => void action('/fact-check')}><ShieldCheck size={16} className="mr-2"/>Vérifier les faits</Button><Button disabled={busy || approved || !storyReady} onClick={() => void action('/approve', {})}>{approved ? 'Script validé' : 'Valider le script'}</Button></div></div>
      <p className="facecam-duration">{wordCount} mots · {state.selected_story_id === FULL_STORY_ID ? 'Histoire intégrale · Durée conseillée : 65–70 s' : 'Cible : 65–70 s'}
        {saved && state.estimated_duration_seconds != null && <> · Estimation : {Math.round(state.estimated_duration_seconds)} s, à confirmer avec votre lecture.
          {state.estimated_duration_seconds > 70 ? state.selected_story_id === FULL_STORY_ID ? ' Le récit intégral dépasse la durée conseillée. Privilégiez une histoire plus courte pour garder l’attention.' : ' Vous pouvez condenser le texte à la main ; cette estimation ne bloque pas la validation.' : state.estimated_duration_seconds < 65 ? ' Une lecture est nécessaire pour vérifier que le montage dépasse une minute.' : ''}</>}
      </p>
      <div className="facecam-editor-layout"><FacecamEditor blocks={state.blocks} findings={visibleFindings} disabled={!!busy} onChange={edit} onFinding={setFinding}/>
        <aside className="facecam-review" aria-label="Vérification des faits"><h3>Vérification des faits</h3>
          {state.fact_status === 'unavailable' ? <p role="alert">Vérification indisponible. {state.fact_error}</p> : state.fact_revision == null ? <p>Ce texte n’a pas encore été vérifié.</p> : <p>{visibleFindings.length ? `${visibleFindings.length} point(s) à examiner.` : state.fact_revision !== state.revision || !saved ? 'Aucun signalement restant.' : 'Aucune anomalie signalée par la recherche.'} Les corrections restent à votre appréciation.
            {(state.fact_revision !== state.revision || !saved) && <> Les passages modifiés n’ont pas été revérifiés ; les autres signalements sont conservés.</>}</p>}
          {state.fact_context && <details className="facecam-evidence-context"><summary>Sous-titres transmis à la vérification</summary><p>Horaires dans les épisodes sources · marge de {state.fact_context.padding_seconds} s. Les dialogues ne permettent pas de vérifier tous les détails visuels.</p>
            {state.fact_context.episodes.length === 0 && <p>Aucun épisode associé disponible. Vérification web uniquement.</p>}
            {state.fact_context.episodes.map((episode, i) => <details key={i}><summary>{episode.episode} · {episode.cues.length} réplique(s){episode.language ? ` · ${episode.language}` : ''}</summary>
              <p>{episode.windows.map(w => `${w.start}–${w.end}`).join(', ')}</p>{episode.note && <p role="note">{episode.note}</p>}
              <div className="facecam-evidence-cues">{episode.cues.map(c => <p key={c.id}><a href={c.source_url} target="_blank" rel="noreferrer">{c.start}–{c.end}</a><br/>{c.text}</p>)}</div>
            </details>)}
          </details>}
          {visibleFindings.map((f, i) => <button key={f.id ?? i} className={`facecam-fact-item ${f.kind}`} onClick={() => setFinding(f)}>{f.kind === 'contradiction' ? 'À corriger' : 'À vérifier'} — {f.quote}</button>)}
          {finding && <div className="facecam-fact-detail"><strong>{finding.quote}</strong><p>{finding.explanation}</p>{finding.correction && <p><b>Suggestion :</b> {finding.correction}</p>}{finding.sources.map(s => <div key={s.url}><a href={s.url} target="_blank" rel="noreferrer">{s.title}<ExternalLink size={12}/></a>{s.kind === 'subtitle' && s.text && <blockquote>{s.text}</blockquote>}</div>)}<button onClick={() => setFinding(null)} className="underline mt-3">Fermer</button></div>}
        </aside></div>
    </section>}
    <section className="facecam-card" hidden={!state.blocks.length}><h2>4. Votre enregistrement</h2><p>Lisez uniquement votre narration, sans les extraits audio originaux ni le titre final. Vous pourrez retirer les ratés après l’import. Les silences seront coupés et les extraits réinsérés automatiquement.</p>
      <FacecamPrompter projectId={id} title={project.anime_name || 'Facecam'} revision={state.revision} blocks={state.blocks} saved={saved} busy={!!busy} prepare={flush}/>
      <label className={`facecam-upload ${!approved || busy ? 'disabled' : ''}`}><Upload size={24}/><span>{state.recording ? 'Remplacer l’enregistrement' : 'Importer votre vidéo'}<small>MP4 ou MOV avec son</small></span><input type="file" accept="video/mp4,video/quicktime,.mp4,.mov" disabled={!approved || busy} onChange={e => {const file = e.target.files?.[0]; if(file) void importFile(file, 'recording'); e.target.value = '';}}/></label>
      {!approved && <p>Validez votre script avant d’importer une vidéo.</p>}{upload !== null && <progress max={100} value={upload} aria-label="Envoi du fichier"/>}
      {state.recording && <div className="facecam-recording"><video ref={recordingPlayer} controls src={`/api/projects/${id}/facecam/recording?v=${state.recording.fingerprint}`} preload="metadata"/>
        <div><Button variant="outline" disabled={working || preview.requestBusy || (!!busy && !state.recording_edit)} onClick={() => void openCuts()}><Scissors size={16} className="mr-2"/>{cutsPending ? 'Vérifier les coupes' : 'Modifier les coupes'}</Button>
          {cutsPending ? <p>Vérifiez les reprises et validez votre prise avant la transcription définitive.</p> : <><h3 className="mt-4">Texte enregistré</h3><p className="max-h-48 overflow-auto">{state.recording.transcript}</p>
          {state.recording.differences.length > 0 && <details open={state.recording.reviewed_revision !== state.revision}><summary>{state.recording.differences.length} différence(s) à vérifier</summary><ul>{state.recording.differences.map((d,i) => <li key={i}><del>{d.expected || '(ajout)'}</del><ins>{d.spoken || '(omis)'}</ins></li>)}</ul></details>}
          {state.recording.reviewed_revision !== state.revision && <Button variant="outline" disabled={busy || !approved} onClick={() => void action('/recording/review', {})}>J’ai vérifié les différences</Button>}
          <p className="text-sm">Modifiez le script si votre lecture a changé, ou acceptez les écarts de transcription après écoute.</p></>}
        </div></div>}
      {state.recording && !cutsPending && <>
        <FacecamAutoEditor value={preview.tuning} defaults={settings?.auto_editor_defaults ?? DEFAULT_FACECAM_AUTO_EDITOR} disabled={preview.controlsDisabled}
          pending={preview.pending} automatic={preview.enabled} onChange={preview.changeTuning}/>
        <div className="mt-5 flex flex-wrap items-center gap-4"><Button disabled={busy || !settings || !approved || state.recording.reviewed_revision !== state.revision} onClick={preview.prepare}>Préparer l’aperçu audio</Button>{previewReady && <audio key={state.prepared?.preview_version} controls aria-label="Aperçu audio Facecam" src={`/api/projects/${id}/facecam/preview?r=${state.revision}&v=${state.prepared?.preview_version}`} preload="metadata"/>}</div>
      </>}
      {previewReady && state.prepared?.cut_report && <p>{state.prepared.cut_report.removed_seconds.toFixed(1)} s de pauses retirées · {state.prepared.cut_report.cut_count} coupes · {state.prepared.cut_report.protected_word_count} mots protégés.</p>}
      {previewReady && state.prepared?.duration_seconds != null && <p>Durée montée, carte finale comprise : {state.prepared.duration_seconds.toFixed(1)} s.
        {state.prepared.duration_seconds <= 60 ? ' Le montage ne dépasse pas encore une minute : ajustez le récit et enregistrez une nouvelle prise.' : state.prepared.duration_seconds > 70 ? ' Le montage dépasse la cible de 65–70 s.' : ''}</p>}
    </section>
    <section className="facecam-card"><h2>Finition et publication</h2><div className="facecam-settings">
      {settings && <><label>Musique<select aria-label="Musique" disabled={preview.controlsDisabled} value={preview.selectedMusic || ''} onChange={e => preview.changeMusic(e.target.value)}>{settings.music.map(m => <option key={m.key} value={m.key}>{m.label}</option>)}</select></label><p>La musique est incluse dans l’aperçu audio.</p></>}
      <div><h3>Couverture finale</h3><p>Récupérée automatiquement au traitement. Vous pouvez aussi la choisir ici.</p><div className="flex gap-3 flex-wrap"><Button variant="outline" disabled={busy} onClick={() => {setError(''); facecamRequest<Cover[]>(id, '/covers').then(setCovers).catch(e => setError(e.message));}}>Choisir la couverture</Button><label className="facecam-file-button">Importer une image<input type="file" accept="image/jpeg,image/png,image/webp" disabled={busy} onChange={e => {const file=e.target.files?.[0]; if(file) void importFile(file,'cover');}}/></label></div>
      {state.cover && <img className="facecam-cover-preview" src={`/api/projects/${id}/facecam/cover?v=${JSON.stringify(state.cover)}`} alt="Couverture et titre de l’anime"/>}</div>
    </div>
    {!!covers.length && <div className="facecam-covers">{covers.map(c => <button key={c.id} disabled={busy} onClick={() => {setCovers([]); void action(`/covers/${c.id}`);}}><img src={c.coverImage.extraLarge} alt=""/><span>{c.title.english || c.title.romaji}</span></button>)}</div>}
    <div className="flex flex-wrap gap-3 mt-5"><Button variant="outline" disabled={busy || !approved} onClick={() => void action('/metadata/generate')}>{metadata ? 'Régénérer les métadonnées' : 'Générer les métadonnées'}</Button>{metadata && <Button variant="outline" onClick={() => setMetadataOpen(true)}>Modifier les métadonnées</Button>}
    {project.drive_folder_id && <><Button variant="outline" onClick={() => facecamRequest<{status:string}>(id, '/delivery').then(setDelivery).catch(e=>setError(e.message))}><RefreshCw size={14} className="mr-2"/>Actualiser la livraison</Button><span>{delivery?.status === 'complete' ? 'Montage terminé' : 'Montage en attente'}</span>{delivery?.status === 'complete' && <Button variant="outline" onClick={() => {if(window.confirm('Archiver le montage livré et demander une nouvelle version ?')) facecamRequest<{status:string}>(id,'/delivery/reopen','POST').then(setDelivery).catch(e=>setError(e.message));}}>Demander un nouveau montage</Button>}</>}
    </div></section>
      </div>
    </div>
    <dialog ref={dialog} className="facecam-dialog facecam-candidates" onCancel={e => {e.preventDefault(); if(!busy) void closeChoices();}}><header className="flex justify-between items-center"><div><small>{stage === 'hooks' ? 'ÉTAPE 1 / 2' : 'ÉTAPE 2 / 2'}</small><h2>{stage === 'hooks' ? 'Choisissez votre accroche' : 'Quel est votre point de vue ?'}</h2></div><button aria-label="Fermer" disabled={busy} onClick={() => void closeChoices()}>×</button></header>
      <p className="my-3 text-sm opacity-60">{stage === 'opinions' ? 'Le point de vue est facultatif. Choisissez une proposition ou continuez sans avis personnel.' : 'Sélectionnez une proposition, puis ajustez librement sa formulation.'}</p>
      {stage === 'opinions' && <button type="button" className="facecam-skip-opinion" aria-pressed={!opinion.trim()} disabled={busy} onClick={() => setOpinion('')}><strong>Sans point de vue</strong><span>Passer directement du récit à la révélation du titre.</span></button>}
      <div className="facecam-candidate-grid">{options.map((c, i) => <button key={c.id} aria-pressed={selection === c.text} disabled={busy} onClick={() => select(c.text)}><small>{c.original ? 'ORIGINALE · TRADUITE' : `OPTION ${i + 1}`}</small>{c.text}</button>)}</div>
      <Button variant="outline" className="my-4" disabled={busy} onClick={() => void action(`/candidates/${stage}`)}>{busy ? <Loader2 size={16} className="animate-spin mr-2"/> : <RefreshCw size={16} className="mr-2"/>}{options.length ? 'Proposer 8 nouvelles variantes' : 'Proposer 8 variantes'}</Button>
      <label className="block">{stage === 'opinions' ? 'Votre formulation (facultatif)' : 'Votre formulation'}<textarea rows={3} value={selection} disabled={busy} placeholder={stage === 'opinions' ? 'Laissez vide pour ne pas ajouter de point de vue.' : undefined} onChange={e => select(e.target.value)} className="facecam-textarea"/></label>
      {error && <p role="alert" className="text-red-400">{error}</p>}{state.job?.status === 'error' && <p role="alert" className="text-red-400">{state.job.message}</p>}
      <footer className="flex flex-wrap gap-3 justify-between mt-5">{stage === 'opinions' ? <Button variant="outline" disabled={busy} onClick={() => setStage('hooks')}>Retour aux accroches</Button> : <span/>}<Button disabled={busy || !hook.trim()} onClick={() => void (stage === 'hooks' ? chooseNext() : generate())}>{stage === 'hooks' ? 'Choisir le point de vue' : 'Générer mon script'}</Button></footer>
    </dialog>
    {cutsOpen && state.recording_edit && <Suspense fallback={<p role="status">Ouverture du montage…</p>}><FacecamCuts key={state.recording_edit.id} projectId={id} edit={state.recording_edit} busy={!!busy}
      job={state.job} onClose={closeCuts} onApply={applyCuts} onAnalyze={analyzeCuts}/></Suspense>}
    {metadata && <MetadataEditorModal isOpen={metadataOpen} onClose={() => setMetadataOpen(false)} metadata={metadata} onSave={value => {facecamRequest(id, '/metadata', 'PUT', value).then(() => {setMetadata(value); setMetadataOpen(false);}).catch(e => setError(e.message));}}/>}
  </main>;
}
