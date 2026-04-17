import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { SourceStreamDescriptor } from "@/types";

export type SourcePlaybackMode = "passthrough" | "hls";

const START_OFFSET_BOUNDARY_SECONDS = 30;
const START_OFFSET_PREROLL_SECONDS = 5;

function snapTargetForDescriptor(target: number | undefined): number | undefined {
  if (target === undefined || !Number.isFinite(target) || target <= START_OFFSET_PREROLL_SECONDS) {
    return undefined;
  }
  const candidate = Math.max(0, target - START_OFFSET_PREROLL_SECONDS);
  const snapped = Math.floor(candidate / START_OFFSET_BOUNDARY_SECONDS) * START_OFFSET_BOUNDARY_SECONDS;
  return snapped > 0 ? snapped : undefined;
}

interface UseSourcePlaybackStrategyOptions {
  projectId: string;
  episode: string;
  enabled?: boolean;
  targetTime?: number;
  getDescriptor?: (
    episode: string,
    options?: { targetTime?: number },
  ) => Promise<SourceStreamDescriptor | null>;
}

interface SourcePlaybackStrategy {
  descriptor: SourceStreamDescriptor | null;
  loading: boolean;
  mode: SourcePlaybackMode | null;
  sourceUrl: string;
  startOffset: number;
}

function resolveMode(
  descriptor: SourceStreamDescriptor | null,
): SourcePlaybackMode | null {
  if (!descriptor) return null;
  return descriptor.mode === "hls" ? "hls" : "passthrough";
}

function resolveHlsUrl(descriptor: SourceStreamDescriptor | null): string {
  if (!descriptor?.hls_manifest_url) return "";
  return api.toMediaUrl(descriptor.hls_manifest_url);
}

export function useSourcePlaybackStrategy({
  projectId,
  episode,
  enabled = true,
  targetTime,
  getDescriptor,
}: UseSourcePlaybackStrategyOptions): SourcePlaybackStrategy {
  const [descriptor, setDescriptor] = useState<SourceStreamDescriptor | null>(
    null,
  );
  const [loading, setLoading] = useState(false);

  const loadDescriptor = useCallback(
    (episodePath: string, options?: { targetTime?: number }) => {
      if (getDescriptor) return getDescriptor(episodePath, options);
      return api.getSourceDescriptor(projectId, episodePath, options);
    },
    [getDescriptor, projectId],
  );

  const snappedTarget = useMemo(
    () => snapTargetForDescriptor(targetTime),
    [targetTime],
  );

  useEffect(() => {
    if (!enabled || !episode) {
      setDescriptor(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    void loadDescriptor(episode, { targetTime: snappedTarget })
      .then((next) => {
        if (cancelled) return;
        setDescriptor(next);
      })
      .catch(() => {
        if (cancelled) return;
        setDescriptor(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, episode, loadDescriptor, snappedTarget]);

  const mode = useMemo(() => resolveMode(descriptor), [descriptor]);

  const sourceUrl = useMemo(() => {
    if (!enabled || !episode || !descriptor || !mode) return "";
    if (mode === "hls") return resolveHlsUrl(descriptor);
    return api.getSourceVideoUrl(projectId, episode);
  }, [descriptor, enabled, episode, mode, projectId]);

  const startOffset = useMemo(() => {
    if (!descriptor) return 0;
    return Math.max(0, descriptor.hls_start_offset ?? 0);
  }, [descriptor]);

  return useMemo<SourcePlaybackStrategy>(
    () => ({ descriptor, loading, mode, sourceUrl, startOffset }),
    [descriptor, loading, mode, sourceUrl, startOffset],
  );
}
