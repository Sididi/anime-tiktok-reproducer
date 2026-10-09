import { useEffect, useRef, useState } from 'react';
import { DEFAULT_FACECAM_AUTO_EDITOR, facecamRequest, sameAutoEditorSettings, type FacecamAutoEditorSettings, type FacecamState } from '@/api/facecam';

const PREVIEW_JOB = "Préparation de la voix et de l'aperçu";

/** Debounce edits and serialize requests, retaining edits made during a job. */
export function useFacecamPreview({id, state, musicKey, settingsLoaded, approved, working, apply, saveMusic, onError}: {
  id: string; state: FacecamState | null; musicKey: string | null; settingsLoaded: boolean;
  approved: boolean; working: boolean; apply: (state: FacecamState) => void;
  saveMusic: (key: string | null) => void; onError: (error: string) => void;
}) {
  const [tuningDraft, setTuningDraft] = useState<FacecamAutoEditorSettings | null>(null);
  const [musicDraft, setMusicDraft] = useState<string | undefined>(undefined);
  const [manualVersion, setManualVersion] = useState(0);
  const handledManual = useRef(0);
  const [requestBusy, setRequestBusy] = useState(false);
  const attempted = useRef<string | null>(null), inFlight = useRef(false);
  const tuning = {...DEFAULT_FACECAM_AUTO_EDITOR, ...(tuningDraft ?? state?.auto_editor)};
  const selectedMusic = musicDraft ?? musicKey;
  const enabled = manualVersion > 0 || !!state?.preview_enabled || !!state?.prepared?.preview_version;
  const jobBusy = state?.job?.status === 'processing';
  const controlsDisabled = working || (jobBusy && state?.job?.step !== PREVIEW_JOB);
  const ready = approved && !!state?.recording && state.recording.reviewed_revision === state.revision;
  const prepared = state?.prepared;
  const previewReady = ready && !!prepared?.preview_version && !!prepared.auto_editor_signature
    && prepared.revision === state?.revision && prepared.fingerprint === state?.recording?.fingerprint
    && prepared.preview_music_key === selectedMusic && sameAutoEditorSettings(tuning, prepared.auto_editor);
  const musicChanged = settingsLoaded && selectedMusic !== musicKey;
  // A review/approval can make the same settings renderable after a music save.
  const desiredKey = JSON.stringify([id, state?.revision, state?.recording?.fingerprint, tuning, selectedMusic, ready]);

  useEffect(() => {
    if (working || requestBusy || jobBusy || !settingsLoaded) return;
    const force = manualVersion !== handledManual.current;
    if (!force && !musicChanged && !(enabled && ready && !previewReady)) return;
    // A failed request is retried only after another edit or an explicit click.
    if (!force && attempted.current === desiredKey) return;
    const timer = setTimeout(async () => {
      if (inFlight.current) return;
      attempted.current = desiredKey; handledManual.current = manualVersion;
      inFlight.current = true; setRequestBusy(true); onError('');
      // Freeze this request's values. New edits stay in the drafts and will be
      // submitted after the server job completes, never erased by its response.
      const [, revision, , autoEditor, music] = JSON.parse(desiredKey);
      try {
        if (musicChanged) {
          const result = await facecamRequest<{music_key: string | null}>(id, '/settings', 'PUT', {music_key: music});
          saveMusic(result.music_key);
        }
        if (ready && (force || (enabled && !previewReady))) {
          apply(await facecamRequest(id, '/preview', 'POST', {revision, auto_editor: autoEditor}));
        }
      } catch (error) {
        onError((error as Error).message);
      } finally {
        inFlight.current = false; setRequestBusy(false);
      }
    }, force ? 0 : 300);
    return () => clearTimeout(timer);
  }, [id, working, requestBusy, jobBusy, settingsLoaded, musicChanged, enabled, ready, previewReady,
    desiredKey, manualVersion, apply, saveMusic, onError]);

  function changeTuning(value: FacecamAutoEditorSettings) {attempted.current = null; setTuningDraft(value);}
  function changeMusic(value: string) {attempted.current = null; setMusicDraft(value);}
  function prepare() {setManualVersion(version => version + 1);}
  return {tuning, selectedMusic, enabled, requestBusy, controlsDisabled, previewReady, changeTuning, changeMusic, prepare,
    pending: !previewReady && (enabled || !sameAutoEditorSettings(tuning, state?.auto_editor ?? DEFAULT_FACECAM_AUTO_EDITOR))};
}
