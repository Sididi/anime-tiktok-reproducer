import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Loader2, Check, Download, Package, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui";
import { useProjectStore } from "@/stores";

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
  // Gap detection fields
  gaps_detected?: boolean;
  gap_count?: number;
  total_gap_duration?: number;
}

export function ProcessingPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { loadProject } = useProjectStore();
  const hasStartedProcessing = useRef(false);

  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [gapsDetected, setGapsDetected] = useState(false);
  const [gapInfo, setGapInfo] = useState<{ count: number; duration: number } | null>(null);
  const [steps, setSteps] = useState<ProcessingStep[]>([
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
    { id: "bundling", label: "Bundling project assets", status: "pending" },
  ]);

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

  const startProcessing = useCallback(async () => {
    if (!projectId) return;

    setProcessing(true);
    setError(null);

    try {
      const response = await fetch(`/api/projects/${projectId}/process`, {
        method: "POST",
      });

      if (!response.ok) {
        throw new Error("Failed to start processing");
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("No response body");
      }

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const data = JSON.parse(line.slice(6)) as ProcessingProgress;

              // Handle gaps detected - pause processing and redirect
              if (data.status === "gaps_detected" && data.gaps_detected) {
                setGapsDetected(true);
                setGapInfo({
                  count: data.gap_count || 0,
                  duration: data.total_gap_duration || 0,
                });
                // Mark gap_detection step as paused
                setSteps((prev) =>
                  prev.map((step) => {
                    if (step.id === "gap_detection") {
                      return { ...step, status: "paused", message: data.message };
                    }
                    const stepIndex = prev.findIndex((s) => s.id === step.id);
                    const currentIndex = prev.findIndex((s) => s.id === "gap_detection");
                    if (stepIndex < currentIndex) {
                      return { ...step, status: "complete" };
                    }
                    return step;
                  })
                );
                setProcessing(false);
                return; // Stop processing here
              }

              // Update current step
              if (data.step) {
                // Mark previous steps as complete
                setSteps((prev) =>
                  prev.map((step) => {
                    const stepIndex = prev.findIndex((s) => s.id === step.id);
                    const currentIndex = prev.findIndex(
                      (s) => s.id === data.step,
                    );

                    if (stepIndex < currentIndex) {
                      return { ...step, status: "complete" };
                    }
                    if (step.id === data.step) {
                      return {
                        ...step,
                        status:
                          data.status === "error" ? "error" : "processing",
                        message: data.message,
                      };
                    }
                    return step;
                  }),
                );
              }

              if (data.status === "complete") {
                // Mark all steps as complete
                setSteps((prev) =>
                  prev.map((step) => ({ ...step, status: "complete" })),
                );
                if (data.download_url) {
                  setDownloadUrl(data.download_url);
                }
              }

              if (data.status === "error") {
                throw new Error(data.error || "Processing failed");
              }
            } catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }
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
      downloadUrl ||
      gapsDetected ||
      hasStartedProcessing.current
    )
      return;

    hasStartedProcessing.current = true;
    startProcessing();
  }, [projectId, loading, processing, downloadUrl, gapsDetected, startProcessing]);

  const handleDownload = () => {
    if (downloadUrl) {
      window.location.href = downloadUrl;
    }
  };

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
      <div className="max-w-lg w-full space-y-8">
        <div className="text-center space-y-2">
          <h1 className="text-2xl font-bold">
            {downloadUrl
              ? "Processing Complete!"
              : gapsDetected
              ? "Gaps Detected"
              : "Processing Your Project"}
          </h1>
          <p className="text-[hsl(var(--muted-foreground))]">
            {downloadUrl
              ? "Your Premiere Pro project bundle is ready for download"
              : gapsDetected
              ? "Some clips need adjustments to fill timeline gaps"
              : "Please wait while we generate your Premiere Pro project"}
          </p>
        </div>

        {error && (
          <div className="p-3 bg-[hsl(var(--destructive))]/10 rounded-lg">
            <p className="text-sm text-[hsl(var(--destructive))]">{error}</p>
            <Button
              variant="outline"
              size="sm"
              onClick={startProcessing}
              className="mt-2"
            >
              Retry
            </Button>
          </div>
        )}

        {/* Gap detection banner */}
        {gapsDetected && gapInfo && (
          <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-amber-500 shrink-0 mt-0.5" />
              <div className="space-y-2 flex-1">
                <p className="text-sm font-medium">
                  {gapInfo.count} clip{gapInfo.count !== 1 ? "s" : ""} hit the 75% speed floor
                </p>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  Total gap duration: {gapInfo.duration.toFixed(2)}s. You can extend these clips
                  to fill the gaps, or skip to keep them as-is.
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

        {downloadUrl && (
          <div className="space-y-4">
            <Button className="w-full h-12" onClick={handleDownload}>
              <Download className="h-5 w-5 mr-2" />
              Download Project Bundle
            </Button>
            <p className="text-xs text-center text-[hsl(var(--muted-foreground))]">
              The bundle contains: .jsx script, edited TTS audio, subtitles
              (.srt), and references to source episodes
            </p>
          </div>
        )}

        {!downloadUrl && !error && !gapsDetected && (
          <div className="flex items-center justify-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
            <Package className="h-4 w-4" />
            <span>This may take a few minutes</span>
          </div>
        )}
      </div>
    </div>
  );
}
