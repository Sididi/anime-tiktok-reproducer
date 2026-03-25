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

  activateProjectLibrary: (projectId: string) =>
    request<import("@/types").LibraryActivationState>(
      `/projects/${projectId}/library/activate`,
      { method: "POST" },
    ),

  getProjectLibraryActivation: (projectId: string) =>
    request<import("@/types").LibraryActivationState>(
      `/projects/${projectId}/library/activation`,
    ),

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
    fetch(`${API_BASE}/project-manager/projects/${projectId}/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: accountId ?? null,
        facebook_strategy: facebookStrategy ?? null,
        youtube_strategy: youtubeStrategy ?? null,
        copyright_audio_path: copyrightAudioPath ?? null,
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

  checkCopyright: (projectId: string, accountId?: string) =>
    request<import("@/types").CopyrightCheckResult>(
      `/project-manager/projects/${projectId}/copyright-check`,
      {
        method: "POST",
        body: JSON.stringify({ account_id: accountId ?? null }),
      },
    ),

  buildCopyrightAudio: (projectId: string, musicKey: string | null, noMusicFileId: string) =>
    request<{ audio_path: string }>(
      `/project-manager/projects/${projectId}/copyright-build-audio`,
      {
        method: "POST",
        body: JSON.stringify({ music_key: musicKey, no_music_file_id: noMusicFileId }),
      },
    ),

  getCopyrightAudioUrl: (projectId: string) =>
    `${API_BASE}/project-manager/projects/${projectId}/copyright-audio`,

  getCopyrightVideoUrl: (projectId: string) =>
    `${API_BASE}/project-manager/projects/${projectId}/copyright-video`,

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

  validateBatchFolders: (
    paths: string[],
    libraryType: import("@/types").LibraryType,
  ) =>
    request<{
      results: Array<{
        path: string;
        name: string;
        has_videos: boolean;
        suggested_path: string | null;
        index_status: "new" | "exact_match" | "conflict";
        conflict_details: {
          new_episodes: string[];
          removed_episodes: string[];
          existing_episode_count: number;
          existing_torrent_count: number;
        } | null;
      }>;
    }>("/anime/validate-batch-folders", {
      method: "POST",
      body: JSON.stringify({ paths, library_type: libraryType }),
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

  // Library - Jobs
  listIndexationJobs: () =>
    request<{ jobs: import("@/types").IndexationJob[] }>("/anime/jobs"),

  streamIndexationJobs: () =>
    fetch(`${API_BASE}/anime/jobs/stream`),

  // Library - Purge
  purgeLibrary: (libraryType: import("@/types").LibraryType, allTypes: boolean) =>
    request<import("@/types").PurgeResult>("/anime/purge", {
      method: "POST",
      body: JSON.stringify({ library_type: libraryType, all_types: allTypes }),
    }),

  // Library - Purge protection
  togglePermanentPin: (libraryType: import("@/types").LibraryType, seriesId: string) =>
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

  evictSeries: (
    libraryType: import("@/types").LibraryType,
    seriesId: string,
  ) =>
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

  // Library - Estimate purge size
  estimatePurgeSize: (libraryType: import("@/types").LibraryType, allTypes: boolean) =>
    request<{ estimated_bytes: number; source_count: number }>(
      `/anime/purge/estimate?library_type=${encodeURIComponent(libraryType)}&all_types=${allTypes}`,
    ),

  // TikTok URL duplicate check
  checkTiktokUrl: (url: string) =>
    request<{ exists: boolean; video_id: string | null; registered_at: string | null }>(
      "/tiktok-urls/check",
      { method: "POST", body: JSON.stringify({ url }) },
    ),

  // Duration Warning
  acknowledgeDurationWarning: (projectId: string) =>
    request<{ status: string }>(
      `/projects/${projectId}/duration-warning/acknowledge`,
      { method: "POST" },
    ),

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
    fetch(`${API_BASE}/anime/${encodeURIComponent(sourceName)}/torrents/replace`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        library_type: libraryType,
        replacements,
      }),
    }),

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
};
