import { useDownloadProgressStore } from "@/stores/downloadProgressStore";

/**
 * Floating bottom-right card showing byte-level progress for any final
 * video currently being pulled into the shared upload_source cache.
 * Mounted once at the App root — independent of any modal's lifecycle.
 */
export function DownloadProgressCard() {
  const downloads = useDownloadProgressStore((s) => s.downloads);
  const active = Object.entries(downloads).filter(
    ([, entry]) => entry.state === "in_progress",
  );

  if (active.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-[70] flex flex-col gap-2 w-72">
      {active.map(([projectId, entry]) => {
        const percent =
          entry.bytesTotal && entry.bytesTotal > 0
            ? Math.round(((entry.bytesDone ?? 0) / entry.bytesTotal) * 100)
            : undefined;
        return (
          <div
            key={projectId}
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg shadow-lg p-3"
          >
            <div className="text-xs font-medium truncate mb-1">
              {entry.title || projectId}
            </div>
            <div className="text-[11px] text-[hsl(var(--muted-foreground))] mb-1.5">
              Téléchargement de la vidéo finale…
              {percent !== undefined ? ` ${percent}%` : ""}
            </div>
            <div className="h-1.5 rounded-full bg-[hsl(var(--muted))] overflow-hidden">
              {percent !== undefined ? (
                <div
                  className="h-full bg-[hsl(var(--primary))] transition-[width] duration-300"
                  style={{ width: `${percent}%` }}
                />
              ) : (
                <div className="h-full w-1/3 bg-[hsl(var(--primary))] animate-pulse rounded-full" />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
