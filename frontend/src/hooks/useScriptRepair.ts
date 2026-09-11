import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ScriptRepairReport, Transcription } from "@/types";
import { inspectScriptJson } from "@/utils/scriptValidation";

export function useScriptRepair({ projectId, targetLanguage, transcription, draft, onApply, onReady }: {
  projectId: string | undefined;
  targetLanguage: string;
  transcription: Transcription | null;
  draft: string;
  onApply: (value: string) => void;
  onReady: () => void;
}) {
  const [report, setReport] = useState<ScriptRepairReport | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  const activeSnapshot = useRef<{ context: string; draft: string } | null>(null);
  const lastAutoDraft = useRef<string | null>(null);
  const context = useMemo(() => JSON.stringify([projectId, targetLanguage, transcription]), [projectId, targetLanguage, transcription]);
  const latest = useRef({ context, draft, onApply, onReady });
  useLayoutEffect(() => { latest.current = { context, draft, onApply, onReady }; }, [context, draft, onApply, onReady]);

  const cancel = useCallback(() => {
    active.current?.abort();
    active.current = null;
    setRunning(false);
  }, []);

  useEffect(() => {
    const snapshot = activeSnapshot.current;
    if (snapshot && (snapshot.context !== context || snapshot.draft !== draft)) cancel();
  }, [context, draft, cancel]);
  useEffect(() => () => { active.current?.abort(); }, []);

  const restore = useCallback((value: ScriptRepairReport | null, id: string | null) => {
    setReport(value);
    setRunId(id);
    setError(null);
  }, []);

  const repair = useCallback(async (value = latest.current.draft, automatic = false) => {
    if (!projectId || !inspectScriptJson(value, transcription)?.repairable) return;
    const key = JSON.stringify([context, value]);
    if (automatic && lastAutoDraft.current === key) return;
    if (automatic) lastAutoDraft.current = key;
    cancel();
    const controller = new AbortController();
    active.current = controller;
    const snapshot = { context, draft: value };
    activeSnapshot.current = snapshot;
    setRunning(true);
    setError(null);
    try {
      const result = await api.repairScript(projectId, {
        script_json: JSON.parse(value), target_language: targetLanguage,
      }, controller.signal);
      if (controller.signal.aborted || active.current !== controller ||
          latest.current.context !== snapshot.context || latest.current.draft !== snapshot.draft) return;
      active.current = null;
      restore(result.repair_report, result.run_id);
      latest.current.onApply(JSON.stringify(result.script_json, null, 2));
      latest.current.onReady();
    } catch (err) {
      if (!controller.signal.aborted && active.current === controller)
        setError((err as Error).message);
    } finally {
      if (active.current === controller) active.current = null;
      if (!active.current) setRunning(false);
    }
  }, [projectId, transcription, targetLanguage, context, cancel, restore]);

  const approve = useCallback(async (value: string) => {
    if (!projectId || !runId) return;
    const snapshot = { context: latest.current.context, draft: latest.current.draft };
    await api.reviewScriptRepair(projectId, {
      run_id: runId, script_json: JSON.parse(value), target_language: targetLanguage,
    });
    if (latest.current.context !== snapshot.context || latest.current.draft !== snapshot.draft)
      throw new Error("Draft, source, or language changed during review.");
    setReport((previous) => previous ? { ...previous, review_required: false, unresolved_scene_indices: [], issues: [] } : null);
  }, [projectId, runId, targetLanguage]);

  return { report, runId, running, error, restore, repair, cancel, approve,
    reviewRequired: report?.review_required ?? false };
}
