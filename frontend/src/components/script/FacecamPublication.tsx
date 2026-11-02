import { useEffect, useState } from 'react';
import { Check, ImageIcon, Loader2, SlidersHorizontal } from 'lucide-react';
import { Button } from '@/components/ui';
import { facecamRequest, type FacecamState } from '@/api/facecam';
import type { MetadataTitleCandidatesPayload, PlatformMetadata } from '@/types';
import { TitleSelectionModal } from './TitleSelectionModal';
import { MetadataEditorModal } from './MetadataEditorModal';
import './FacecamPublication.css';

interface MetadataStatus {
  revision: number; generation_id: string | null; candidates: Pick<MetadataTitleCandidatesPayload, 'title_candidates'> | null;
  metadata: PlatformMetadata | null; ready: boolean;
}
interface Thumbnail {
  id: string; episode: string; label: string; timestamp: number; crop_x: number; crop_y: number;
}
interface ThumbnailsStatus {
  version: string; candidates: Thumbnail[]; selected: Thumbnail | null; ready: boolean; generated: boolean;
  episodes: {id: string; label: string; duration: number; fps: number}[]; warning: string | null;
}
type ThumbnailChoice = Pick<Thumbnail, 'episode' | 'timestamp' | 'crop_x' | 'crop_y'>;
const timecode = (seconds: number) => `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(2).padStart(5, '0')}`;

function ManualThumbnail({id, status, initial, busy, onClose, onSave}: {
  id: string; status: ThumbnailsStatus; initial: Thumbnail | null; busy: boolean;
  onClose: () => void; onSave: (choice: ThumbnailChoice) => Promise<void>;
}) {
  const [episode, setEpisode] = useState(status.episodes.some(s => s.id === initial?.episode) ? initial!.episode : status.episodes[0].id);
  const source = status.episodes.find(s => s.id === episode)!;
  const [timestamp, setTimestamp] = useState(initial?.timestamp ?? Math.min(60, source.duration / 2));
  const [cropX, setCropX] = useState(initial?.crop_x ?? .5), [cropY, setCropY] = useState(initial?.crop_y ?? .5);
  const [frame, setFrame] = useState<{url: string; episode: string; timestamp: number} | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const ready = frame?.episode === episode && frame.timestamp === timestamp;
  const lastFrame = Math.max(0, source.duration - 1 / source.fps);
  useEffect(() => {
    const abort = new AbortController(); let url: string | undefined;
    const timer = setTimeout(() => {
      setError('');
      const params = new URLSearchParams({version: status.version, episode, timestamp: String(timestamp)});
      fetch(`/api/projects/${id}/facecam/thumbnails/frame?${params}`, {signal: abort.signal})
        .then(async response => {
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail || 'Image indisponible. Essayez un autre instant.');
          }
          const blob = await response.blob();
          if (abort.signal.aborted) return;
          url = URL.createObjectURL(blob); setFrame({url, episode, timestamp});
        }).catch(e => {if (!abort.signal.aborted) setError(e.message);});
    }, 250);
    return () => {clearTimeout(timer); abort.abort(); if (url) URL.revokeObjectURL(url);};
  }, [id, status.version, episode, timestamp, retry]);
  async function save() {
    setError('');
    try {await onSave({episode, timestamp, crop_x: cropX, crop_y: cropY}); onClose();}
    catch (e) {setError((e as Error).message);}
  }
  return <div className="facecam-thumbnail-backdrop">
    <section className="facecam-thumbnail-dialog" role="dialog" aria-modal="true" aria-labelledby="thumbnail-manual-title">
      <header><div><h2 id="thumbnail-manual-title">Choisir une image dans un épisode</h2><p>Parcourez l’épisode, puis ajustez le cadrage vertical.</p></div>
        <Button variant="ghost" disabled={busy} onClick={onClose} aria-label="Fermer le sélecteur de miniature">×</Button></header>
      <fieldset disabled={busy}>
        <label>Épisode<select value={episode} onChange={e => {
          const next = status.episodes.find(s => s.id === e.target.value)!;
          setEpisode(next.id); setTimestamp(Math.min(60, next.duration / 2)); setCropX(.5); setCropY(.5);
        }}>{status.episodes.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
        <div className="facecam-thumbnail-preview">
          <div className="facecam-thumbnail-source">{ready ? <img src={frame.url} alt="Image complète de l’épisode"/> : <span role="status"><Loader2 className="animate-spin"/>Chargement de l’image…</span>}</div>
          <div className="facecam-thumbnail-portrait">{ready && <img src={frame.url} alt="Aperçu de la miniature verticale" style={{objectPosition: `${cropX * 100}% ${cropY * 100}%`}}/>}</div>
        </div>
        <label>Position dans l’épisode · {timecode(timestamp)} / {timecode(source.duration)}
          <input aria-label="Position dans l’épisode" type="range" min="0" max={lastFrame} step={1 / source.fps} value={timestamp} onChange={e => setTimestamp(Number(e.target.value))}/>
        </label>
        <div className="flex flex-wrap items-center gap-3 my-3">
          <Button variant="outline" onClick={() => setTimestamp(t => Math.max(0, t - 1 / source.fps))}>Image précédente</Button>
          <label>Instant (secondes)<input type="number" min="0" max={lastFrame} step="0.01" value={Number(timestamp.toFixed(3))}
            onChange={e => setTimestamp(Math.min(lastFrame, Math.max(0, Number(e.target.value))))}/></label>
          <Button variant="outline" onClick={() => setTimestamp(t => Math.min(lastFrame, t + 1 / source.fps))}>Image suivante</Button>
        </div>
        <div className="facecam-thumbnail-crops">
          <label>Cadrage horizontal<input type="range" min="0" max="1" step="0.01" value={cropX} onChange={e => setCropX(Number(e.target.value))}/></label>
          <label>Cadrage vertical<input type="range" min="0" max="1" step="0.01" value={cropY} onChange={e => setCropY(Number(e.target.value))}/></label>
        </div>
      </fieldset>
      {error && <div role="alert" className="facecam-error">{error}<Button variant="outline" disabled={busy} onClick={() => setRetry(n => n + 1)}>Réessayer l’image</Button></div>}
      <footer><p>JPEG · 1080 × 1920 · image complète disponible pour le recadrage.</p>
        <Button disabled={busy || !ready || !!error} onClick={() => void save()}>Utiliser cette image</Button></footer>
    </section>
  </div>;
}

export function FacecamPublication({id, state, disabled, approved, onState, onReady, onBusyChange}: {
  id: string; state: FacecamState; disabled: boolean; approved: boolean;
  onState: (state: FacecamState) => void;
  onReady: (status: {revision: number; ready: boolean}) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [metadata, setMetadata] = useState<MetadataStatus | null>(null);
  const [thumbnails, setThumbnails] = useState<ThumbnailsStatus | null>(null);
  const [metadataError, setMetadataError] = useState(''), [thumbnailError, setThumbnailError] = useState('');
  const [saving, setSaving] = useState(false), [loadedKey, setLoadedKey] = useState('');
  const [titlesOpen, setTitlesOpen] = useState(false), [editorOpen, setEditorOpen] = useState(false);
  const [manual, setManual] = useState<{initial: Thumbnail | null} | null>(null);
  const [refresh, setRefresh] = useState(0);
  const placementReady = state.placement?.status === 'complete';
  const metadataId = state.metadata_generation?.id, thumbnailId = state.thumbnail?.id;
  const requestKey = JSON.stringify([id, state.revision, state.job?.status, placementReady, state.placement?.key, metadataId, thumbnailId, refresh]);
  const loading = loadedKey !== requestKey;
  useEffect(() => {
    let cancelled = false;
    const requests = [
      facecamRequest<MetadataStatus>(id, '/metadata').then(next => {
        if (cancelled) return;
        setMetadataError(''); setMetadata(next); setTitlesOpen(!!next.candidates && !next.metadata);
      }).catch(e => {if (!cancelled) {setMetadata(null); setMetadataError(e.message);}}),
      placementReady ? facecamRequest<ThumbnailsStatus>(id, '/thumbnails').then(next => {if (!cancelled) {setThumbnailError(''); setThumbnails(next);}})
        .catch(e => {if (!cancelled) {setThumbnails(null); setThumbnailError(e.message);}}) : Promise.resolve().then(() => {if (!cancelled) setThumbnails(null);}),
    ];
    void Promise.all(requests).finally(() => {if (!cancelled) setLoadedKey(requestKey);});
    return () => {cancelled = true;};
  }, [id, placementReady, requestKey]);
  const ready = !loading && !saving && approved && metadata?.revision === state.revision && !!metadata?.ready && !!thumbnails?.ready;
  useEffect(() => {onReady({revision: state.revision, ready});}, [onReady, state.revision, ready]);
  useEffect(() => {onBusyChange(saving); return () => onBusyChange(false);}, [onBusyChange, saving]);
  const busy = disabled || saving || loading;
  async function generate(kind: 'metadata' | 'thumbnails') {
    setSaving(true); const setError = kind === 'metadata' ? setMetadataError : setThumbnailError;
    setError('');
    try {onState(await facecamRequest(id, `/${kind}/generate`, 'POST', {revision: state.revision}));}
    catch (e) {setError((e as Error).message);}
    finally {setSaving(false); setRefresh(n => n + 1);}
  }
  async function chooseTitle(title: string) {
    setSaving(true); setMetadataError('');
    try {
      setMetadata(await facecamRequest<MetadataStatus>(id, '/metadata/title', 'PUT', {
        revision: state.revision, generation_id: metadata?.generation_id, title}));
      setTitlesOpen(false);
    } catch (e) {setMetadataError((e as Error).message);}
    finally {setSaving(false);}
  }
  async function saveMetadata(value: PlatformMetadata) {
    setSaving(true);
    try {setMetadata(await facecamRequest<MetadataStatus>(id, '/metadata', 'PUT', {
      revision: state.revision, generation_id: metadata?.generation_id, metadata: value}));}
    finally {setSaving(false);}
  }
  async function selectThumbnail(choice: ThumbnailChoice) {
    setSaving(true); setThumbnailError('');
    try {setThumbnails(await facecamRequest<ThumbnailsStatus>(id, '/thumbnail', 'PUT', {version: thumbnails?.version, ...choice}));}
    finally {setSaving(false);}
  }
  return <section className="facecam-card facecam-publication" aria-labelledby="facecam-publication-title">
    <header><div><h2 id="facecam-publication-title">5. Préparer la publication</h2><p>Choisissez votre titre et votre miniature pour débloquer le traitement.</p></div>
      <span className={ready ? 'facecam-publication-done' : ''}>{ready ? <><Check size={16}/> Prêt pour le traitement</> : 'Étape obligatoire'}</span></header>
    <div className="facecam-publication-metadata"><h3>Titre et métadonnées</h3>
      <p>Huit propositions ou votre propre titre. Une description commune sans CTA, des hashtags fixes et un texte TikTok dédié.</p>
      {metadata?.ready && <blockquote>{metadata.metadata?.youtube.title}</blockquote>}
      <div className="flex flex-wrap gap-3">
        <Button variant="outline" disabled={busy || !approved} onClick={() => void generate('metadata')}>{metadata?.candidates ? 'Régénérer les métadonnées' : 'Générer les métadonnées'}</Button>
        {metadata?.candidates && <Button variant="outline" disabled={busy || !approved} onClick={() => setTitlesOpen(true)}>Choisir parmi les 8 titres</Button>}
        {metadata?.metadata && <Button variant="outline" disabled={busy || !approved} onClick={() => setEditorOpen(true)}>Modifier les métadonnées</Button>}
      </div>
      {!approved && <p>Validez le script avant de générer les métadonnées.</p>}
      {metadataError && <p role="alert" className="text-red-400">{metadataError} <button onClick={() => setRefresh(n => n + 1)}>Recharger</button></p>}
    </div>
    <div className="facecam-publication-thumbnails"><h3><ImageIcon size={17}/> Miniature de publication</h3>
      <p>Jusqu’à 10 images extraites des épisodes du montage, triées par netteté, exposition et diversité. Sélection locale gratuite, sans appel IA payant.</p>
      <div className="flex flex-wrap gap-3">
        <Button variant="outline" disabled={busy || !approved || !placementReady} onClick={() => void generate('thumbnails')}>{thumbnails?.generated ? 'Recharger les propositions' : 'Proposer 10 miniatures'}</Button>
        <Button variant="outline" disabled={busy || !approved || !thumbnails?.episodes.length} onClick={() => setManual({initial: thumbnails?.selected ?? null})}><SlidersHorizontal size={16} className="mr-2"/>Sélecteur manuel</Button>
      </div>
      {!placementReady && <p>Les miniatures seront disponibles après le placement des scènes.</p>}
      {!!thumbnails?.candidates.length && <div className="facecam-thumbnail-grid" aria-label="Miniatures proposées">
        {thumbnails.candidates.map((candidate, index) => {
          const selected = thumbnails.selected?.episode === candidate.episode && thumbnails.selected?.timestamp === candidate.timestamp;
          return <div key={candidate.id} className="facecam-thumbnail-card">
            <button disabled={busy} aria-pressed={selected} aria-label={`Choisir la miniature ${index + 1}`} onClick={() => {
              void selectThumbnail({episode: candidate.episode, timestamp: candidate.timestamp, crop_x: candidate.crop_x, crop_y: candidate.crop_y}).catch(e => setThumbnailError(e.message));
            }}><img src={`/api/projects/${id}/facecam/thumbnails/${candidate.id}?version=${thumbnails.version}`} alt={`Proposition ${index + 1}`} loading="lazy"/>
              <span>{selected ? <Check size={14}/> : index + 1} · {timecode(candidate.timestamp)}</span></button>
            <button className="facecam-thumbnail-adjust" disabled={busy} onClick={() => setManual({initial: candidate})} aria-label={`Recadrer la miniature ${index + 1}`}>Recadrer</button>
          </div>;
        })}
      </div>}
      {thumbnails?.generated && thumbnails.candidates.length < 10 && <p>{thumbnails.candidates.length} images distinctes exploitables trouvées. Vous pouvez aussi explorer les épisodes manuellement.</p>}
      {thumbnails?.selected && <div className="facecam-thumbnail-selected"><img src={`/api/projects/${id}/facecam/thumbnail?v=${thumbnails.selected.id}`} alt="Miniature sélectionnée"/>
        <div><strong><Check size={16}/> Miniature enregistrée</strong><p>{thumbnails.selected.label} · {timecode(thumbnails.selected.timestamp)}<br/>JPEG · 1080 × 1920 · 9:16 · moins de 2 Mo<br/>Incluse dans le ZIP : metadata/thumbnail.jpg</p>
          <Button variant="outline" disabled={busy} onClick={() => setManual({initial: thumbnails.selected})}>Ajuster le cadrage</Button></div></div>}
      <p>Format vertical pour YouTube Shorts et Instagram Reels. Pour Facebook, utilisez le fichier lorsque l’outil de publication propose une couverture personnalisée.</p>
      {thumbnails?.warning && <p role="status">{thumbnails.warning}</p>}
      {thumbnailError && <p role="alert" className="text-red-400">{thumbnailError} <button onClick={() => setRefresh(n => n + 1)}>Recharger</button></p>}
    </div>
    {titlesOpen && metadata?.candidates && <TitleSelectionModal isOpen allowCustomMetadataTitle metadataCandidates={metadata.candidates} isSaving={busy}
      initialMetadataTitle={metadata.metadata?.youtube.title}
      error={metadataError} onClose={() => setTitlesOpen(false)} onConfirm={({metadataTitle}) => {if (metadataTitle) void chooseTitle(metadataTitle);}}/>}
    {editorOpen && metadata?.metadata && <MetadataEditorModal isOpen facecam metadata={metadata.metadata} onClose={() => setEditorOpen(false)} onSave={saveMetadata}/>}
    {manual && thumbnails?.episodes.length && <ManualThumbnail key={thumbnails.version} id={id} status={thumbnails} initial={manual.initial} busy={busy}
      onClose={() => setManual(null)} onSave={selectThumbnail}/>}
  </section>;
}
