import {useCallback, useEffect, useRef, useState} from 'react';
import WaveSurfer from 'wavesurfer.js';
import Regions, {type Region} from 'wavesurfer.js/dist/plugins/regions.esm.js';
import Timeline from 'wavesurfer.js/dist/plugins/timeline.esm.js';
import {Check, Loader2, Pause, Play, Scissors, Trash2, Undo2, X} from 'lucide-react';
import {Button} from '@/components/ui';
import {facecamRequest, type FacecamState, type RecordingCut, type RecordingCutData, type RecordingEdit} from '@/api/facecam';
import {cutTime, removedIntervals} from '@/utils/facecamCuts';
import './FacecamCuts.css';

type Draft = {cuts: RecordingCut[]; seen: string[]};
const asCut = ({id, start, end, enabled, origin}: RecordingCut): RecordingCut => ({id, start, end, enabled, origin});

function initialDraft(edit: RecordingEdit, key: string): Draft {
  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null');
    if (saved?.version === edit.version && Array.isArray(saved.cuts) && Array.isArray(saved.seen)
      && saved.cuts.every((c: RecordingCut) => typeof c.id === 'string' && Number.isFinite(c.start) && Number.isFinite(c.end)
        && c.start >= 0 && c.end > c.start && c.end <= edit.duration)) return saved;
  } catch { /* A browser may disable local storage. Server-validated cuts remain available. */ }
  const cuts = edit.applied ? edit.cuts : edit.suggestions.map(asCut);
  // Reopening an applied edit must also show declined proposals, unchecked.
  return {cuts: [...cuts, ...edit.suggestions.filter(s => !cuts.some(c => c.id === s.id))
    .map(s => ({...asCut(s), enabled: false}))], seen: edit.suggestions.map(s => s.id)};
}

export function FacecamCuts({projectId, edit, busy, job, onClose, onApply, onAnalyze}: {
  projectId: string; edit: RecordingEdit; busy: boolean; job: FacecamState['job']; onClose: () => void;
  onApply: (cuts: RecordingCut[]) => Promise<void>; onAnalyze: () => Promise<void>;
}) {
  const storageKey = `facecam-cuts:${projectId}:${edit.id}`;
  const [draft, setDraft] = useState<Draft>(() => initialDraft(edit, storageKey));
  const [history, setHistory] = useState<RecordingCut[][]>([]);
  const [data, setData] = useState<RecordingCutData | null>(null), [loadVersion, setLoadVersion] = useState(0);
  const [error, setError] = useState(''), [loadError, setLoadError] = useState('');
  const [active, setActive] = useState<string | null>(null), [selecting, setSelecting] = useState(false);
  const [waveReady, setWaveReady] = useState(false), [zoom, setZoom] = useState(1);
  const [time, setTime] = useState(0), [playing, setPlaying] = useState(false);
  const [marker, setMarker] = useState<number | null>(null), [mode, setMode] = useState<'original' | 'cut'>('cut');
  const [submitted, setSubmitted] = useState<number | null>(null);
  const [requestPending, setRequestPending] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null), video = useRef<HTMLVideoElement>(null), container = useRef<HTMLDivElement>(null);
  const wave = useRef<WaveSurfer | null>(null), regions = useRef<Regions | null>(null), syncing = useRef(false);
  const zoomAnchor = useRef<{time: number; x: number} | null>(null);
  const draftRef = useRef(draft), applyingRef = useRef(false);
  const audition = useRef<{mode: 'original' | 'cut'; stop?: number; cuts?: RecordingCut[]}>({mode: 'cut'});
  const applying = requestPending || (submitted !== null && job?.status !== 'error');
  const selected = draft.cuts.find(c => c.id === active);
  const intervals = removedIntervals(draft.cuts), removed = intervals.reduce((n, c) => n + c.end - c.start, 0);
  const suggestions = new Map(edit.suggestions.map(s => [s.id, s]));
  const ordered = [...draft.cuts].sort((a, b) => a.start - b.start);
  const sourceUrl = `/api/projects/${projectId}/facecam/recording/source?edit_id=${encodeURIComponent(edit.id)}`;
  const addedSuggestions = edit.suggestions.filter(s => !draft.seen.includes(s.id));
  if (addedSuggestions.length) {
    const added = addedSuggestions.map(asCut);
    setDraft({cuts: [...draft.cuts, ...added], seen: [...draft.seen, ...addedSuggestions.map(s => s.id)]});
    setHistory(previous => previous.map(cuts => [...cuts, ...added]));
  }

  useEffect(() => {draftRef.current = draft; applyingRef.current = applying;}, [draft, applying]);
  useEffect(() => {
    try {localStorage.setItem(storageKey, JSON.stringify({...draft, version: edit.version}));} catch { /* Optional local draft. */ }
  }, [storageKey, draft, edit.version]);
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => {node?.close();};
  }, []);
  useEffect(() => {
    if (submitted !== null && edit.version > submitted) {
      try {localStorage.removeItem(storageKey);} catch { /* Optional local draft. */ }
      onClose();
    }
  }, [submitted, edit.version, storageKey, onClose]);
  useEffect(() => {
    let cancelled = false;
    facecamRequest<RecordingCutData>(projectId, `/recording/cuts?edit_id=${encodeURIComponent(edit.id)}`)
      .then(next => {if (!cancelled) {setLoadError(''); setData(previous => ({...next, peaks: previous?.peaks ?? next.peaks}));}})
      .catch(e => {if (!cancelled) setLoadError(e.message);});
    return () => {cancelled = true;};
  }, [projectId, edit.id, edit.analysis_status, loadVersion]);

  const commit = useCallback((cuts: RecordingCut[]) => {
    if (applyingRef.current) return;
    setHistory(previous => [...previous.slice(-49), draftRef.current.cuts]);
    setDraft(previous => ({...previous, cuts}));
    setError('');
  }, []);
  const add = useCallback((start: number, end: number) => {
    if (end - start < 1 / 60) {setError('Placez la fin après le début de la coupe.'); return;}
    const cut: RecordingCut = {id: `manual-${crypto.randomUUID()}`, start, end, enabled: true, origin: 'manual'};
    commit([...draftRef.current.cuts, cut]); setActive(cut.id); setMarker(null); setSelecting(false);
  }, [commit]);
  const undo = useCallback(() => {
    if (applyingRef.current) return;
    setHistory(previous => {
      const last = previous.at(-1);
      if (last) setDraft(current => ({...current, cuts: last}));
      return previous.slice(0, -1);
    });
  }, []);

  const peaks = data?.peaks, duration = data?.duration;
  useEffect(() => {
    if (!peaks || !duration || !container.current || !video.current) return;
    const plugin = Regions.create();
    const instance = WaveSurfer.create({container: container.current, media: video.current,
      peaks: [peaks], duration, height: 112, waveColor: '#718096', progressColor: '#b8b5f8',
      cursorColor: '#fff', cursorWidth: 2, normalize: true, autoCenter: false,
      plugins: [plugin, Timeline.create({height: 22, style: {fontSize: '10px', color: '#94a3b8'}})]});
    wave.current = instance; regions.current = plugin;
    const update = (region: Region) => {
      if (syncing.current || applyingRef.current) return;
      const previous = draftRef.current.cuts.find(c => c.id === region.id);
      const cut: RecordingCut = {...previous, id: region.id, start: region.start, end: region.end,
        enabled: previous?.enabled ?? true, origin: previous?.origin ?? 'manual'};
      commit(previous ? draftRef.current.cuts.map(c => c.id === region.id ? cut : c) : [...draftRef.current.cuts, cut]);
      setActive(region.id); setSelecting(false);
    };
    plugin.on('region-created', update);
    plugin.on('region-updated', update);
    plugin.on('region-clicked', (region, event) => {event.stopPropagation(); setActive(region.id); if (video.current) video.current.currentTime = region.start;});
    instance.on('ready', () => setWaveReady(true));
    instance.on('interaction', () => {audition.current.stop = undefined; audition.current.cuts = undefined;});
    instance.on('error', e => setLoadError(`Lecture indisponible : ${e.message}`));
    return () => {instance.destroy(); wave.current = null; regions.current = null; setWaveReady(false);};
  }, [peaks, duration, commit]);
  useEffect(() => {
    if (!waveReady || !regions.current) return;
    syncing.current = true;
    regions.current.clearRegions();
    for (const cut of draft.cuts) {
      const region = regions.current.addRegion({id: cut.id, start: cut.start, end: cut.end,
        color: cut.enabled ? '#ef777744' : '#d2b76c18', minLength: 1 / 60, drag: !applying, resize: !applying});
      if (region.element) {
        region.element.style.border = `1px ${cut.enabled ? 'solid' : 'dashed'} ${cut.id === active ? '#fff' : cut.enabled ? '#ef9999' : '#a99c75'}`;
        region.element.title = cut.enabled ? 'Passage à retirer' : 'Proposition conservée';
      }
    }
    syncing.current = false;
  }, [draft.cuts, active, waveReady, applying]);
  useEffect(() => {
    if (waveReady && selecting && !applying) return regions.current?.enableDragSelection({color: '#ef777744', minLength: 1 / 60});
  }, [selecting, waveReady, applying]);
  useEffect(() => {
    const instance = wave.current;
    if (!waveReady || !instance) return;
    const pixelsPerSecond = instance.getWidth() / edit.duration * zoom;
    instance.zoom(pixelsPerSecond);
    if (zoomAnchor.current) {
      instance.setScroll(zoomAnchor.current.time * pixelsPerSecond - zoomAnchor.current.x);
      zoomAnchor.current = null;
    }
  }, [zoom, waveReady, edit.duration]);
  useEffect(() => {
    const node = container.current, instance = wave.current;
    if (!waveReady || !node || !instance) return;
    const onWheel = (event: WheelEvent) => {
      if (Math.abs(event.deltaX) >= Math.abs(event.deltaY) || event.shiftKey) return;
      event.preventDefault();
      const x = Math.max(0, Math.min(instance.getWidth(), event.clientX - instance.getWrapper().getBoundingClientRect().left - instance.getScroll()));
      const pixelsPerSecond = instance.getWrapper().scrollWidth / edit.duration;
      zoomAnchor.current = {time: (instance.getScroll() + x) / pixelsPerSecond, x};
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? node.clientHeight : 1);
      setZoom(current => Math.max(1, Math.min(20, current * Math.exp(-delta * .002))));
    };
    node.addEventListener('wheel', onWheel, {passive: false});
    return () => node.removeEventListener('wheel', onWheel);
  }, [waveReady, edit.duration]);

  // rAF checks cut boundaries every display frame; timeupdate alone can lag 250 ms.
  useEffect(() => {
    const media = video.current;
    if (!media) return;
    let frame = 0, lastDisplay = 0;
    const tick = () => {
      const preview = audition.current;
      if (!media.paused && !media.seeking && preview.mode === 'cut') {
        const range = removedIntervals(preview.cuts ?? draftRef.current.cuts).find(c => media.currentTime >= c.start && media.currentTime < c.end);
        if (range) media.currentTime = range.end;
      }
      if (preview.stop !== undefined && media.currentTime >= preview.stop) media.pause();
      if (performance.now() - lastDisplay > 80) {setTime(media.currentTime); lastDisplay = performance.now();}
      if (!media.paused) frame = requestAnimationFrame(tick);
    };
    const start = () => {cancelAnimationFrame(frame); setPlaying(true); frame = requestAnimationFrame(tick);};
    const stop = () => {cancelAnimationFrame(frame); setPlaying(false);};
    media.addEventListener('play', start); media.addEventListener('pause', stop);
    return () => {cancelAnimationFrame(frame); media.removeEventListener('play', start); media.removeEventListener('pause', stop); media.pause();};
  }, []);

  function play(start?: number, stop?: number, cut?: RecordingCut, playbackMode = mode) {
    const media = video.current;
    if (!media) return;
    audition.current = {mode: playbackMode, stop, cuts: cut ? [...draftRef.current.cuts.filter(c => c.id !== cut.id), {...cut, enabled: true}] : undefined};
    setMode(playbackMode);
    if (start !== undefined) media.currentTime = start;
    if (playbackMode === 'cut') {
      const range = removedIntervals(audition.current.cuts ?? draftRef.current.cuts).find(c => media.currentTime >= c.start && media.currentTime < c.end);
      if (range) media.currentTime = range.end;
    }
    void media.play().catch(() => setError('La vidéo ne peut pas être lue. Réessayez après son chargement.'));
  }
  function listen(cut: RecordingCut, playbackMode: 'original' | 'cut') {
    setActive(cut.id);
    const suggestion = suggestions.get(cut.id);
    const nextWord = data?.words.find(w => w.start >= cut.end + 2.5);
    const start = playbackMode === 'original' ? cut.start : Math.max(0, cut.start - 1.5);
    const stop = playbackMode === 'original' ? cut.end : Math.min(edit.duration, suggestion ? nextWord?.end ?? cut.end + 4 : cut.end + 2);
    play(start, stop, cut, playbackMode);
    wave.current?.setScrollTime(Math.max(0, start - .5));
  }
  function togglePlayback() {
    if (video.current?.paused) play();
    else video.current?.pause();
  }
  function changeBound(key: 'start' | 'end', value: number) {
    if (!selected || !Number.isFinite(value)) return;
    const cut = {...selected, [key]: value};
    if (cut.start < 0 || cut.end > edit.duration || cut.end - cut.start < 1 / 60) {
      setError('La fin doit suivre le début et rester dans la vidéo.'); return;
    }
    commit(draft.cuts.map(c => c.id === cut.id ? cut : c));
  }
  async function submit() {
    video.current?.pause(); setError(''); setSubmitted(edit.version); setRequestPending(true);
    try {await onApply(draft.cuts);} catch (e) {setSubmitted(null); setError((e as Error).message);}
    finally {setRequestPending(false);}
  }

  return <dialog ref={dialog} className="facecam-dialog facecam-cuts" aria-labelledby="facecam-cuts-title"
    onCancel={e => {e.preventDefault(); if (!applying) onClose();}}
    onKeyDown={e => {
      const target = e.target as HTMLElement;
      if (target.closest('input, textarea, select, [contenteditable=true]') || applying) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {e.preventDefault(); undo();}
      else if (e.code === 'Space' && !target.closest('button, video')) {e.preventDefault(); togglePlayback();}
      else if (e.key.toLowerCase() === 'i') {e.preventDefault(); setMarker(video.current?.currentTime ?? 0);}
      else if (e.key.toLowerCase() === 'o' && marker !== null) {e.preventDefault(); add(marker, video.current?.currentTime ?? 0);}
      else if ((e.key === 'Delete' || e.key === 'Backspace') && active) {e.preventDefault(); commit(draft.cuts.map(c => c.id === active ? {...c, enabled: false} : c));}
    }}>
    <header className="facecam-cuts-header"><div><span className="facecam-cuts-eyebrow">AVANT LA TRANSCRIPTION FINALE</span>
      <h2 id="facecam-cuts-title"><Scissors size={19}/> Nettoyer votre prise</h2>
      <p>Retirez les ratés, gardez la bonne reprise. Les silences seront traités ensuite.</p></div>
      <Button variant="ghost" size="icon" disabled={applying} aria-label="Fermer le montage" onClick={onClose}><X size={20}/></Button>
    </header>
    <div className="facecam-cuts-body">
      <div className="facecam-cuts-main" tabIndex={0} aria-label="Zone de montage et raccourcis">
        <video ref={video} src={sourceUrl} controls preload="metadata" playsInline aria-label="Prise originale à monter"
          onTimeUpdate={e => setTime(e.currentTarget.currentTime)} onError={() => setError('Vidéo indisponible. Rouvrez le montage pour réessayer.')}/>
        <div className="facecam-cuts-transport">
          <Button variant="outline" size="icon" disabled={!data || applying} onClick={togglePlayback} aria-label={playing ? 'Mettre en pause' : 'Lire la vidéo'}>{playing ? <Pause size={17}/> : <Play size={17}/>}</Button>
          <output aria-label="Position de lecture">{cutTime(time)} <span>/ {cutTime(edit.duration)}</span></output>
          <div className="facecam-cuts-modes" aria-label="Mode d’écoute">{(['original', 'cut'] as const).map(value => <button key={value} disabled={applying}
            aria-pressed={mode === value} onClick={() => {setMode(value); audition.current = {mode: value};}}>{value === 'original' ? 'Original' : 'Avec les coupes'}</button>)}</div>
        </div>
        <div className={`facecam-cuts-wave ${selecting ? 'selecting' : ''}`} ref={container} aria-label="Forme d’onde audio"/>
        {!data && !loadError && <p role="status">Chargement de la forme d’onde…</p>}
        {loadError && <p role="alert">{loadError} <button className="underline" onClick={() => setLoadVersion(v => v + 1)}>Réessayer</button></p>}
        <div className="facecam-cuts-tools">
          <Button variant={selecting ? 'secondary' : 'outline'} size="sm" disabled={!waveReady || applying} aria-pressed={selecting} onClick={() => setSelecting(!selecting)}><Scissors size={15}/> Sélectionner une coupe</Button>
          <Button variant="ghost" size="sm" disabled={applying} onClick={() => setMarker(video.current?.currentTime ?? 0)}>Début [I]</Button>
          <Button variant="ghost" size="sm" disabled={marker === null || applying} onClick={() => add(marker!, video.current?.currentTime ?? 0)}>Fin [O]</Button>
          <Button variant="ghost" size="icon" disabled={!history.length || applying} aria-label="Annuler la dernière modification" onClick={undo}><Undo2 size={16}/></Button>
          <label>Zoom <input type="range" aria-label="Zoom de la forme d’onde" min="1" max="20" step="0.01" value={zoom} disabled={!waveReady} onChange={e => {zoomAnchor.current = null; setZoom(Number(e.target.value));}}/></label>
        </div>
        <p className="facecam-cuts-hint">{selecting ? 'Glissez sur la forme d’onde pour créer une coupe, puis ajustez ses bords.' : marker !== null ? `Début marqué à ${cutTime(marker)}. Placez la lecture à la fin puis appuyez sur O.` : 'Cliquez pour vous déplacer. Molette pour zoomer à cet endroit. Glissez les bords d’un passage pour affiner la coupe.'}</p>
        {selected && <fieldset className="facecam-cuts-bounds" disabled={applying}><legend>Ajuster le passage sélectionné</legend>
          <label>Début (s)<input aria-label="Début de la coupe" type="number" min="0" max={edit.duration} step="0.01" value={Number(selected.start.toFixed(3))} onChange={e => changeBound('start', e.target.valueAsNumber)}/></label>
          <label>Fin (s)<input aria-label="Fin de la coupe" type="number" min="0" max={edit.duration} step="0.01" value={Number(selected.end.toFixed(3))} onChange={e => changeBound('end', e.target.valueAsNumber)}/></label>
          <Button variant="ghost" size="sm" onClick={() => listen(selected, 'cut')}><Play size={14}/> Écouter le raccord</Button>
        </fieldset>}
        {!!data?.words.length && <details className="facecam-cuts-transcript"><summary>Se repérer dans les mots</summary><p>Cliquez sur un mot pour l’écouter à cet endroit.</p>
          <div>{data.words.map((word, index) => <button key={index} className={intervals.some(c => word.start >= c.start && word.end <= c.end) ? 'removed' : ''}
            title={cutTime(word.start)} disabled={applying} onClick={() => {play(Math.max(0, word.start - .15), undefined, undefined, 'original'); wave.current?.setScrollTime(Math.max(0, word.start - 2));}}>{word.text} </button>)}</div>
        </details>}
      </div>
      <aside className="facecam-cuts-proposals" aria-label="Passages à retirer">
        <div className="facecam-cuts-list-heading"><h3>Passages à retirer</h3><button disabled={applying || !draft.cuts.some(c => c.enabled)} onClick={() => commit(draft.cuts.map(c => ({...c, enabled: false})))}>Tout garder</button></div>
        {edit.analysis_status === 'analyzing' && <p className="facecam-cuts-analysis" role="status"><Loader2 size={15} className="animate-spin"/> Analyse des reprises… Vous pouvez déjà couper manuellement.</p>}
        {edit.analysis_status === 'error' && <div className="facecam-cuts-analysis" role="status"><p>{edit.analysis_error}</p><button disabled={busy} onClick={() => void onAnalyze().catch(e => setError(e.message))}>Réessayer l’analyse</button></div>}
        <p className="facecam-cuts-legend"><span className="high">Reprise nette</span> précochée · <span>À vérifier</span> conservée</p>
        <p className="facecam-cuts-hint">Écouter lit le passage depuis son début. Raccord démarre 1,5 s avant et saute le passage pour écouter la transition, même décoché.</p>
        {!ordered.length && edit.analysis_status !== 'analyzing' && <p className="facecam-cuts-empty">Aucune reprise évidente signalée. Écoutez votre prise ou ajoutez une coupe manuelle.</p>}
        <div className="facecam-cuts-list">{ordered.map((cut, index) => {
          const suggestion = suggestions.get(cut.id);
          return <article key={cut.id} className={`${cut.enabled ? 'enabled' : ''} ${active === cut.id ? 'active' : ''}`} aria-label={`Passage ${index + 1}`}>
            <div className="facecam-cuts-item-heading"><label><input type="checkbox" checked={cut.enabled} disabled={applying}
              aria-label={`Retirer le passage ${index + 1}`} onChange={e => commit(draft.cuts.map(c => c.id === cut.id ? {...c, enabled: e.target.checked} : c))}/>
              <span className={suggestion?.confidence === 'high' ? 'high' : ''}>{suggestion ? suggestion.confidence === 'high' ? 'Reprise nette' : 'À vérifier' : cut.origin === 'manual' ? 'Coupe manuelle' : 'Proposition précédente'}</span></label>
              {!suggestion && <button disabled={applying} aria-label={`Supprimer la coupe manuelle ${index + 1}`} onClick={() => commit(draft.cuts.filter(c => c.id !== cut.id))}><Trash2 size={14}/></button>}</div>
            <button className="facecam-cuts-range" disabled={applying} onClick={() => {setActive(cut.id); if (video.current) video.current.currentTime = cut.start; wave.current?.setScrollTime(Math.max(0, cut.start - 2));}}>{cutTime(cut.start)} – {cutTime(cut.end)} <span>· {(cut.end - cut.start).toFixed(1)} s</span></button>
            {suggestion && <><p className="facecam-cuts-quote">« {suggestion.removed_text} »</p><p className="facecam-cuts-reason">{suggestion.reason}</p><details><summary>Reprise conservée</summary><p>« {suggestion.retained_text} »</p></details></>}
            <div className="facecam-cuts-listen"><Button variant="ghost" size="sm" disabled={applying} title="Écouter le passage original, du début à la fin de la coupe" onClick={() => listen(cut, 'original')}><Play size={13}/> Écouter</Button>
              <Button variant="ghost" size="sm" disabled={applying} title="Écouter la transition en sautant ce passage, même décoché" onClick={() => listen(cut, 'cut')}><Scissors size={13}/> Raccord</Button></div>
          </article>;
        })}</div>
      </aside>
    </div>
    {(error || (submitted !== null && job?.status === 'error')) && <p className="facecam-error" role="alert">{error || job?.error || job?.message || 'Montage interrompu. Réessayez.'}</p>}
    <footer className="facecam-cuts-footer"><div><strong>{intervals.length} coupe{intervals.length > 1 ? 's' : ''} · {removed.toFixed(1)} s retirées</strong>
      <small>{cutTime(Math.max(0, edit.duration - removed))} conservées · Original toujours disponible</small></div>
      <Button disabled={busy || applying || edit.duration - removed < .25} onClick={() => void submit()}>{applying ? <Loader2 size={16} className="animate-spin"/> : <Check size={16}/>}
        {applying ? job?.message || job?.step || 'Application des coupes…' : intervals.length ? 'Valider les coupes et continuer' : 'Garder toute la prise et continuer'}</Button>
    </footer>
  </dialog>;
}
