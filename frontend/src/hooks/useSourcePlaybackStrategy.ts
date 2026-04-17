import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { SourceStreamDescriptor } from "@/types";

export type SourcePlaybackMode = "passthrough" | "hls";

interface UseSourcePlaybackStrategyOptions {
  projectId: string;
  episode: string;
  enabled?: boolean;
  getDescriptor?: (episode: string) => Promise<SourceStreamDescriptor | null>;
}

interface SourcePlaybackStrategy {
  descriptor: SourceStreamDescriptor | null;
  loading: boolean;
  mode: SourcePlaybackMode | null;
  sourceUrl: string;
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
  getDescriptor,
}: UseSourcePlaybackStrategyOptions): SourcePlaybackStrategy {
  const [descriptor, setDescriptor] = useState<SourceStreamDescriptor | null>(
    null,
  );
  const [loading, setLoading] = useState(false);

  const loadDescriptor = useCallback(
    (episodePath: string) => {
      if (getDescriptor) return getDescriptor(episodePath);
      return api.getSourceDescriptor(projectId, episodePath);
    },
    [getDescriptor, projectId],
  );

  useEffect(() => {
    if (!enabled || !episode) {
      setDescriptor(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    void loadDescriptor(episode)
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
  }, [enabled, episode, loadDescriptor]);

  const mode = useMemo(() => resolveMode(descriptor), [descriptor]);

  const sourceUrl = useMemo(() => {
    if (!enabled || !episode || !descriptor || !mode) return "";
    if (mode === "hls") return resolveHlsUrl(descriptor);
    return api.getSourceVideoUrl(projectId, episode);
  }, [descriptor, enabled, episode, mode, projectId]);

  return useMemo<SourcePlaybackStrategy>(
    () => ({ descriptor, loading, mode, sourceUrl }),
    [descriptor, loading, mode, sourceUrl],
  );
}
