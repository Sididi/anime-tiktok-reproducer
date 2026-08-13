import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ChevronLeft,
  ChevronRight,
  Eraser,
  Eye,
  Loader2,
  Play,
  Plus,
  SkipForward,
  Trash2,
  Type,
} from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui";
import { readSSEStream } from "@/utils/sse";
import { useProjectStore } from "@/stores/projectStore";
import type { CleanupState, CleanupZone } from "@/types";

const MIN_ZONE_SIZE = 0.02;

type DragMode =
  | "move"
  | "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

interface DragState {
  zoneId: string;
  mode: DragMode;
  startX: number; // frame coords 0..1
  startY: number;
  original: CleanupZone;
}

interface FrameBox {
  offsetX: number;
  offsetY: number;
  width: number;
  height: number;
}

const HANDLES: Array<{ mode: DragMode; style: React.CSSProperties }> = [
  { mode: "nw", style: { left: -5, top: -5, cursor: "nwse-resize" } },
  { mode: "n", style: { left: "calc(50% - 5px)", top: -5, cursor: "ns-resize" } },
  { mode: "ne", style: { right: -5, top: -5, cursor: "nesw-resize" } },
  { mode: "e", style: { right: -5, top: "calc(50% - 5px)", cursor: "ew-resize" } },
  { mode: "se", style: { right: -5, bottom: -5, cursor: "nwse-resize" } },
  { mode: "s", style: { left: "calc(50% - 5px)", bottom: -5, cursor: "ns-resize" } },
  { mode: "sw", style: { left: -5, bottom: -5, cursor: "nesw-resize" } },
  { mode: "w", style: { left: -5, top: "calc(50% - 5px)", cursor: "ew-resize" } },
];

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function applyDrag(
  original: CleanupZone,
  mode: DragMode,
  dx: number,
  dy: number,
): CleanupZone {
  let { x, y, w, h } = original;
  if (mode === "move") {
    x = clamp01(Math.min(x + dx, 1 - w));
    y = clamp01(Math.min(y + dy, 1 - h));
    return { ...original, x, y };
  }
  let x1 = x + w;
  let y1 = y + h;
  if (mode.includes("w")) x = clamp01(Math.min(x + dx, x1 - MIN_ZONE_SIZE));
  if (mode.includes("e")) x1 = clamp01(Math.max(x1 + dx, x + MIN_ZONE_SIZE));
  if (mode.includes("n")) y = clamp01(Math.min(y + dy, y1 - MIN_ZONE_SIZE));
  if (mode.includes("s")) y1 = clamp01(Math.max(y1 + dy, y + MIN_ZONE_SIZE));
  return { ...original, x, y, w: x1 - x, h: y1 - y };
}

export function CleanupPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { project, loadProject } = useProjectStore();

  const videoRef = useRef<HTMLVideoElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const zonesRef = useRef<CleanupZone[]>([]);
  const streamAbortRef = useRef<AbortController | null>(null);

  const [zones, setZones] = useState<CleanupZone[]>([]);
  const [selectedZoneId, setSelectedZoneId] = useState<string | null>(null);
  const [frameBox, setFrameBox] = useState<FrameBox | null>(null);
  const [cleanupState, setCleanupState] = useState<CleanupState | null>(null);
  const [previewUrls, setPreviewUrls] = useState<{ before: string; after: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  zonesRef.current = zones;
  const fps = project?.video_fps || 30;
  const running = cleanupState?.status === "running";
  const complete = cleanupState?.status === "complete";

  // -- loading ---------------------------------------------------------------

  useEffect(() => {
    if (!projectId) return;
    loadProject(projectId);
    api
      .getCleanupState(projectId)
      .then((state) => {
        setCleanupState(state);
        setZones(state.zones);
        if (state.status === "running") startStream();
      })
      .catch((err) => setError((err as Error).message));
    return () => streamAbortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const startStream = useCallback(() => {
    if (!projectId) return;
    streamAbortRef.current?.abort();
    const controller = new AbortController();
    streamAbortRef.current = controller;
    void (async () => {
      try {
        const response = await api.streamCleanup(projectId, controller.signal);
        await readSSEStream<CleanupState & { status: string }>(
          response,
          (state) => setCleanupState(state),
          {
            signal: controller.signal,
            stopWhen: (state) =>
              state.status === "complete" || state.status === "error",
          },
        );
      } catch {
        // Stream interrupted; state polling resumes on reload.
      }
    })();
  }, [projectId]);

  // -- letterbox math --------------------------------------------------------

  const recomputeFrameBox = useCallback(() => {
    const video = videoRef.current;
    const overlay = overlayRef.current;
    if (!video || !overlay || !video.videoWidth || !video.videoHeight) return;
    const rect = overlay.getBoundingClientRect();
    const scale = Math.min(
      rect.width / video.videoWidth,
      rect.height / video.videoHeight,
    );
    const width = video.videoWidth * scale;
    const height = video.videoHeight * scale;
    setFrameBox({
      offsetX: (rect.width - width) / 2,
      offsetY: (rect.height - height) / 2,
      width,
      height,
    });
  }, []);

  useEffect(() => {
    window.addEventListener("resize", recomputeFrameBox);
    return () => window.removeEventListener("resize", recomputeFrameBox);
  }, [recomputeFrameBox]);

  const clientToFrame = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const overlay = overlayRef.current;
      if (!overlay || !frameBox) return null;
      const rect = overlay.getBoundingClientRect();
      return {
        x: (clientX - rect.left - frameBox.offsetX) / frameBox.width,
        y: (clientY - rect.top - frameBox.offsetY) / frameBox.height,
      };
    },
    [frameBox],
  );

  // -- zone edition ----------------------------------------------------------

  const persistZones = useCallback(
    (next: CleanupZone[]) => {
      if (!projectId) return;
      api
        .saveCleanupZones(projectId, next)
        .then((state) => setCleanupState(state))
        .catch((err) => setError((err as Error).message));
    },
    [projectId],
  );

  const addZone = useCallback(
    (kind: "subtitle" | "watermark") => {
      const zone: CleanupZone = {
        id: Math.random().toString(36).slice(2, 10),
        kind,
        ...(kind === "subtitle"
          ? { x: 0.08, y: 0.62, w: 0.84, h: 0.2 }
          : { x: 0.68, y: 0.06, w: 0.26, h: 0.08 }),
      };
      const next = [...zonesRef.current, zone];
      setZones(next);
      setSelectedZoneId(zone.id);
      persistZones(next);
    },
    [persistZones],
  );

  const removeZone = useCallback(
    (zoneId: string) => {
      const next = zonesRef.current.filter((z) => z.id !== zoneId);
      setZones(next);
      persistZones(next);
    },
    [persistZones],
  );

  const beginDrag = useCallback(
    (event: React.PointerEvent, zone: CleanupZone, mode: DragMode) => {
      if (running) return;
      event.preventDefault();
      event.stopPropagation();
      const point = clientToFrame(event.clientX, event.clientY);
      if (!point) return;
      setSelectedZoneId(zone.id);
      dragRef.current = {
        zoneId: zone.id,
        mode,
        startX: point.x,
        startY: point.y,
        original: { ...zone },
      };

      const onMove = (moveEvent: PointerEvent) => {
        const drag = dragRef.current;
        const current = clientToFrame(moveEvent.clientX, moveEvent.clientY);
        if (!drag || !current) return;
        const dx = current.x - drag.startX;
        const dy = current.y - drag.startY;
        setZones((previous) =>
          previous.map((z) =>
            z.id === drag.zoneId ? applyDrag(drag.original, drag.mode, dx, dy) : z,
          ),
        );
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        if (dragRef.current) {
          dragRef.current = null;
          persistZones(zonesRef.current);
        }
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [clientToFrame, persistZones, running],
  );

  // -- frame stepping --------------------------------------------------------

  const stepFrame = useCallback(
    (direction: 1 | -1) => {
      const video = videoRef.current;
      if (!video) return;
      video.pause();
      video.currentTime = Math.max(0, video.currentTime + direction / fps);
    },
    [fps],
  );

  // -- preview / job ---------------------------------------------------------

  const handlePreview = useCallback(async () => {
    if (!projectId || !videoRef.current) return;
    setPreviewLoading(true);
    setError(null);
    try {
      await api.renderCleanupPreview(projectId, videoRef.current.currentTime);
      const stamp = Date.now();
      setPreviewUrls({
        before: `${api.getCleanupPreviewUrl(projectId, "before")}?t=${stamp}`,
        after: `${api.getCleanupPreviewUrl(projectId, "after")}?t=${stamp}`,
      });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPreviewLoading(false);
    }
  }, [projectId]);

  const handleRun = useCallback(async () => {
    if (!projectId) return;
    setError(null);
    try {
      await api.runCleanup(projectId);
      setCleanupState((state) =>
        state ? { ...state, status: "running", progress: 0 } : state,
      );
      startStream();
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId, startStream]);

  const handleCancel = useCallback(async () => {
    if (!projectId) return;
    try {
      await api.cancelCleanup(projectId);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId]);

  const runSceneDetection = useCallback(async () => {
    if (!projectId) return;
    setDetecting(true);
    setError(null);
    try {
      const response = await api.detectScenes(projectId, undefined, 10);
      await readSSEStream<{ status: string; error?: string | null }>(
        response,
        () => undefined,
        {
          stopWhen: (data) => data.status === "complete" || data.status === "error",
        },
      );
      navigate(`/project/${projectId}/scenes`);
    } catch (err) {
      setError((err as Error).message);
      setDetecting(false);
    }
  }, [projectId, navigate]);

  const handleContinue = useCallback(async () => {
    setContinuing(true);
    await runSceneDetection();
  }, [runSceneDetection]);

  const handleSkip = useCallback(async () => {
    if (!projectId) return;
    setError(null);
    try {
      await api.skipCleanup(projectId);
      await runSceneDetection();
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId, runSceneDetection]);

  // -- render ----------------------------------------------------------------

  const zoneStyles = useMemo(() => {
    if (!frameBox) return new Map<string, React.CSSProperties>();
    const map = new Map<string, React.CSSProperties>();
    for (const zone of zones) {
      map.set(zone.id, {
        left: frameBox.offsetX + zone.x * frameBox.width,
        top: frameBox.offsetY + zone.y * frameBox.height,
        width: zone.w * frameBox.width,
        height: zone.h * frameBox.height,
      });
    }
    return map;
  }, [zones, frameBox]);

  if (!projectId) return null;

  return (
    <div className="min-h-screen bg-[hsl(var(--background))] text-[hsl(var(--foreground))] p-4 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold flex items-center gap-2">
          <Eraser className="h-5 w-5" />
          Cleanup — remove burned-in subtitles &amp; watermark
        </h1>
        <div className="text-sm text-[hsl(var(--muted-foreground))]">
          Scrub to the frame where subtitles take the most space, then fit the
          rectangle around them.
        </div>
      </div>

      <div className="flex gap-4 flex-1 min-h-0">
        {/* Player + overlay */}
        <div className="flex flex-col items-center gap-2">
          <div className="relative bg-black rounded overflow-hidden" style={{ height: "70vh", aspectRatio: "9 / 16" }}>
            <video
              ref={videoRef}
              src={api.getVideoUrl(projectId)}
              className="h-full w-full object-contain"
              controls={false}
              onLoadedMetadata={recomputeFrameBox}
              crossOrigin="anonymous"
            />
            <div ref={overlayRef} className="absolute inset-0">
              {zones.map((zone) => {
                const style = zoneStyles.get(zone.id);
                if (!style) return null;
                const selected = zone.id === selectedZoneId;
                const isSubtitle = zone.kind === "subtitle";
                return (
                  <div
                    key={zone.id}
                    className="absolute"
                    style={{
                      ...style,
                      border: `2px ${selected ? "solid" : "dashed"} ${isSubtitle ? "#38bdf8" : "#fbbf24"}`,
                      backgroundColor: isSubtitle
                        ? "rgba(56, 189, 248, 0.12)"
                        : "rgba(251, 191, 36, 0.12)",
                      cursor: running ? "default" : "move",
                    }}
                    onPointerDown={(event) => beginDrag(event, zone, "move")}
                  >
                    <span
                      className="absolute -top-5 left-0 text-[10px] font-semibold uppercase tracking-wide"
                      style={{ color: isSubtitle ? "#38bdf8" : "#fbbf24" }}
                    >
                      {zone.kind}
                    </span>
                    {selected &&
                      !running &&
                      HANDLES.map((handle) => (
                        <div
                          key={handle.mode}
                          className="absolute w-2.5 h-2.5 bg-white border border-black/40 rounded-sm"
                          style={handle.style}
                          onPointerDown={(event) =>
                            beginDrag(event, zone, handle.mode)
                          }
                        />
                      ))}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Transport */}
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => stepFrame(-1)}>
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                const video = videoRef.current;
                if (!video) return;
                if (video.paused) void video.play();
                else video.pause();
              }}
            >
              <Play className="h-4 w-4" />
            </Button>
            <Button variant="outline" size="sm" onClick={() => stepFrame(1)}>
              <ChevronRight className="h-4 w-4" />
            </Button>
            <input
              type="range"
              min={0}
              max={project?.video_duration || 60}
              step={1 / fps}
              defaultValue={0}
              className="w-64"
              onChange={(event) => {
                const video = videoRef.current;
                if (video) video.currentTime = Number(event.target.value);
              }}
            />
          </div>
        </div>

        {/* Side panel */}
        <div className="flex-1 flex flex-col gap-4 min-w-72">
          <div className="border border-[hsl(var(--border))] rounded p-3 flex flex-col gap-2">
            <div className="text-sm font-semibold">Zones</div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={running || zones.some((z) => z.kind === "subtitle")}
                onClick={() => addZone("subtitle")}
              >
                <Type className="h-4 w-4 mr-1" /> Subtitle zone
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={running}
                onClick={() => addZone("watermark")}
              >
                <Plus className="h-4 w-4 mr-1" /> Watermark zone
              </Button>
            </div>
            {zones.length === 0 && (
              <p className="text-xs text-[hsl(var(--muted-foreground))]">
                Add the subtitle zone and (optionally) watermark zones. The
                subtitle zone is only repaired on frames where text is
                detected; watermark zones are repaired for the whole video
                (slower).
              </p>
            )}
            {zones.map((zone) => (
              <div
                key={zone.id}
                className={`flex items-center justify-between text-xs px-2 py-1 rounded border ${
                  zone.id === selectedZoneId
                    ? "border-[hsl(var(--primary))]"
                    : "border-[hsl(var(--border))]"
                }`}
                onClick={() => setSelectedZoneId(zone.id)}
              >
                <span className="capitalize">{zone.kind}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={running}
                  onClick={(event: React.MouseEvent) => {
                    event.stopPropagation();
                    removeZone(zone.id);
                  }}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ))}
          </div>

          <div className="border border-[hsl(var(--border))] rounded p-3 flex flex-col gap-2">
            <div className="text-sm font-semibold">Preview</div>
            <Button
              variant="outline"
              size="sm"
              disabled={zones.length === 0 || previewLoading || running}
              onClick={handlePreview}
            >
              {previewLoading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-1" />
                  Inpainting ~4s at playhead…
                </>
              ) : (
                <>
                  <Eye className="h-4 w-4 mr-1" /> Preview at playhead
                </>
              )}
            </Button>
            {previewUrls && (
              <div className="grid grid-cols-2 gap-2">
                {(["before", "after"] as const).map((which) => (
                  <div key={which} className="flex flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
                      {which}
                    </span>
                    <video
                      src={previewUrls[which]}
                      className="w-full rounded bg-black"
                      controls
                      loop
                      muted
                      autoPlay
                    />
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="border border-[hsl(var(--border))] rounded p-3 flex flex-col gap-2">
            <div className="text-sm font-semibold">Full cleanup</div>
            {running ? (
              <>
                <div className="h-2 rounded bg-[hsl(var(--muted))] overflow-hidden">
                  <div
                    className="h-full bg-[hsl(var(--primary))] transition-all"
                    style={{ width: `${Math.round((cleanupState?.progress || 0) * 100)}%` }}
                  />
                </div>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  {cleanupState?.message || "Running…"}
                </p>
                <Button variant="outline" size="sm" onClick={handleCancel}>
                  Cancel
                </Button>
              </>
            ) : (
              <>
                {complete && (
                  <p className="text-xs text-emerald-500">
                    {cleanupState?.message || "Cleanup complete."}
                  </p>
                )}
                {cleanupState?.status === "error" && (
                  <p className="text-xs text-[hsl(var(--destructive))]">
                    {cleanupState.error}
                  </p>
                )}
                <Button
                  size="sm"
                  disabled={zones.length === 0 || detecting}
                  onClick={handleRun}
                >
                  <Eraser className="h-4 w-4 mr-1" />
                  {complete ? "Re-run cleanup" : "Launch full cleanup"}
                </Button>
                <Button
                  size="sm"
                  variant={complete ? "default" : "outline"}
                  disabled={detecting || continuing || !complete}
                  onClick={handleContinue}
                >
                  {detecting ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin mr-1" />
                      Detecting scenes…
                    </>
                  ) : (
                    "Continue → Scene detection"
                  )}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={detecting}
                  onClick={handleSkip}
                >
                  <SkipForward className="h-4 w-4 mr-1" /> Skip cleanup
                </Button>
              </>
            )}
          </div>

          {error && (
            <div className="text-sm text-[hsl(var(--destructive))]">{error}</div>
          )}
        </div>
      </div>
    </div>
  );
}
