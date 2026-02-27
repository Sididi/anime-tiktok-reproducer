import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import {
  Loader2,
  Check,
  Download,
  Package,
  AlertTriangle,
  CloudUpload,
} from "lucide-react";
import { Button } from "@/components/ui";
import { useProjectStore } from "@/stores";
import { api } from "@/api/client";
import { readSSEStream } from "@/utils/sse";

interface ProcessingStep {
  id: string;
  label: string;
  status: "pending" | "processing" | "complete" | "error" | "paused";
  message?: string;
}

interface ProcessingProgress {
  status: string;
  step: string;
  progress: number;
  message: string;
  error: string | null;
  download_url?: string;
  folder_url?: string;
  // Gap detection fields
  gaps_detected?: boolean;
  gap_count?: number;
  total_gap_duration?: number;
}

const INITIAL_STEPS: ProcessingStep[] = [
  {
    id: "auto_editor",
    label: "Running auto-editor (audio + XML export)",
    status: "pending",
  },
  {
    id: "transcription",
    label: "Extracting word timings from audio",
    status: "pending",
  },
  {
    id: "gap_detection",
    label: "Checking for clips with gaps",
    status: "pending",
  },
  {
    id: "jsx_generation",
    label: "Generating Premiere Pro JSX script",
    status: "pending",
  },
  {
    id: "srt_generation",
    label: "Creating subtitles with word timing",
    status: "pending",
  },
  {
    id: "overlay_image_generation",
    label: "Generating video overlay images",
    status: "pending",
  },
];

export function ProcessingPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { loadProject } = useProjectStore();
  const hasStartedProcessing = useRef(false);
  const resumeAfterGapsRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gapsDetected, setGapsDetected] = useState(false);
  const [gapInfo, setGapInfo] = useState<{
    count: number;
    duration: number;
  } | null>(null);
  const [steps, setSteps] = useState<ProcessingStep[]>(INITIAL_STEPS);
  const [processingComplete, setProcessingComplete] = useState(false);

  const [bundleLoading, setBundleLoading] = useState(false);
  const [driveLoading, setDriveLoading] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [driveFolderUrl, setDriveFolderUrl] = useState<string | null>(null);
  const [driveUploaded, setDriveUploaded] = useState(false);

  // Load project
  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      setLoading(true);
      try {
        await loadProject(projectId);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [projectId, loadProject]);

  // Reset local processing state when returning to this page
  useEffect(() => {
    abortRef.current?.abort();
    hasStartedProcessing.current = false;
    resumeAfterGapsRef.current = Boolean(
      (location.state as { resumeAfterGaps?: boolean } | null)?.resumeAfterGaps,
    );
    setProcessing(false);
    setError(null);
    setGapsDetected(false);
    setGapInfo(null);
    setSteps(INITIAL_STEPS);
    setProcessingComplete(false);
    setBundleLoading(false);
    setDriveLoading(false);
    setActionMessage(null);
    setDriveFolderUrl(null);
    setDriveUploaded(false);
  }, [projectId, location.key, location.state]);

  const startProcessing = useCallback(async () => {
    if (!projectId) return;

    setProcessing(true);
    setError(null);
    setActionMessage(null);

    const controller = new AbortController();
    abortRef.current = controller;

    if (resumeAfterGapsRef.current) {
      setSteps((prev) =>
        prev.map((step) => {
          if (step.id === "jsx_generation") {
            return { ...step, status: "processing", message: "Resuming..." };
          }
          if (step.id === "srt_generation" || step.id === "overlay_image_generation") {
            return { ...step, status: "pending" };
          }
          return { ...step, status: "complete" };
        }),
      );
      resumeAfterGapsRef.current = false;
    }

    try {
      const response = await fetch(`/api/projects/${projectId}/process`, {
        method: "POST",
      });

      await readSSEStream<ProcessingProgress>(response, (data) => {
        if (data.status === "gaps_detected" && data.gaps_detected) {
          setGapsDetected(true);
          setGapInfo({
            count: data.gap_count || 0,
            duration: data.total_gap_duration || 0,
          });
          setSteps((prev) =>
            prev.map((step) => {
              if (step.id === "gap_detection") {
                return {
                  ...step,
                  status: "paused",
                  message: data.message,
                };
              }
              const stepIndex = prev.findIndex((s) => s.id === step.id);
              const currentIndex = prev.findIndex((s) => s.id === "gap_detection");
              if (stepIndex < currentIndex) {
                return { ...step, status: "complete" };
              }
              return step;
            }),
          );
          return;
        }

        if (data.step) {
          setSteps((prev) =>
            prev.map((step) => {
              const stepIndex = prev.findIndex((s) => s.id === step.id);
              const currentIndex = prev.findIndex((s) => s.id === data.step);
              if (stepIndex < currentIndex) {
                return { ...step, status: "complete" };
              }
              if (step.id === data.step) {
                return {
                  ...step,
                  status: data.status === "error" ? "error" : "processing",
                  message: data.message,
                };
              }
              return step;
            }),
          );
        }

        if (data.status === "complete") {
          setSteps((prev) => prev.map((step) => ({ ...step, status: "complete" })));
          setProcessingComplete(true);
          setActionMessage("Processing complete. Choose download or Drive upload.");
        }
      }, controller.signal);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProcessing(false);
    }
  }, [projectId]);

  // Start processing automatically
  useEffect(() => {
    if (
      !projectId ||
      loading ||
      processing ||
      processingComplete ||
      gapsDetected ||
      hasStartedProcessing.current
    ) {
      return;
    }
    hasStartedProcessing.current = true;
    startProcessing();
  }, [projectId, loading, processing, processingComplete, gapsDetected, startProcessing]);

  const handleBuildAndDownload = useCallback(async () => {
    if (!projectId || bundleLoading || driveLoading) return;
    setBundleLoading(true);
    setError(null);
    setActionMessage("Building project bundle...");

    try {
      const response = await api.createBundleExport(projectId);
      const finalEvent = await readSSEStream<ProcessingProgress>(response, (data) => {
        if (data.message) setActionMessage(data.message);
      });
      const downloadUrl = finalEvent?.download_url;
      if (!downloadUrl) {
        throw new Error("Bundle endpoint did not return a download URL");
      }
      window.location.href = downloadUrl;
      setActionMessage("Download started.");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBundleLoading(false);
    }
  }, [projectId, bundleLoading, driveLoading]);

  const handleUploadDrive = useCallback(async () => {
    if (!projectId || driveLoading || bundleLoading) return;
    setDriveLoading(true);
    setError(null);
    setActionMessage("Uploading project to Google Drive...");

    try {
      const response = await api.uploadExportToGDrive(projectId);
      const finalEvent = await readSSEStream<ProcessingProgress>(response, (data) => {
        if (data.message) setActionMessage(data.message);
      });
      if (finalEvent?.folder_url) {
        setDriveFolderUrl(finalEvent.folder_url);
        setDriveUploaded(true);
      }
      setActionMessage("Google Drive upload complete.");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setDriveLoading(false);
    }
  }, [projectId, driveLoading, bundleLoading]);

  const handleResolveGaps = () => {
    if (projectId) {
      navigate(`/project/${projectId}/gaps`);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="max-w-xl w-full space-y-8">
        <div className="text-center space-y-2">
          <h1 className="text-2xl font-bold">
            {processingComplete
              ? "Processing Complete"
              : gapsDetected
                ? "Gaps Detected"
                : "Processing Your Project"}
          </h1>
          <p className="text-[hsl(var(--muted-foreground))]">
            {processingComplete
              ? "Choose how to export project assets"
              : gapsDetected
                ? "Some clips need adjustments to fill timeline gaps"
                : "Please wait while we generate your Premiere Pro project"}
          </p>
        </div>

        {error && (
          <div className="p-3 bg-[hsl(var(--destructive))]/10 rounded-lg">
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
            {!processingComplete && !gapsDetected && (
              <Button
                variant="outline"
                size="sm"
                onClick={startProcessing}
                className="mt-2"
              >
                Retry
              </Button>
            )}
          </div>
        )}

        {gapsDetected && gapInfo && (
          <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-amber-500 shrink-0 mt-0.5" />
              <div className="space-y-2 flex-1">
                <p className="text-sm font-medium">
                  {gapInfo.count} clip{gapInfo.count !== 1 ? "s" : ""} hit the 75%
                  speed floor
                </p>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  Total gap duration: {gapInfo.duration.toFixed(2)}s. You can
                  extend these clips to fill the gaps, or skip to keep them as-is.
                </p>
                <Button onClick={handleResolveGaps} className="w-full mt-2">
                  <AlertTriangle className="h-4 w-4 mr-2" />
                  Resolve Gaps
                </Button>
              </div>
            </div>
          </div>
        )}

        <div className="bg-[hsl(var(--card))] rounded-lg p-6 space-y-4">
          {steps.map((step) => (
            <div key={step.id} className="flex items-start gap-3">
              <div className="shrink-0 mt-0.5">
                {step.status === "complete" ? (
                  <div className="h-5 w-5 rounded-full bg-green-500 flex items-center justify-center">
                    <Check className="h-3 w-3 text-white" />
                  </div>
                ) : step.status === "processing" ? (
                  <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--primary))]" />
                ) : step.status === "paused" ? (
                  <div className="h-5 w-5 rounded-full bg-amber-500 flex items-center justify-center">
                    <AlertTriangle className="h-3 w-3 text-white" />
                  </div>
                ) : step.status === "error" ? (
                  <div className="h-5 w-5 rounded-full bg-[hsl(var(--destructive))]" />
                ) : (
                  <div className="h-5 w-5 rounded-full border-2 border-[hsl(var(--border))]" />
                )}
              </div>
              <div className="flex-1 min-w-0">
                <p
                  className={`font-medium ${
                    step.status === "pending"
                      ? "text-[hsl(var(--muted-foreground))]"
                      : step.status === "paused"
                        ? "text-amber-500"
                        : ""
                  }`}
                >
                  {step.label}
                </p>
                {step.message && (
                  <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5">
                    {step.message}
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>

        {processingComplete && !gapsDetected && (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <Button
                className="flex-1 h-12"
                onClick={handleUploadDrive}
                disabled={driveLoading || bundleLoading || driveUploaded}
              >
                {driveLoading ? (
                  <>
                    <Loader2 className="h-5 w-5 mr-2 animate-spin" />
                    Uploading to Drive...
                  </>
                ) : driveUploaded ? (
                  <>
                    <Check className="h-5 w-5 mr-2" />
                    Uploaded to Drive
                  </>
                ) : (
                  <>
                    <CloudUpload className="h-5 w-5 mr-2" />
                    Upload to Google Drive
                  </>
                )}
              </Button>
              <Button
                variant="outline"
                size="icon"
                onClick={handleBuildAndDownload}
                disabled={bundleLoading || driveLoading}
                title="Download ZIP bundle"
                aria-label="Download ZIP bundle"
              >
                {bundleLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Download className="h-4 w-4" />
                )}
              </Button>
            </div>
            <p className="text-xs text-center text-[hsl(var(--muted-foreground))]">
              Drive and ZIP contain: JSX script, edited TTS audio, subtitles, metadata
              files, overlay images, assets, and source mapping.
            </p>
            {driveFolderUrl && (
              <p className="text-xs text-center">
                <a
                  href={driveFolderUrl}
                  className="text-[hsl(var(--primary))] underline"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open Google Drive folder
                </a>
              </p>
            )}
          </div>
        )}

        {processingComplete && (
          <div className="text-center">
            <button
              type="button"
              onClick={() => navigate("/")}
              className="text-sm text-[hsl(var(--primary))] hover:underline"
            >
              Back to Projects
            </button>
          </div>
        )}

        {actionMessage && (
          <div className="flex items-center justify-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
            {(processing || bundleLoading || driveLoading) ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Package className="h-4 w-4" />
            )}
            <span>{actionMessage}</span>
          </div>
        )}
      </div>
    </div>
  );
}
