const API_BASE = "/api";

// Gap resolution types
interface GapInfo {
  scene_index: number;
  episode: string;
  current_start: number;
  current_end: number;
  current_duration: number;
  timeline_start: number;
  timeline_end: number;
  target_duration: number;
  required_speed: number;
  effective_speed: number;
  gap_duration: number;
}

interface GapCandidate {
  start_time: number;
  end_time: number;
  duration: number;
  effective_speed: number;
  speed_diff: number;
  extend_type: string;
  snap_description: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
    ...options,
  });

  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(error.detail || "Request failed");
  }

  return res.json();
}

export const api = {
  // Projects
  createProject: (
    tiktokUrl?: string,
    sourcePath?: string,
    animeName?: string,
  ) =>
    request<import("@/types").Project>("/projects", {
      method: "POST",
      body: JSON.stringify({
        tiktok_url: tiktokUrl,
        source_path: sourcePath,
        anime_name: animeName,
      }),
    }),

  listProjects: () => request<import("@/types").Project[]>("/projects"),

  getProject: (id: string) =>
    request<import("@/types").Project>(`/projects/${id}`),

  updateProject: (id: string, data: { anime_name?: string }) =>
    request<import("@/types").Project>(`/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  deleteProject: (id: string) =>
    request<{ status: string }>(`/projects/${id}`, { method: "DELETE" }),

  // Accounts
  listAccounts: () =>
    request<{ accounts: import("@/types").Account[] }>("/accounts"),

  // Project manager
  listProjectManagerProjects: () =>
    request<{ projects: import("@/types").ProjectManagerRow[] }>(
      "/project-manager/projects",
    ),

  runProjectUpload: (projectId: string, accountId?: string) =>
    fetch(`${API_BASE}/project-manager/projects/${projectId}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId ?? null }),
    }),

  deleteManagedProject: (projectId: string) =>
    request<{ status: string; local_deleted: boolean; drive_deleted: boolean }>(
      `/project-manager/projects/${projectId}`,
      { method: "DELETE" },
    ),

  // Video
  getVideoInfo: (projectId: string) =>
    request<import("@/types").VideoInfo>(`/projects/${projectId}/video/info`),

  getVideoUrl: (projectId: string) => `${API_BASE}/projects/${projectId}/video`,

  // Scenes
  getScenes: (projectId: string) =>
    request<{ scenes: import("@/types").Scene[] }>(
      `/projects/${projectId}/scenes`,
    ),

  updateScenes: (projectId: string, scenes: import("@/types").Scene[]) =>
    request<{ scenes: import("@/types").Scene[] }>(
      `/projects/${projectId}/scenes`,
      {
        method: "PUT",
        body: JSON.stringify({ scenes }),
      },
    ),

  splitScene: (projectId: string, sceneIndex: number, timestamp: number) =>
    request<{ scenes: import("@/types").Scene[] }>(
      `/projects/${projectId}/scenes/${sceneIndex}/split`,
      {
        method: "POST",
        body: JSON.stringify({ timestamp }),
      },
    ),

  mergeScenes: (projectId: string, sceneIndices: number[]) =>
    request<{ scenes: import("@/types").Scene[] }>(
      `/projects/${projectId}/scenes/merge`,
      {
        method: "POST",
        body: JSON.stringify({ scene_indices: sceneIndices }),
      },
    ),

  // Download
  downloadVideo: (projectId: string, url: string) => {
    return fetch(`${API_BASE}/projects/${projectId}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  },

  // Scene Detection
  detectScenes: (projectId: string, threshold = 18.0, minSceneLen = 10) => {
    return fetch(`${API_BASE}/projects/${projectId}/scenes/detect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ threshold, min_scene_len: minSceneLen }),
    });
  },

  // Matching
  setSources: (projectId: string, paths: string[]) =>
    request<{ status: string; source_paths: string[] }>(
      `/projects/${projectId}/sources`,
      {
        method: "POST",
        body: JSON.stringify({ paths }),
      },
    ),

  getSources: (projectId: string) =>
    request<{ source_paths: string[] }>(`/projects/${projectId}/sources`),

  getEpisodes: (projectId: string) =>
    request<{ episodes: string[] }>(`/projects/${projectId}/sources/episodes`),

  findMatches: (projectId: string, sourcePath?: string, mergeContinuous = true) => {
    return fetch(`${API_BASE}/projects/${projectId}/matches/find`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_path: sourcePath, merge_continuous: mergeContinuous }),
    });
  },

  getMatches: (projectId: string) =>
    request<{ matches: import("@/types").SceneMatch[] }>(
      `/projects/${projectId}/matches`,
    ),

  updateMatch: (
    projectId: string,
    sceneIndex: number,
    data: {
      episode: string;
      start_time: number;
      end_time: number;
      confirmed?: boolean;
    },
  ) =>
    request<{ status: string; match: import("@/types").SceneMatch }>(
      `/projects/${projectId}/matches/${sceneIndex}`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      },
    ),

  undoMerge: (projectId: string, sceneIndex: number) =>
    request<{ scenes: import("@/types").Scene[]; matches: import("@/types").SceneMatch[] }>(
      `/projects/${projectId}/matches/undo-merge/${sceneIndex}`,
      { method: "POST" },
    ),

  // Source video
  getSourceVideoUrl: (projectId: string, episodePath: string) =>
    `${API_BASE}/projects/${projectId}/video/source?path=${encodeURIComponent(episodePath)}`,

  // Transcription
  startTranscription: (projectId: string, language = "auto") => {
    return fetch(`${API_BASE}/projects/${projectId}/transcription/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
    });
  },

  getTranscription: (projectId: string) =>
    request<{ transcription: import("@/types").Transcription | null }>(
      `/projects/${projectId}/transcription`,
    ),

  updateTranscription: (
    projectId: string,
    scenes: { scene_index: number; text: string }[],
  ) =>
    request<{ status: string; transcription: import("@/types").Transcription }>(
      `/projects/${projectId}/transcription`,
      {
        method: "PUT",
        body: JSON.stringify({ scenes }),
      },
    ),

  confirmTranscription: (projectId: string) =>
    request<{ status: string }>(
      `/projects/${projectId}/transcription/confirm`,
      {
        method: "POST",
      },
    ),

  // Anime Library
  listIndexedAnime: () =>
    request<{ series: string[]; count: number }>("/anime/list"),

  indexAnime: (sourcePath: string, animeName?: string, fps = 2.0) => {
    return fetch(`${API_BASE}/anime/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_path: sourcePath,
        anime_name: animeName,
        fps,
        batch_size: 64,
        prefetch_batches: 3,
        transform_workers: 4,
        require_gpu: true,
      }),
    });
  },

  checkFolders: (path: string) =>
    request<{ path: string; folders: string[] }>("/anime/check-folders", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  browseDirectories: (path?: string) =>
    request<{
      current_path: string;
      parent_path: string | null;
      entries: { name: string; path: string; is_dir: boolean; has_videos: boolean }[];
    }>(`/anime/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`),

  // Gap Resolution
  getGaps: (projectId: string) =>
    request<{ has_gaps: boolean; gaps: GapInfo[]; total_gap_duration: number }>(
      `/projects/${projectId}/gaps`,
    ),

  getGapCandidates: (projectId: string, sceneIndex: number) =>
    request<{ scene_index: number; candidates: GapCandidate[] }>(
      `/projects/${projectId}/gaps/${sceneIndex}/candidates`,
    ),

  updateGapTiming: (
    projectId: string,
    sceneIndex: number,
    data: { start_time: number; end_time: number; skipped?: boolean },
  ) =>
    request<{ status: string; scene_index: number }>(
      `/projects/${projectId}/gaps/${sceneIndex}`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      },
    ),

  markGapsResolved: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/gaps/mark-resolved`, {
      method: "POST",
    }),

  computeSpeed: (
    projectId: string,
    data: { start_time: number; end_time: number; target_duration: number },
  ) =>
    request<{ effective_speed: number; raw_speed: number; has_gap: boolean }>(
      `/projects/${projectId}/gaps/compute-speed`,
      {
        method: "POST",
        body: JSON.stringify(data),
      },
    ),

  // Metadata
  getProjectMetadata: (projectId: string) =>
    request<{ exists: boolean; metadata: import("@/types").PlatformMetadata | null }>(
      `/projects/${projectId}/metadata`,
    ),

  buildMetadataPrompt: (
    projectId: string,
    payload: { script: string; target_language: string },
  ) =>
    request<{ prompt: string }>(`/projects/${projectId}/metadata/prompt`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // Exports
  createBundleExport: (projectId: string) =>
    fetch(`${API_BASE}/projects/${projectId}/exports/bundle`, {
      method: "POST",
    }),

  uploadExportToGDrive: (projectId: string) =>
    fetch(`${API_BASE}/projects/${projectId}/exports/gdrive`, {
      method: "POST",
    }),
};
