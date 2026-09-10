import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { NarratorSample, RawScenesResponse } from "@/types";
import { Button } from "@/components/ui";
import { ProjectClippedVideoPlayer } from "@/components/video";
import type { ClippedVideoPlayerHandle } from "@/components/video/ClippedVideoPlayer";
import { formatTime } from "@/utils";
import { MEDIA_PRIORITY } from "@/utils/mediaPriorities";

interface Props {
  projectId: string;
  refreshKey: unknown;
  disabled?: boolean;
  hasUnsavedEdits?: boolean;
  onBusyChange: (busy: boolean) => void;
  onChanged: (data: RawScenesResponse) => void | Promise<void>;
}

export function NarratorControls({ projectId, refreshKey, disabled, hasUnsavedEdits, onChanged, onBusyChange }: Props) {
  const [data, setData] = useState<RawScenesResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState<string[]>([]);
  const [automatic, setAutomatic] = useState(true);
  const [sample, setSample] = useState<NarratorSample | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const player = useRef<ClippedVideoPlayerHandle>(null);
  const sampleContainer = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (disabled) return;
    let cancelled = false;
    api.getRawScenes(projectId).then((result) => {
      if (!cancelled) setData(result);
    }).catch((err: Error) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [projectId, refreshKey, disabled]);

  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);

  useEffect(() => {
    const playback = player.current;
    let cancelled = false;
    if (sample && open && playback) {
      sampleContainer.current?.scrollIntoView({ block: "nearest" });
      void (async () => {
        // Let the player's mount/lease effects settle before requesting
        // playback (including React's development-mode remount).
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        if (cancelled) return;
        await playback.waitUntilReady({ minReadyState: 2, timeoutMs: 15000 });
        if (cancelled) return;
        if (playback.hasLoadError() || playback.getReadyState() < 2) {
          setError("The voice sample could not load. Retry using the sample player.");
          return;
        }
        await playback.seekToStart();
        if (cancelled) return;
        if (!await playback.playChecked() && !cancelled) {
          setError("Press Play in the sample player to hear this voice.");
        }
      })();
    }
    return () => { cancelled = true; playback?.pause(); };
  }, [sample, open]);

  const narrator = data?.narrator;
  const currentIds = data?.detection?.selected_narrator_ids?.length
    ? data.detection.selected_narrator_ids : [data?.detection?.tts_speaker_id ?? ""];
  const voiceNames = narrator?.speakers.flatMap((speaker, index) =>
    currentIds.includes(speaker.speaker_id) ? [`Voice ${index + 1}`] : []) ?? [];
  const act = async (undo: boolean) => {
    if (!narrator || busy) return;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    player.current?.pause();
    try {
      const result = undo
        ? await api.undoNarrator(projectId, narrator.revision)
        : await api.changeNarrator(projectId, automatic ? null : selection, narrator.revision);
      setData(result);
      await onChanged(result);
      setOpen(false);
      setSample(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  };

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] p-3 space-y-2">
      <div className="flex items-center flex-wrap gap-3 text-sm">
        <span>{voiceNames.length ? `Narrator: ${voiceNames.join(" + ")}` : "Narrator"}
          {voiceNames.length > 0 && ` (${data?.detection?.selection_origin === "manual" ? (voiceNames.length > 1 ? "merged" : "selected") : "automatic"})`}
        </span>
        <Button variant="outline" disabled={disabled || busy || !narrator?.correction_available}
          onClick={() => {
            setAutomatic(data?.detection?.selection_origin !== "manual");
            setSelection(data?.detection?.selection_origin === "manual" ? currentIds : []);
            setError(null);
            setOpen(true);
          }}>Change narrator</Button>
        {narrator?.undo_available && !hasUnsavedEdits && <Button variant="outline" disabled={disabled || busy}
          onClick={() => void act(true)}>Undo narrator change</Button>}
      </div>
      {narrator?.unavailable_reason && <p className="text-sm text-[hsl(var(--muted-foreground))]">{narrator.unavailable_reason}</p>}
      {narrator?.warning && <p className="text-sm">Voice matching was unavailable. Voice correction is still available.</p>}
      {data?.transcription?.unrecovered_gaps?.length ? <p className="text-sm" role="status">
        Speech could not be transcribed at {data.transcription.unrecovered_gaps.map(([s, e]) => `${formatTime(s)}–${formatTime(e)}`).join(", ")}.
      </p> : null}
      {error && !open && <p role="alert" className="text-sm text-[hsl(var(--destructive))]">{error}</p>}
      <dialog ref={dialog} aria-labelledby="narrator-title" onCancel={(event) => {
        if (busy) event.preventDefault(); else { setOpen(false); setSample(null); }
      }} className="m-auto w-[min(42rem,95vw)] max-h-[90vh] overflow-y-auto rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] text-[hsl(var(--foreground))] p-5 backdrop:bg-black/60">
        {open && <div className="space-y-4">
          <h2 id="narrator-title" className="text-lg font-semibold">Choose narration voices</h2>
          <p className="text-sm">Select one voice, or merge several voices into one narration track. Unselected voices and cinematic gaps remain eligible as raw scenes. Changing the narrator resets transcript edits and raw-scene decisions. You can undo the change until your next edit.</p>
          {sample && <div ref={sampleContainer}>
            <ProjectClippedVideoPlayer ref={player} projectId={projectId}
              key={`${sample.start_time}-${sample.end_time}`} startTime={sample.start_time} endTime={sample.end_time}
              muted={false} eager className="w-full h-48" leasePriority={MEDIA_PRIORITY.MANUAL_MODAL}
              warmupPriority={MEDIA_PRIORITY.MANUAL_MODAL} />
          </div>}
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" name="narrator" checked={automatic} onChange={() => setAutomatic(true)} disabled={busy} />
            Automatic (longest speaking voice)
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" name="narrator" checked={!automatic} onChange={() => setAutomatic(false)} disabled={busy} />
            Manual (select one or more voices)
          </label>
          {narrator?.speakers.map((speaker, index) => <div key={speaker.speaker_id} className="border border-[hsl(var(--border))] rounded p-3 space-y-2">
            <label className="flex items-center gap-2 font-medium">
              <input type="checkbox" checked={!automatic && selection.includes(speaker.speaker_id)} onChange={(event) => {
                const checked = event.target.checked;
                setSelection((previous) => checked
                  ? [...(automatic ? [] : previous), speaker.speaker_id]
                  : previous.filter((id) => id !== speaker.speaker_id));
                setAutomatic(false);
              }} disabled={busy} />
              Voice {index + 1} · {formatTime(speaker.duration)} of speech
            </label>
            {speaker.samples.map((clip, i) => <div key={clip.start_time} className="text-sm space-y-1">
              <Button variant="outline" disabled={busy} onClick={() => setSample({ ...clip })}
                aria-label={`Play Voice ${index + 1} sample ${i + 1}`}>Play sample {i + 1} · {formatTime(clip.start_time)}</Button>
              <p lang={data?.transcription?.language}>{clip.text || "No transcript for this sample"}</p>
            </div>)}
          </div>)}
          {!automatic && selection.length === 0 && <p className="text-sm">Select at least one narration voice.</p>}
          {error && <p role="alert" className="text-sm text-[hsl(var(--destructive))]">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" disabled={busy} onClick={() => { setOpen(false); setSample(null); }}>Cancel</Button>
            <Button disabled={busy || disabled || (!automatic && selection.length === 0)} onClick={() => void act(false)}>{busy ? "Applying…" : "Apply narrator and reset edits"}</Button>
          </div>
        </div>}
      </dialog>
    </div>
  );
}
