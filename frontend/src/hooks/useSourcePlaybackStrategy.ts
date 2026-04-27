import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { SourceStreamDescriptor } from "@/types";

interface UseSourcePlaybackStrategyOptions {
  projectId: string;
  episode: string;
  enabled?: boolean;
  getDescriptor?: (episode: string) => Promise<SourceStreamDescriptor | null>;
}

interface SourcePlaybackStrategy {
  descriptor: SourceStreamDescriptor | null;
  loading: boolean;
  sourceUrl: string;
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

  const sourceUrl = useMemo(() => {
    if (!enabled || !episode) return "";
    return api.getSourceVideoUrl(projectId, episode);
  }, [enabled, episode, projectId]);

  return useMemo<SourcePlaybackStrategy>(
    () => ({ descriptor, loading, sourceUrl }),
    [descriptor, loading, sourceUrl],
  );
}
