import { useEffect, useRef, useState } from 'react';
import { Check, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui';
import { facecamRequest, type FacecamBlock, type FacecamState, type RecordingReview, type RecordingWordChange } from '@/api/facecam';
import { FacecamRecordingEditor } from './FacecamRecordingEditor';
import './FacecamRecordingReview.css';

export function FacecamRecordingReview({projectId, state, disabled, onState, onDirtyChange}: {
  projectId: string; state: FacecamState; disabled: boolean;
  onState: (state: FacecamState) => void; onDirtyChange: (dirty: boolean) => void;
}) {
  const [review, setReview] = useState<RecordingReview | null>(null);
  const [error, setError] = useState(''), [saving, setSaving] = useState(false), [submitting, setSubmitting] = useState(false);
  const [reload, setReload] = useState(0), [dirty, setDirty] = useState(false);
  const draft = useRef<RecordingReview | null>(null), server = useRef<RecordingReview | null>(null);
  const changed = useRef(0), saved = useRef(0), promise = useRef<Promise<void> | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null), mounted = useRef(true), validating = useRef(false);
  const callbacks = useRef({onState, onDirtyChange}); callbacks.current = {onState, onDirtyChange};
  const identity = `${state.revision}:${state.recording?.fingerprint}`;

  useEffect(() => {
    mounted.current = true;
    const warn = (event: BeforeUnloadEvent) => {if (changed.current !== saved.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', warn);
    return () => {mounted.current = false; window.removeEventListener('beforeunload', warn); if (timer.current) clearTimeout(timer.current);};
  }, []);

  useEffect(() => {
    const next = state.recording_review;
    if (!next || next.revision !== state.revision || next.fingerprint !== state.recording?.fingerprint) {
      if (changed.current === saved.current) {server.current = null; draft.current = null; setReview(null);}
      let cancelled = false;
      facecamRequest(projectId, '/recording/review').then(value => {
        if (!cancelled) callbacks.current.onState(value);
      }).catch(cause => {if (!cancelled) setError((cause as Error).message);});
      return () => {cancelled = true;};
    }
    if (changed.current !== saved.current) {
      if (server.current && (next.id !== server.current.id || next.version !== server.current.version)) {
        setError('La relecture a changé ailleurs. Votre texte reste affiché ; rechargez avant de valider.');
      }
      return;
    }
    server.current = next; draft.current = next; setReview(next);
  }, [projectId, identity, state.recording_review, state.revision, state.recording?.fingerprint, reload]);

  async function flush(): Promise<void> {
    if (timer.current) clearTimeout(timer.current);
    if (promise.current) await promise.current;
    if (changed.current === saved.current || !draft.current || !server.current) return;
    const snapshot = draft.current, previous = server.current, version = changed.current;
    const remaining = new Set(snapshot.changes.map(c => c.id));
    setSaving(true);
    const operation = (async () => {
      const value = await facecamRequest(projectId, '/recording/review', 'PUT', {
        revision: previous.revision, review_id: previous.id, version: previous.version, blocks: snapshot.blocks,
        resolved_ids: previous.changes.filter(c => !remaining.has(c.id)).map(c => c.id),
      });
      server.current = value.recording_review!; saved.current = version;
      callbacks.current.onState(value);
      if (!mounted.current) return;
      if (changed.current === version) {
        draft.current = server.current; setReview(server.current); setDirty(false); callbacks.current.onDirtyChange(false);
      }
    })();
    promise.current = operation;
    try {await operation;} finally {promise.current = null; if (mounted.current) setSaving(false);}
    if (changed.current !== saved.current) await flush();
  }

  function edit(blocks: FacecamBlock[], changes: RecordingWordChange[]) {
    if (!draft.current) return;
    changed.current++; setDirty(true); callbacks.current.onDirtyChange(true); setError('');
    draft.current = {...draft.current, blocks, changes, validated_revision: null}; setReview(draft.current);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {void flush().catch(cause => {if (mounted.current) setError((cause as Error).message);});}, 500);
  }

  async function validate() {
    if (validating.current) return;
    validating.current = true; setSubmitting(true); setError('');
    try {
      await flush();
      const latest = server.current;
      if (!latest) return;
      const value = await facecamRequest(projectId, '/recording/review', 'POST', {
        revision: latest.revision, review_id: latest.id, version: latest.version,
      });
      server.current = value.recording_review!; draft.current = server.current;
      setReview(server.current); callbacks.current.onState(value);
    } catch (cause) {setError((cause as Error).message);}
    finally {validating.current = false; if (mounted.current) setSubmitting(false);}
  }

  const complete = !dirty && review?.validated_revision === state.revision && state.placement?.status === 'complete';
  const retry = !dirty && review?.validated_revision === state.revision && (state.placement?.status === 'error' || state.job?.status === 'error');
  return <section className="facecam-recording-review" aria-label="Vérification du texte enregistré">
    <header><div><span className="facecam-review-kicker">APRÈS LA TRANSCRIPTION</span><h3>Relire le texte enregistré</h3></div>
      <span role="status" className="facecam-review-save">{saving || dirty ? 'Enregistrement…' : review ? 'Relecture enregistrée' : 'Chargement…'}</span></header>
    <p>Votre script reste inchangé tant que vous n’acceptez pas une suggestion. Cliquez sur les mots signalés, ou écrivez directement dans le texte.</p>
    <div className="facecam-recording-legend"><span><i className="old"/>Mots omis ou remplacés</span><span><i className="new"/>Mots entendus</span></div>
    {error && <div className="facecam-error" role="alert">{error}<button type="button" disabled={disabled || submitting} onClick={() => {
      if (dirty) void flush().then(() => setError('')).catch(cause => setError((cause as Error).message));
      else {setError(''); setReload(value => value + 1);}
    }}>Réessayer</button></div>}
    {review ? <FacecamRecordingEditor key={review.id} blocks={review.blocks} changes={review.changes}
      disabled={disabled || submitting} onChange={edit}/> : <p role="status"><Loader2 size={16} className="animate-spin inline mr-2"/>Préparation de la relecture…</p>}
    <footer><div><strong>{complete ? 'Texte validé et scènes placées' : `${review?.changes.length ?? 0} suggestion(s) restante(s)`}</strong>
      <p>Les suggestions non acceptées conservent votre formulation. La validation lance le placement des scènes.</p></div>
      <Button disabled={disabled || submitting || !review || complete} onClick={() => void validate()}>
        {submitting ? <Loader2 size={16} className="animate-spin mr-2"/> : complete ? <Check size={16} className="mr-2"/> : null}
        {complete ? 'Texte validé' : retry ? 'Reprendre le placement des scènes' : 'Valider le texte et placer les scènes'}
      </Button></footer>
  </section>;
}
