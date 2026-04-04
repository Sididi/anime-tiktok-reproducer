import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { SourceStreamDescriptor } from "@/types";

export const MAX_SOURCE_CHUNK_SECONDS = 120;

export type SourcePlaybackMode = "passthrough" | "chunked";

export interface ResolveSourcePlaybackModeOptions {
  playbackRate?: number;
  preferChunkedHighRateHevc?: boolean;
}

export interface SourceChunkWindowOptions {
  windowDuration?: number;
  alignment?: "center" | "guard";
  maxDuration?: number;
}

interface RetargetChunkWindowOptions extends SourceChunkWindowOptions {
  minimumDuration?: number;
}

interface UseSourcePlaybackStrategyOptions {
  projectId: string;
  episode: string;
  enabled?: boolean;
  playbackRate?: number;
  preferChunkedHighRateHevc?: boolean;
  initialTargetTime?: number;
  minimumChunkDuration?: number;
  maxChunkDuration?: number;
  chunkAlignment?: "center" | "guard";
  getDescriptor?: (episode: string) => Promise<SourceStreamDescriptor | null>;
}

interface SourcePlaybackStrategy {
  descriptor: SourceStreamDescriptor | null;
  loading: boolean;
  mode: SourcePlaybackMode | null;
  sourceUrl: string;
  chunkWindowStart: number;
  chunkWindowDuration: number;
  retargetChunkWindow: (
    targetTime: number,
    options?: RetargetChunkWindowOptions,
  ) => void;
  containsTime: (targetTime: number) => boolean;
  toLocalTime: (globalTime: number) => number;
  toGlobalTime: (playerTime: number) => number;
}

export function resolveSourcePlaybackMode(
  descriptor: SourceStreamDescriptor | null,
  options: ResolveSourcePlaybackModeOptions = {},
): SourcePlaybackMode | null {
  if (!descriptor) {
    return null;
  }

  if (descriptor.mode === "chunked") {
    return "chunked";
  }

  if (
    options.preferChunkedHighRateHevc &&
    (options.playbackRate ?? 1) >= 8
  ) {
    const codec = descriptor.codec.toLowerCase();
    const pixFmt = descriptor.pix_fmt.toLowerCase();
    if (codec === "hevc" || pixFmt.includes("10")) {
      return "chunked";
    }
  }

  return "passthrough";
}

export function clampSourceChunkWindowDuration(
  descriptor: SourceStreamDescriptor,
  requestedDuration?: number,
  maxDuration = MAX_SOURCE_CHUNK_SECONDS,
): number {
  return Math.min(
    Math.max(
      requestedDuration ?? descriptor.chunk_duration,
      descriptor.chunk_duration,
    ),
    maxDuration,
  );
}

export function computeSourceChunkWindowStart(
  targetTime: number,
  descriptor: SourceStreamDescriptor,
  options: SourceChunkWindowOptions = {},
): number {
  const windowDuration = clampSourceChunkWindowDuration(
    descriptor,
    options.windowDuration,
    options.maxDuration,
  );
  const duration = Math.max(descriptor.duration || 0, 0);
  const maxStart = Math.max(duration - windowDuration, 0);
  const boundedTarget = Math.min(Math.max(targetTime, 0), duration || targetTime);
  const step = Math.max(descriptor.chunk_step || 0.001, 0.001);

  if (options.alignment === "center") {
    const centeredStart = Math.max(boundedTarget - windowDuration / 2, 0);
    const snappedCenter = Math.floor(centeredStart / step) * step;
    return Math.min(Math.max(snappedCenter, 0), maxStart);
  }

  const guard = Math.min(
    Math.max(descriptor.seek_guard_seconds || 0, 0),
    windowDuration / 4,
  );
  const desiredStart = Math.max(boundedTarget - guard - windowDuration / 4, 0);
  const snapped = Math.floor(Math.min(desiredStart, maxStart) / step) * step;
  return Math.min(Math.max(snapped, 0), maxStart);
}

export function isTimeInsideSourceChunkWindow(
  targetTime: number,
  windowStart: number,
  descriptor: SourceStreamDescriptor,
  windowDuration?: number,
): boolean {
  const duration = clampSourceChunkWindowDuration(
    descriptor,
    windowDuration,
    windowDuration,
  );
  const guard = Math.min(
    Math.max(descriptor.seek_guard_seconds || 0, 0),
    duration / 4,
  );
  const safeStart = windowStart + guard;
  const safeEnd = windowStart + duration - guard;
  return targetTime >= safeStart && targetTime <= safeEnd;
}

export function useSourcePlaybackStrategy({
  projectId,
  episode,
  enabled = true,
  playbackRate = 1,
  preferChunkedHighRateHevc = false,
  initialTargetTime = 0,
  minimumChunkDuration,
  maxChunkDuration = MAX_SOURCE_CHUNK_SECONDS,
  chunkAlignment = "guard",
  getDescriptor,
}: UseSourcePlaybackStrategyOptions): SourcePlaybackStrategy {
  const [descriptor, setDescriptor] = useState<SourceStreamDescriptor | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [chunkWindowStart, setChunkWindowStart] = useState(0);
  const [chunkWindowDuration, setChunkWindowDuration] = useState(0);

  const loadDescriptor = useCallback(
    (episodePath: string) => {
      if (getDescriptor) {
        return getDescriptor(episodePath);
      }
      return api.getSourceDescriptor(projectId, episodePath);
    },
    [getDescriptor, projectId],
  );

  const mode = useMemo(
    () =>
      resolveSourcePlaybackMode(descriptor, {
        playbackRate,
        preferChunkedHighRateHevc,
      }),
    [descriptor, playbackRate, preferChunkedHighRateHevc],
  );

  useEffect(() => {
    if (!enabled || !episode) {
      setDescriptor(null);
      setLoading(false);
      setChunkWindowStart(0);
      setChunkWindowDuration(0);
      return;
    }

    let cancelled = false;
    setLoading(true);

    void loadDescriptor(episode)
      .then((nextDescriptor) => {
        if (cancelled) {
          return;
        }

        setDescriptor(nextDescriptor);
        if (!nextDescriptor) {
          setChunkWindowStart(0);
          setChunkWindowDuration(0);
          return;
        }

        const nextMode = resolveSourcePlaybackMode(nextDescriptor, {
          playbackRate,
          preferChunkedHighRateHevc,
        });

        if (nextMode === "chunked") {
          const nextDuration = clampSourceChunkWindowDuration(
            nextDescriptor,
            minimumChunkDuration,
            maxChunkDuration,
          );
          setChunkWindowDuration(nextDuration);
          setChunkWindowStart(
            computeSourceChunkWindowStart(initialTargetTime, nextDescriptor, {
              windowDuration: nextDuration,
              alignment: chunkAlignment,
              maxDuration: maxChunkDuration,
            }),
          );
          return;
        }

        setChunkWindowStart(0);
        setChunkWindowDuration(nextDescriptor.chunk_duration);
      })
      .catch(() => {
        if (cancelled) {
          return;
        }
        setDescriptor(null);
        setChunkWindowStart(0);
        setChunkWindowDuration(0);
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [
    chunkAlignment,
    enabled,
    episode,
    initialTargetTime,
    loadDescriptor,
    maxChunkDuration,
    minimumChunkDuration,
    playbackRate,
    preferChunkedHighRateHevc,
  ]);

  const retargetChunkWindow = useCallback(
    (targetTime: number, options: RetargetChunkWindowOptions = {}) => {
      if (!descriptor || mode !== "chunked") {
        return;
      }

      const nextDuration = clampSourceChunkWindowDuration(
        descriptor,
        options.minimumDuration ?? options.windowDuration ?? minimumChunkDuration,
        options.maxDuration ?? maxChunkDuration,
      );
      setChunkWindowDuration(nextDuration);
      setChunkWindowStart(
        computeSourceChunkWindowStart(targetTime, descriptor, {
          windowDuration: nextDuration,
          alignment: options.alignment ?? chunkAlignment,
          maxDuration: options.maxDuration ?? maxChunkDuration,
        }),
      );
    },
    [
      chunkAlignment,
      descriptor,
      maxChunkDuration,
      minimumChunkDuration,
      mode,
    ],
  );

  const sourceUrl = useMemo(() => {
    if (!enabled || !episode || !descriptor || !mode) {
      return "";
    }

    if (mode === "chunked") {
      return api.getSourceChunkUrl(
        projectId,
        episode,
        chunkWindowStart,
        chunkWindowDuration || descriptor.chunk_duration,
      );
    }

    return api.getSourceVideoUrl(projectId, episode);
  }, [
    chunkWindowDuration,
    chunkWindowStart,
    descriptor,
    enabled,
    episode,
    mode,
    projectId,
  ]);

  const containsTime = useCallback(
    (targetTime: number) => {
      if (!descriptor || mode !== "chunked") {
        return true;
      }
      return isTimeInsideSourceChunkWindow(
        targetTime,
        chunkWindowStart,
        descriptor,
        chunkWindowDuration || descriptor.chunk_duration,
      );
    },
    [chunkWindowDuration, chunkWindowStart, descriptor, mode],
  );

  const toLocalTime = useCallback(
    (globalTime: number) =>
      mode === "chunked" ? Math.max(0, globalTime - chunkWindowStart) : globalTime,
    [chunkWindowStart, mode],
  );

  const toGlobalTime = useCallback(
    (playerTime: number) =>
      mode === "chunked" ? chunkWindowStart + playerTime : playerTime,
    [chunkWindowStart, mode],
  );

  return {
    descriptor,
    loading,
    mode,
    sourceUrl,
    chunkWindowStart,
    chunkWindowDuration,
    retargetChunkWindow,
    containsTime,
    toLocalTime,
    toGlobalTime,
  };
}

