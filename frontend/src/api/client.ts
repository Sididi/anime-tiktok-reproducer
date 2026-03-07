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

  runProjectUpload: (
    projectId: string,
    accountId?: string,
    facebookStrategy?: string,
    youtubeStrategy?: string,
  ) =>
    fetch(`${API_BASE}/project-manager/projects/${projectId}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: accountId ?? null,
        facebook_strategy: facebookStrategy ?? null,
        youtube_strategy: youtubeStrategy ?? null,
      }),
    }),

  checkFacebookDuration: (projectId: string, accountId?: string) =>
    request<import("@/types").FacebookCheckResult>(
      `/project-manager/projects/${projectId}/facebook-check`,
      {
        method: "POST",
        body: JSON.stringify({ account_id: accountId ?? null }),
      },
    ),

  getFacebookPreviewUrl: (projectId: string, version: "original" | "sped_up") =>
    `${API_BASE}/project-manager/projects/${projectId}/facebook-preview/${version}`,

  checkYouTubeDuration: (projectId: string, accountId?: string) =>
    request<import("@/types").YouTubeCheckResult>(
      `/project-manager/projects/${projectId}/youtube-check`,
      {
        method: "POST",
        body: JSON.stringify({ account_id: accountId ?? null }),
      },
    ),

  getYouTubePreviewUrl: (projectId: string, version: "original" | "sped_up") =>
    `${API_BASE}/project-manager/projects/${projectId}/youtube-preview/${version}`,

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

  getScenesConfig: (projectId: string) =>
    request<{ skip_ui_enabled: boolean }>(
      `/projects/${projectId}/scenes/config`,
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

  findMatches: (
    projectId: string,
    sourcePath?: string,
    mergeContinuous = true,
  ) => {
    return fetch(`${API_BASE}/projects/${projectId}/matches/find`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_path: sourcePath,
        merge_continuous: mergeContinuous,
      }),
    });
  },

  getMatches: (projectId: string) =>
    request<{ matches: import("@/types").SceneMatch[] }>(
      `/projects/${projectId}/matches`,
    ),

  prepareMatchesPlayback: (projectId: string, force = false) =>
    fetch(`${API_BASE}/projects/${projectId}/matches/playback/prepare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    }),

  prepareMatchesPlaybackScene: (
    projectId: string,
    sceneIndex: number,
    force = false,
  ) =>
    fetch(
      `${API_BASE}/projects/${projectId}/matches/playback/prepare-scene/${sceneIndex}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force }),
      },
    ),

  getMatchesPlaybackManifest: (projectId: string) =>
    request<import("@/types").MatchesPlaybackManifest>(
      `/projects/${projectId}/matches/playback/manifest`,
    ),

  getMatchesPlaybackClipUrl: (
    projectId: string,
    sceneIndex: number,
    track: "tiktok" | "source",
    fingerprint?: string,
  ) => {
    const suffix = fingerprint
      ? `?fingerprint=${encodeURIComponent(fingerprint)}`
      : "";
    return `${API_BASE}/projects/${projectId}/matches/playback/clip/${sceneIndex}/${track}${suffix}`;
  },

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

  updateMatchesBatch: (
    projectId: string,
    updates: Array<{
      scene_index: number;
      episode: string;
      start_time: number;
      end_time: number;
      confirmed?: boolean;
    }>,
  ) =>
    request<{ status: string; matches: import("@/types").SceneMatch[] }>(
      `/projects/${projectId}/matches`,
      {
        method: "PUT",
        body: JSON.stringify({ updates }),
      },
    ),

  undoMerge: (projectId: string, sceneIndex: number) =>
    request<{
      scenes: import("@/types").Scene[];
      matches: import("@/types").SceneMatch[];
    }>(`/projects/${projectId}/matches/undo-merge/${sceneIndex}`, {
      method: "POST",
    }),

  // Source video
  getSourceVideoUrl: (projectId: string, episodePath: string) =>
    `${API_BASE}/projects/${projectId}/video/source?path=${encodeURIComponent(episodePath)}`,

  getSourceDescriptor: (projectId: string, episodePath: string) =>
    request<import("@/types").SourceStreamDescriptor>(
      `/projects/${projectId}/video/source/descriptor?path=${encodeURIComponent(episodePath)}`,
    ),

  getSourceChunkUrl: (
    projectId: string,
    episodePath: string,
    chunkStart: number,
    chunkDuration?: number,
  ) => {
    const params = new URLSearchParams({
      path: episodePath,
      chunk_start: String(chunkStart),
    });
    if (chunkDuration !== undefined) {
      params.set("chunk_duration", String(chunkDuration));
    }
    return `${API_BASE}/projects/${projectId}/video/source/chunk?${params.toString()}`;
  },

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

  getTranscriptionConfig: (projectId: string) =>
    request<{ full_auto_enabled: boolean }>(
      `/projects/${projectId}/transcription/config`,
    ),

  getProcessingConfig: (projectId: string) =>
    request<{ gdrive_full_auto_enabled: boolean }>(
      `/projects/${projectId}/config`,
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
    request<{ status: string; next_phase?: string }>(
      `/projects/${projectId}/transcription/confirm`,
      {
        method: "POST",
      },
    ),

  // Raw Scene Validation
  getRawScenes: (projectId: string) =>
    request<{
      detection: import("@/types").RawSceneDetectionResult | null;
      transcription: import("@/types").Transcription | null;
    }>(`/projects/${projectId}/raw-scenes`),

  validateRawScenes: (
    projectId: string,
    validations: Array<{ scene_index: number; is_raw: boolean; text?: string }>,
  ) =>
    request<{ status: string; transcription: import("@/types").Transcription }>(
      `/projects/${projectId}/raw-scenes/validate`,
      {
        method: "POST",
        body: JSON.stringify({ validations }),
      },
    ),

  confirmRawScenes: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/raw-scenes/confirm`, {
      method: "POST",
    }),

  resetRawScenes: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/raw-scenes/reset`, {
      method: "POST",
    }),

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
      entries: {
        name: string;
        path: string;
        is_dir: boolean;
        has_videos: boolean;
      }[];
    }>(`/anime/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`),

  // Gap Resolution
  getGapsConfig: (projectId: string) =>
    request<{ full_auto_enabled: boolean }>(
      `/projects/${projectId}/gaps/config`,
    ),

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
    request<{
      exists: boolean;
      metadata: import("@/types").PlatformMetadata | null;
    }>(`/projects/${projectId}/metadata`),

  buildMetadataPrompt: (
    projectId: string,
    payload: { script: string; target_language: string },
  ) =>
    request<{ prompt: string }>(`/projects/${projectId}/metadata/prompt`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // Script automation (Gemini + ElevenLabs)
  getScriptAutomationConfig: (projectId: string) =>
    request<import("@/types").ScriptAutomationConfig>(
      `/projects/${projectId}/script/automation/config`,
    ),

  getScriptPrompt: (projectId: string, targetLanguage: string) =>
    request<{ prompt: string }>(
      `/projects/${projectId}/script/prompt?target_language=${encodeURIComponent(targetLanguage)}`,
    ),

  automateScript: (
    projectId: string,
    payload: {
      target_language: string;
      voice_key: string;
      existing_script_json?: Record<string, unknown>;
      skip_metadata?: boolean;
      skip_tts?: boolean;
      pause_after_script?: boolean;
      skip_overlay?: boolean;
    },
    signal?: AbortSignal,
  ) =>
    fetch(`${API_BASE}/projects/${projectId}/script/automate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    }),

  prepareScriptTts: (
    projectId: string,
    payload: {
      script_json: Record<string, unknown>;
      target_language?: string;
    },
  ) =>
    request<import("@/types").ScriptTtsPrepareResponse>(
      `/projects/${projectId}/script/tts/prepare`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

  downloadAutomationPart: (projectId: string, runId: string, partId: string) =>
    fetch(
      `${API_BASE}/projects/${projectId}/script/automate/runs/${encodeURIComponent(runId)}/parts/${encodeURIComponent(partId)}`,
      { method: "GET" },
    ),

  // Music preview
  previewMusicUrl: (projectId: string, musicKey: string) =>
    `${API_BASE}/projects/${projectId}/music/${encodeURIComponent(musicKey)}/preview`,

  // Script settings (TTS speed, music, overlay)
  getScriptSettings: (projectId: string) =>
    request<{
      tts_speed: number | null;
      music_key: string | null;
      video_overlay: import("@/types").VideoOverlay | null;
    }>(`/projects/${projectId}/script/settings`),

  updateScriptSettings: (
    projectId: string,
    payload: {
      tts_speed?: number;
      music_key?: string | null;
      video_overlay?: { title: string; category: string };
    },
  ) =>
    request<{
      status: string;
      tts_speed: number | null;
      music_key: string | null;
    }>(`/projects/${projectId}/script/settings`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  // Video overlay
  generateOverlay: (
    projectId: string,
    payload: { script_json: Record<string, unknown>; target_language: string },
  ) =>
    request<{ status: string; overlay: import("@/types").VideoOverlay }>(
      `/projects/${projectId}/script/overlay/generate`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

  // Stage preview audio (upload files for preview before final submission)
  stagePreviewAudio: (projectId: string, formData: FormData) =>
    fetch(`${API_BASE}/projects/${projectId}/script/preview/stage`, {
      method: "POST",
      body: formData,
    }).then((res) => {
      if (!res.ok) throw new Error("Failed to stage preview audio");
      return res.json() as Promise<{ staged: boolean }>;
    }),

  // Preview audio
  buildPreview: (
    projectId: string,
    payload: {
      run_id?: string | null;
      tts_speed: number;
      music_key?: string | null;
    },
  ) =>
    request<{ preview_url: string; duration_seconds: number }>(
      `/projects/${projectId}/script/preview/build`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

  // Exports
  createBundleExport: (projectId: string) =>
    fetch(`${API_BASE}/projects/${projectId}/exports/bundle`, {
      method: "POST",
    }),

  uploadExportToGDrive: (
    projectId: string,
    options?: { auto?: boolean },
  ) => {
    const params = new URLSearchParams();
    if (options?.auto) {
      params.set("auto", "true");
    }
    const query = params.toString();
    return fetch(
      `${API_BASE}/projects/${projectId}/exports/gdrive${query ? `?${query}` : ""}`,
      {
        method: "POST",
      },
    );
  },
};
