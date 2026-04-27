import { AlertTriangle, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui";
import type { SeriesDeleteReferencingProject, SourceDetails } from "@/types";

interface DeleteSourceModalProps {
  open: boolean;
  source: SourceDetails | null;
  loading: boolean;
  error: string | null;
  blockingProjects: SeriesDeleteReferencingProject[];
  onClose: () => void;
  onConfirm: () => void;
}

function formatDate(value: string | null): string | null {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function DeleteSourceModal({
  open,
  source,
  loading,
  error,
  blockingProjects,
  onClose,
  onConfirm,
}: DeleteSourceModalProps) {
  if (!open || !source) {
    return null;
  }

  const blocked = blockingProjects.length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={() => {
        if (!loading) {
          onClose();
        }
      }}
    >
      <div
        className="w-full max-w-xl rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-6 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-4 flex items-center gap-3">
          <div className="rounded-full bg-red-500/10 p-2 text-red-500">
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-lg font-semibold">
              Supprimer définitivement « {source.name} » ?
            </h2>
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              Cette action supprime la série localement, son cache d&apos;index et
              sa release sur le Storage Box.
            </p>
          </div>
        </div>

        {!blocked && (
          <div className="rounded-lg bg-red-500/10 px-4 py-3 text-sm text-red-200">
            Cette suppression est irréversible. Les épisodes, l&apos;index local et
            les métadonnées distantes seront retirés proprement.
          </div>
        )}

        {error && (
          <div
            className={`mt-4 rounded-lg px-4 py-3 text-sm ${
              blocked
                ? "bg-amber-500/10 text-amber-200"
                : "bg-red-500/10 text-red-200"
            }`}
          >
            {error}
          </div>
        )}

        {blocked && (
          <div className="mt-4">
            <div className="mb-2 text-sm font-medium">
              Projets qui bloquent la suppression
            </div>
            <div className="max-h-64 space-y-2 overflow-y-auto rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--secondary))]/40 p-3">
              {blockingProjects.map((project) => {
                const scheduledAt = formatDate(project.scheduled_at);
                const uploadedAt = formatDate(project.upload_completed_at);
                return (
                  <div
                    key={project.project_id}
                    className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-3 py-2"
                  >
                    <div className="font-medium">
                      {project.anime_title || source.name}
                    </div>
                    <div className="text-xs text-[hsl(var(--muted-foreground))]">
                      Projet {project.project_id} · phase {project.phase}
                    </div>
                    {scheduledAt && (
                      <div className="text-xs text-[hsl(var(--muted-foreground))]">
                        Programmé : {scheduledAt}
                      </div>
                    )}
                    {uploadedAt && (
                      <div className="text-xs text-[hsl(var(--muted-foreground))]">
                        Upload terminé : {uploadedAt}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <p className="mt-3 text-xs text-[hsl(var(--muted-foreground))]">
              Supprimez ou réaffectez ces projets avant de relancer la suppression.
            </p>
          </div>
        )}

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="ghost" onClick={onClose} disabled={loading}>
            Annuler
          </Button>
          <Button
            variant="destructive"
            onClick={onConfirm}
            disabled={loading || blocked}
          >
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Suppression...
              </>
            ) : (
              <>
                <Trash2 className="mr-2 h-4 w-4" />
                Supprimer définitivement
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
