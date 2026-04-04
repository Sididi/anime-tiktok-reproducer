import { api } from "@/api/client";

export function buildVideoSourceCandidates(
  primarySrc: string,
  fallbackSrc?: string | null,
): string[] {
  const candidates = [primarySrc, fallbackSrc ?? ""]
    .map((value) => value.trim())
    .filter(Boolean);
  return Array.from(new Set(candidates));
}

export function getProjectVideoSourceCandidates(projectId: string): string[] {
  return buildVideoSourceCandidates(
    api.getVideoUrl(projectId),
    api.getProjectPreviewUrl(projectId),
  );
}

