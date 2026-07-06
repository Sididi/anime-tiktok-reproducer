const API_BASE = "/api";
const VITE_MEDIA_ORIGIN =
  typeof import.meta !== "undefined" &&
  import.meta.env &&
  typeof import.meta.env.VITE_MEDIA_ORIGIN === "string"
    ? import.meta.env.VITE_MEDIA_ORIGIN.trim()
    : "";
const MEDIA_ORIGIN = VITE_MEDIA_ORIGIN || "http://127.0.0.1:8000";
const MEDIA_API_BASE = `${MEDIA_ORIGIN}${API_BASE}`;
const DEFAULT_INDEX_BATCH_SIZE = 64;
const DEFAULT_INDEX_PREFETCH_BATCHES = 3;
const DEFAULT_INDEX_TRANSFORM_WORKERS = 4;
const DEFAULT_INDEX_DECODE_BACKEND = "auto";
const DEFAULT_INDEX_PRECISION = "auto";

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

interface GapsConfig {
  full_auto_enabled: boolean;
  min_speed_factor: number;
}

interface GapsResponse {
  has_gaps: boolean;
  gaps: GapInfo[];
  total_gap_duration: number;
  min_speed_factor: number;
}

export class SeriesDeleteConflictError extends Error {
  code: string;
  referencingProjects: import("@/types").SeriesDeleteReferencingProject[];

  constructor(detail: import("@/types").SeriesDeleteConflictDetail) {
    super(detail.message || "Suppression bloquee");
    this.name = "SeriesDeleteConflictError";
    this.code = detail.code;
    this.referencingProjects = detail.referencing_projects;
  }
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
    const detail = error?.detail;
    if (typeof detail === "string") {
      throw new Error(detail || "Request failed");
    }
    if (detail && typeof detail === "object") {
      const message =
        ("message" in detail && typeof detail.message === "string"
          ? detail.message
          : null) ||
        ("code" in detail && typeof detail.code === "string"
          ? detail.code
          : null);
      throw new Error(message || "Request failed");
    }
    throw new Error("Request failed");
  }

  return res.json();
}

function toMediaUrl(pathOrUrl: string): string {
  if (/^https?:\/\//i.test(pathOrUrl)) {
    return pathOrUrl;
  }
  if (pathOrUrl.startsWith("/")) {
    return `${MEDIA_ORIGIN}${pathOrUrl}`;
  }
  return `${MEDIA_API_BASE}${pathOrUrl.startsWith("/") ? pathOrUrl : `/${pathOrUrl}`}`;
}

export const api = {
  getMediaOrigin: () => MEDIA_ORIGIN,
  toMediaUrl,

  // Projects
  createProject: (
    tiktokUrl?: string,
    sourcePath?: string,
    animeName?: string,
    seriesId?: string,
    libraryType: import("@/types").LibraryType = "anime",
  ) =>
    request<import("@/types").Project>("/projects", {
      method: "POST",
      body: JSON.stringify({
        tiktok_url: tiktokUrl,
        source_path: sourcePath,
        anime_name: animeName,
        series_id: seriesId,
        library_type: libraryType,
      }),
    }),

  startProjectAsync: (
    tiktokUrl: string,
    animeName: string | undefined,
    seriesId: string | undefined,
    libraryType: import("@/types").LibraryType = "anime",
  ) =>
    request<import("@/types").ProjectStartupJob>("/projects/start-async", {
      method: "POST",
      body: JSON.stringify({
        tiktok_url: tiktokUrl,
        anime_name: animeName,
        series_id: seriesId,
        library_type: libraryType,
      }),
    }),

  listProjects: () => request<import("@/types").Project[]>("/projects"),

  getProject: (id: string) =>
    request<import("@/types").Project>(`/projects/${id}`),

  updateProject: (
    id: string,
    data: {
      anime_name?: string;
      series_id?: string;
      library_type?: import("@/types").LibraryType;
    },
  ) =>
    request<import("@/types").Project>(`/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  deleteProject: (id: string) =>
    request<{ status: string }>(`/projects/${id}`, { method: "DELETE" }),

  duplicateProject: (id: string, variants: import("@/types").DuplicationVariant[]) =>
    request<{ projects: import("@/types").Project[] }>(
      `/projects/${id}/duplicate`,
      {
        method: "POST",
        body: JSON.stringify({ variants }),
      },
    ),

  activateProjectLibrary: (projectId: string) =>
    request<import("@/types").LibraryActivationState>(
      `/projects/${projectId}/library/activate`,
      { method: "POST" },
    ),

  getProjectLibraryActivation: (projectId: string) =>
    request<import("@/types").LibraryActivationState>(
      `/projects/${projectId}/library/activation`,
    ),

  retryProjectStartup: (projectId: string) =>
    request<import("@/types").ProjectStartupJob>(
      `/projects/${projectId}/startup/retry`,
      { method: "POST" },
    ),

  listProjectStartupJobs: () =>
    request<{ jobs: import("@/types").ProjectStartupJob[] }>(
      "/projects/startup/jobs",
    ),

  streamProjectStartupJobs: () =>
    fetch(`${API_BASE}/projects/startup/jobs/stream`),

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
    copyrightAudioPath?: string,
  ) =>
    request<import("@/types").ProjectUploadJob>(
      `/project-manager/projects/${projectId}/upload`,
      {
        method: "POST",
        body: JSON.stringify({
          account_id: accountId ?? null,
          facebook_strategy: facebookStrategy ?? null,
          youtube_strategy: youtubeStrategy ?? null,
          copyright_audio_path: copyrightAudioPath ?? null,
        }),
      },
    ),

  getUploadRestrictions: (projectId: string) =>
    request<import("@/types").UploadRestrictions>(
      `/project-manager/projects/${projectId}/upload-restrictions`,
    ),

  listProjectUploadJobs: () =>
    request<{ jobs: import("@/types").ProjectUploadJob[] }>(
      "/project-manager/upload-jobs",
    ),

  streamProjectUploadJobs: () =>
    fetch(`${API_BASE}/project-manager/upload-jobs/stream`),

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

  checkCopyright: (projectId: string, accountId?: string) =>
    request<import("@/types").CopyrightCheckResult>(
      `/project-manager/projects/${projectId}/copyright-check`,
      {
        method: "POST",
        body: JSON.stringify({ account_id: accountId ?? null }),
      },
    ),

  buildCopyrightAudio: (
    projectId: string,
    musicKey: string | null,
    noMusicFileId: string,
  ) =>
    request<{ audio_path: string }>(
      `/project-manager/projects/${projectId}/copyright-build-audio`,
      {
        method: "POST",
        body: JSON.stringify({
          music_key: musicKey,
          no_music_file_id: noMusicFileId,
        }),
      },
    ),

  getCopyrightAudioUrl: (projectId: string) =>
    `${API_BASE}/project-manager/projects/${projectId}/copyright-audio`,

  getCopyrightVideoUrl: (projectId: string) =>
    `${API_BASE}/project-manager/projects/${projectId}/copyright-video`,

  deleteManagedProject: (projectId: string, confirmed = false) =>
    request<{
      status: string;
      local_deleted: boolean;
      drive_deleted: boolean;
      archive: { folder_id: string; folder_url: string; files_copied: number } | null;
      unscheduled: Record<string, string>;
    }>(
      `/project-manager/projects/${projectId}?confirmed=${confirmed}`,
      { method: "DELETE" },
    ),

  // Video
  getVideoInfo: (projectId: string) =>
    request<import("@/types").VideoInfo>(`/projects/${projectId}/video/info`),

  getVideoUrl: (projectId: string) =>
    `${MEDIA_API_BASE}/projects/${projectId}/video`,

  getProjectPreviewUrl: (projectId: string) =>
    `${MEDIA_API_BASE}/projects/${projectId}/video/preview`,

  warmProjectPreview: (projectId: string) =>
    request<{ status: string; ready: boolean }>(
      `/projects/${projectId}/video/preview/warmup`,
      { method: "POST" },
    ),

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
  detectScenes: (projectId: string, threshold = 16.0, minSceneLen = 10) => {
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

  getMatchesConfig: (projectId: string) =>
    request<{ full_auto_enabled: boolean }>(
      `/projects/${projectId}/matches/config`,
    ),

  // Deferred download — check and download missing source episodes
  deferredDownload: (projectId: string) =>
    fetch(`${API_BASE}/projects/${projectId}/matches/deferred-download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    }),

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

  getMatchesPlaybackClipUrl: (projectId: string, clipId: string) =>
    `${MEDIA_API_BASE}/projects/${projectId}/matches/playback/clips/${clipId}`,

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

  mergeMatchWithPrevious: (projectId: string, sceneIndex: number) =>
    request<{
      scenes: import("@/types").Scene[];
      matches: import("@/types").SceneMatch[];
    }>(`/projects/${projectId}/matches/merge-with-previous/${sceneIndex}`, {
      method: "POST",
    }),

  undoMerge: (projectId: string, sceneIndex: number) =>
    request<{
      scenes: import("@/types").Scene[];
      matches: import("@/types").SceneMatch[];
    }>(`/projects/${projectId}/matches/undo-merge/${sceneIndex}`, {
      method: "POST",
    }),

  // Source video
  getSourceVideoUrl: (projectId: string, episodePath: string) =>
    `${MEDIA_API_BASE}/projects/${projectId}/video/source?path=${encodeURIComponent(episodePath)}`,

  getSourcePreviewUrl: (projectId: string, episodePath: string) =>
    `${MEDIA_API_BASE}/projects/${projectId}/video/source/preview?path=${encodeURIComponent(episodePath)}`,

  warmSourcePreview: (projectId: string, episodePath: string) =>
    request<{ status: string; ready: boolean }>(
      `/projects/${projectId}/video/source/preview/warmup?path=${encodeURIComponent(episodePath)}`,
      { method: "POST" },
    ),

  getSourceDescriptor: (projectId: string, episodePath: string) =>
    request<import("@/types").SourceStreamDescriptor>(
      `/projects/${projectId}/video/source/descriptor?path=${encodeURIComponent(episodePath)}`,
    ),

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
  listIndexedAnime: (libraryType: import("@/types").LibraryType) =>
    request<{ series: string[]; count: number }>(
      `/anime/list?library_type=${encodeURIComponent(libraryType)}`,
    ),

  indexAnime: (
    sourcePath: string,
    libraryType: import("@/types").LibraryType,
    animeName?: string,
    fps = 2.0,
  ) => {
    return fetch(`${API_BASE}/anime/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_path: sourcePath,
        library_type: libraryType,
        anime_name: animeName,
        fps,
        batch_size: DEFAULT_INDEX_BATCH_SIZE,
        prefetch_batches: DEFAULT_INDEX_PREFETCH_BATCHES,
        transform_workers: DEFAULT_INDEX_TRANSFORM_WORKERS,
        decode_backend: DEFAULT_INDEX_DECODE_BACKEND,
        precision: DEFAULT_INDEX_PRECISION,
        require_gpu: true,
      }),
    });
  },

  checkFolders: (path: string) =>
    request<{ path: string; folders: string[] }>("/anime/check-folders", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  validateBatchFolders: (
    items: Array<string | { path: string; name?: string }>,
    libraryType: import("@/types").LibraryType,
  ) =>
    request<{
      results: Array<{
        path: string;
        name: string;
        has_videos: boolean;
        suggested_path: string | null;
        resolution:
          | "new"
          | "exact_match"
          | "update_required"
          | "needs_fix"
          | "blocked_orphan";
        series_id: string | null;
        storage_release_id: string | null;
        conflict_details: {
          new_episodes: string[];
          removed_episodes: string[];
          existing_episode_count: number;
          existing_torrent_count: number;
        } | null;
        orphan_reason: string | null;
        invalid_video_files: string[] | null;
      }>;
    }>("/anime/validate-batch-folders", {
      method: "POST",
      body: JSON.stringify({
        items: items.map((item) =>
          typeof item === "string" ? { path: item } : item,
        ),
        library_type: libraryType,
      }),
    }),

  browseDirectories: (path?: string) =>
    request<import("@/types").BrowseResult>(
      `/anime/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),

  // Library - Source details
  getSourceDetails: (libraryType: import("@/types").LibraryType) =>
    request<import("@/types").SourceDetails[]>(
      `/anime/source-details?library_type=${encodeURIComponent(libraryType)}`,
    ),

  // Library - Async indexation
  indexAnimeAsync: (
    sourcePath: string,
    libraryType: import("@/types").LibraryType,
    animeName?: string,
    fps = 2.0,
  ) =>
    request<{ job_id: string }>("/anime/index-async", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        library_type: libraryType,
        anime_name: animeName,
        fps,
      }),
    }),

  updateAnimeAsync: (
    sourcePath: string,
    libraryType: import("@/types").LibraryType,
    animeName: string,
  ) =>
    request<{ job_id: string }>("/anime/update-async", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        library_type: libraryType,
        anime_name: animeName,
      }),
    }),

  // Library - Jobs
  listIndexationJobs: () =>
    request<{ jobs: import("@/types").IndexationJob[] }>("/anime/jobs"),

  streamIndexationJobs: () => fetch(`${API_BASE}/anime/jobs/stream`),

  // Library - Purge
  purgeLibrary: (
    libraryType: import("@/types").LibraryType,
    allTypes: boolean,
  ) =>
    request<import("@/types").PurgeResult>("/anime/purge", {
      method: "POST",
      body: JSON.stringify({ library_type: libraryType, all_types: allTypes }),
    }),

  // Library - Purge protection
  togglePermanentPin: (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
  ) =>
    request<{ permanent_pin: boolean; hydration_started: boolean }>(
      `/anime/${encodeURIComponent(seriesId)}/pin?library_type=${encodeURIComponent(libraryType)}`,
      { method: "PATCH" },
    ),

  hydrateSeries: (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
    payload?: { episode_keys?: string[]; full_series?: boolean },
  ) =>
    request<import("@/types").LibraryActivationState>(
      `/anime/${encodeURIComponent(seriesId)}/hydrate`,
      {
        method: "POST",
        body: JSON.stringify({
          library_type: libraryType,
          episode_keys: payload?.episode_keys ?? [],
          full_series: payload?.full_series ?? false,
        }),
      },
    ),

  getSeriesState: (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
  ) =>
    request<import("@/types").LibraryActivationState>(
      `/anime/${encodeURIComponent(seriesId)}/state?library_type=${encodeURIComponent(libraryType)}`,
    ),

  evictSeries: (libraryType: import("@/types").LibraryType, seriesId: string) =>
    request<import("@/types").LibraryActivationState>(
      `/anime/${encodeURIComponent(seriesId)}/evict`,
      {
        method: "POST",
        body: JSON.stringify({ library_type: libraryType }),
      },
    ),

  getEpisodeSources: (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
  ) =>
    request<import("@/types").EpisodeSourcesPayload>(
      `/anime/${encodeURIComponent(seriesId)}/episodes?library_type=${encodeURIComponent(libraryType)}`,
    ),

  renameSeries: (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
    newName: string,
  ) =>
    request<import("@/types").RenameSeriesResponse>(
      `/anime/${encodeURIComponent(seriesId)}/rename`,
      {
        method: "PATCH",
        body: JSON.stringify({
          library_type: libraryType,
          new_name: newName,
        }),
      },
    ),

  deleteSeries: async (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
  ) => {
    const res = await fetch(
      `${API_BASE}/anime/${encodeURIComponent(seriesId)}?library_type=${encodeURIComponent(libraryType)}`,
      { method: "DELETE" },
    );

    if (res.ok) {
      // Some deployments return 204 (or an empty body) for DELETE.
      // Treat that as success instead of throwing on JSON parsing.
      const text = await res.text();
      if (!text.trim()) {
        return {
          status: "deleted",
          series_id: seriesId,
          library_type: libraryType,
        } as import("@/types").DeleteSeriesResponse;
      }
      return JSON.parse(text) as import("@/types").DeleteSeriesResponse;
    }

    const error = await res.json().catch(() => ({ detail: "Request failed" }));
    const detail = error?.detail;
    if (
      res.status === 409 &&
      detail &&
      typeof detail === "object" &&
      Array.isArray(detail.referencing_projects)
    ) {
      throw new SeriesDeleteConflictError({
        code:
          typeof detail.code === "string"
            ? detail.code
            : "series_delete_blocked",
        message:
          typeof detail.message === "string"
            ? detail.message
            : "Cette source est encore referencee par des projets.",
        referencing_projects:
          detail.referencing_projects as import("@/types").SeriesDeleteReferencingProject[],
      });
    }
    if (typeof detail === "string") {
      throw new Error(detail || "Request failed");
    }
    if (detail && typeof detail === "object") {
      const message =
        ("message" in detail && typeof detail.message === "string"
          ? detail.message
          : null) ||
        ("code" in detail && typeof detail.code === "string"
          ? detail.code
          : null);
      throw new Error(message || "Request failed");
    }
    throw new Error("Request failed");
  },

  // Library - Estimate purge size
  estimatePurgeSize: (
    libraryType: import("@/types").LibraryType,
    allTypes: boolean,
  ) =>
    request<{ estimated_bytes: number; source_count: number }>(
      `/anime/purge/estimate?library_type=${encodeURIComponent(libraryType)}&all_types=${allTypes}`,
    ),

  // TikTok URL duplicate check
  checkTiktokUrl: (url: string) =>
    request<{
      exists: boolean;
      video_id: string | null;
      registered_at: string | null;
    }>("/tiktok-urls/check", { method: "POST", body: JSON.stringify({ url }) }),

  // Duration Warning
  acknowledgeDurationWarning: (projectId: string) =>
    request<{ status: string }>(
      `/projects/${projectId}/duration-warning/acknowledge`,
      { method: "POST" },
    ),

  // Gap Resolution
  getGapsConfig: (projectId: string) =>
    request<GapsConfig>(`/projects/${projectId}/gaps/config`),

  getGaps: (projectId: string) =>
    request<GapsResponse>(`/projects/${projectId}/gaps`),

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

  getLatestGeneration: (projectId: string) =>
    request<{
      exists: boolean;
      source: "automation_run" | "project_root" | null;
      run_id: string | null;
      script_json: Record<string, unknown> | null;
      parts: import("@/types").ScriptAutomationPart[];
    }>(`/projects/${projectId}/script/latest-generation`),

  // Music preview
  previewMusicUrl: (projectId: string, musicKey: string) =>
    `${API_BASE}/projects/${projectId}/music/${encodeURIComponent(musicKey)}/preview`,

  // Script settings (TTS speed, music, overlay)
  getScriptSettings: (projectId: string) =>
    request<{
      tts_speed: number | null;
      music_key: string | null;
      video_overlay: import("@/types").VideoOverlay | null;
      voice_key: string | null;
    }>(`/projects/${projectId}/script/settings`),

  updateScriptSettings: (
    projectId: string,
    payload: {
      tts_speed?: number;
      music_key?: string | null;
      video_overlay?: { title: string; category: string };
      voice_key?: string | null;
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

  updateScriptPhaseSettings: (
    projectId: string,
    payload: import("../types").ScriptPhaseSettingsRequest,
  ) =>
    request<import("../types").ScriptPhaseSettingsResponse>(
      `/projects/${projectId}/script/phase-settings`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),

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

  uploadExportToGDrive: (projectId: string, options?: { auto?: boolean }) => {
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

  // Torrent management
  getSourceTorrents: (
    libraryType: import("@/types").LibraryType,
    sourceName: string,
  ) =>
    request<import("@/types").SourceTorrentMetadata>(
      `/anime/${encodeURIComponent(sourceName)}/torrents?library_type=${encodeURIComponent(libraryType)}`,
    ),

  replaceTorrents: (
    sourceName: string,
    libraryType: import("@/types").LibraryType,
    replacements: Array<{ torrent_id: string; new_magnet_uri: string }>,
  ) =>
    fetch(
      `${API_BASE}/anime/${encodeURIComponent(sourceName)}/torrents/replace`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          library_type: libraryType,
          replacements,
        }),
      },
    ),

  confirmReindex: (
    sourceName: string,
    libraryType: import("@/types").LibraryType,
    torrentIds: string[],
  ) =>
    fetch(
      `${API_BASE}/anime/${encodeURIComponent(sourceName)}/torrents/replace/confirm-reindex`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          library_type: libraryType,
          torrent_ids: torrentIds,
        }),
      },
    ),

  // Scheduling
  async listPlanningEvents(
    params: {
      account_id?: string | null;
      platforms?: import("@/types").Platform[];
      range_start?: string;
      range_end?: string;
    } = {},
  ): Promise<{ events: import("@/types").PlanningEvent[] }> {
    const usp = new URLSearchParams();
    if (params.account_id) usp.set("account_id", params.account_id);
    if (params.platforms?.length)
      usp.set("platforms", params.platforms.join(","));
    if (params.range_start) usp.set("range_start", params.range_start);
    if (params.range_end) usp.set("range_end", params.range_end);
    const qs = usp.toString();
    return request(`/scheduling/events${qs ? `?${qs}` : ""}`);
  },

  async listFreeSlots(params: {
    account_id: string;
    platform: import("@/types").Platform;
    after: string;
    limit?: number;
  }): Promise<{ slots: import("@/types").FreeSlot[] }> {
    const usp = new URLSearchParams({
      account_id: params.account_id,
      platform: params.platform,
      after: params.after,
      limit: String(params.limit ?? 20),
    });
    return request(`/scheduling/free-slots?${usp.toString()}`);
  },

  async resolveAnchor(payload: {
    project_id: string;
    account_id: string;
    tiktok_slot: string;
    overrides?: Partial<Record<import("@/types").Platform, string>>;
  }): Promise<import("@/types").ResolveAnchorResult> {
    return request(`/scheduling/resolve-anchor`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async reserveAnchor(
    project_id: string,
    payload: {
      account_id: string;
      tiktok_slot: string;
      overrides?: Partial<Record<import("@/types").Platform, string>>;
      steals?: Partial<Record<import("@/types").Platform, import("@/types").StealSpec>>;
    },
  ): Promise<{
    platform_schedules: Record<string, { slot: string; scheduled_at: string }>;
  }> {
    return request(`/scheduling/projects/${project_id}/reserve-anchor`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async reschedulePlatform(
    project_id: string,
    platform: import("@/types").Platform,
    new_slot: string,
  ): Promise<{
    slot: string;
    scheduled_at: string;
    notification_status: string;
  }> {
    return request(`/scheduling/projects/${project_id}/platforms/${platform}`, {
      method: "PATCH",
      body: JSON.stringify({ new_slot }),
    });
  },

  async rescheduleAnchor(
    project_id: string,
    payload: {
      tiktok_slot: string;
      overrides?: Partial<Record<import("@/types").Platform, string>>;
      steals?: Partial<Record<import("@/types").Platform, import("@/types").StealSpec>>;
    },
  ): Promise<{
    platform_schedules: Record<string, { slot: string; scheduled_at: string }>;
    notification_status: Record<string, string>;
  }> {
    return request(`/scheduling/projects/${project_id}/anchor`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async cancelPlatformSlot(
    project_id: string,
    platform: import("@/types").Platform,
  ): Promise<void> {
    await fetch(
      `${API_BASE}/scheduling/projects/${project_id}/platforms/${platform}`,
      {
        method: "DELETE",
      },
    );
  },

  async cancelAllSlots(project_id: string): Promise<void> {
    await fetch(`${API_BASE}/scheduling/projects/${project_id}/all`, {
      method: "DELETE",
    });
  },

  async cascadePreview(
    project_id: string,
    account_id: string,
  ): Promise<import("@/types").CascadePreview> {
    return request(`/scheduling/projects/${project_id}/cascade-preview`, {
      method: "POST",
      body: JSON.stringify({ account_id }),
    });
  },

  async cascadeApply(
    project_id: string,
    account_id: string,
  ): Promise<
    import("@/types").CascadePreview & {
      notification_status: Record<string, Record<string, string>>;
    }
  > {
    return request(`/scheduling/projects/${project_id}/cascade-apply`, {
      method: "POST",
      body: JSON.stringify({ account_id }),
    });
  },

  async reserveManual(
    project_id: string,
    payload: {
      account_id: string;
      at: string;
      platforms?: import("@/types").Platform[];
    },
  ): Promise<{
    platform_schedules: Record<
      string,
      { slot: string; scheduled_at: string; manual: boolean }
    >;
    notification_status: Record<string, string>;
  }> {
    return request(`/scheduling/projects/${project_id}/reserve-manual`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async switchPreview(
    project_id: string,
    payload: {
      account_id: string;
      platform: import("@/types").Platform;
      slot: string;
    },
  ): Promise<import("@/types").SwitchPreview> {
    return request(`/scheduling/projects/${project_id}/switch-preview`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async switchApply(
    project_id: string,
    payload: {
      account_id: string;
      platform: import("@/types").Platform;
      slot: string;
      mode: import("@/types").SwitchMode;
      expected_occupant_id: string | null;
    },
  ): Promise<
    import("@/types").SwitchPreview & {
      notification_status: Record<string, string>;
    }
  > {
    return request(`/scheduling/projects/${project_id}/switch-apply`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};
