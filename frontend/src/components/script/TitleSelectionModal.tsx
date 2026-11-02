import { useMemo, useState } from "react";

import { Button } from "@/components/ui";
import { ProjectManagedVideoPlayer } from "@/components/video";
import type {
  MetadataTitleCandidatesPayload,
  VideoOverlay,
} from "@/types";

interface TitleSelectionModalProps {
  isOpen: boolean;
  projectId?: string;
  metadataCandidates?: Pick<MetadataTitleCandidatesPayload, 'title_candidates'> | null;
  allowCustomMetadataTitle?: boolean;
  initialMetadataTitle?: string;
  metadataError?: string | null;
  overlay?: VideoOverlay | null;
  overlayError?: string | null;
  isSaving?: boolean;
  error?: string;
  onClose?: () => void;
  onConfirm: (selection: {
    metadataTitle?: string;
    overlayTitle?: string;
  }) => void;
}

function SelectionList({
  options,
  selected,
  onSelect,
}: {
  options: string[];
  selected: string;
  onSelect: (value: string) => void;
}) {
  return (
    <div className="space-y-2">
      {options.map((option, index) => {
        const isSelected = option === selected;
        return (
          <button
            key={`${index}-${option}`}
            type="button"
            onClick={() => onSelect(option)}
            className={`w-full rounded-md border px-4 py-3 text-left text-sm transition ${
              isSelected
                ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))/0.12] ring-2 ring-[hsl(var(--primary))]"
                : "border-[hsl(var(--border))] bg-transparent hover:bg-[hsl(var(--muted))]"
            }`}
          >
            <span className="mr-3 font-mono text-xs text-[hsl(var(--muted-foreground))]">
              {index + 1}.
            </span>
            <span>{option}</span>
          </button>
        );
      })}
    </div>
  );
}

export function TitleSelectionModal({
  isOpen,
  projectId,
  metadataCandidates,
  allowCustomMetadataTitle = false,
  initialMetadataTitle = '',
  metadataError,
  overlay,
  overlayError,
  onConfirm,
  isSaving = false,
  error,
  onClose,
}: TitleSelectionModalProps) {
  const metadataOptions = metadataCandidates?.title_candidates ?? [];
  const overlayOptions = useMemo(() => {
    return Array.isArray(overlay?.title_hooks)
      ? overlay.title_hooks.filter(
          (hook): hook is string => typeof hook === "string" && hook.trim().length > 0,
        )
      : [];
  }, [overlay]);

  const initialIsCustom = allowCustomMetadataTitle && Boolean(initialMetadataTitle) && !metadataOptions.includes(initialMetadataTitle);
  const [selectedMetadataTitle, setSelectedMetadataTitle] = useState(initialMetadataTitle);
  const [customMetadataTitle, setCustomMetadataTitle] = useState(initialIsCustom ? initialMetadataTitle : '');
  const [useCustomMetadataTitle, setUseCustomMetadataTitle] = useState(initialIsCustom);
  const [selectedOverlayTitle, setSelectedOverlayTitle] = useState("");

  if (!isOpen) return null;

  const hasMetadataPane = metadataOptions.length > 0 || Boolean(metadataError);
  // A single hook is a template's fixed title: it is applied on confirm but
  // there is nothing for the user to decide, so the pane stays hidden.
  const hasOverlayPane = overlayOptions.length > 1 || Boolean(overlayError);
  const twoPaneLayout = hasMetadataPane && hasOverlayPane;
  const effectiveMetadataTitle =
    allowCustomMetadataTitle && useCustomMetadataTitle
      ? customMetadataTitle.trim()
      : metadataOptions.includes(selectedMetadataTitle) ? selectedMetadataTitle : metadataOptions[0] || "";
  const effectiveOverlayTitle =
    overlayOptions.includes(selectedOverlayTitle)
      ? selectedOverlayTitle
      : overlayOptions[0] || overlay?.title || "";
  const canConfirmMetadata =
    metadataOptions.length === 0 || Boolean(effectiveMetadataTitle);
  const canConfirmOverlay =
    overlayOptions.length === 0 || Boolean(effectiveOverlayTitle);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div role="dialog" aria-modal="true" aria-label="Select Titles"
        className={`flex w-full max-h-[90vh] overflow-y-auto rounded-lg bg-[hsl(var(--card))] shadow-xl ${
          twoPaneLayout ? "max-w-7xl" : "max-w-4xl"
        }`}
      >
        {/* TikTok video preview */}
        {projectId && (
          <div className="w-72 shrink-0 flex items-center justify-center bg-black rounded-l-lg overflow-hidden">
            <ProjectManagedVideoPlayer
              projectId={projectId}
              className="w-full h-auto max-h-[80vh] object-contain"
              requestLoad={isOpen}
              requestWarmup={isOpen}
              controls
              muted
            />
          </div>
        )}

        {/* Main content */}
        <div className="flex-1 min-w-0 p-5">
          <div className="mb-4">
            <h2 className="text-lg font-semibold">Select Titles</h2>
            <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
              Choose the metadata and overlay titles to keep.
            </p>
          </div>

          <div className={`gap-4 ${twoPaneLayout ? "grid md:grid-cols-2" : "space-y-4"}`}>
            {hasMetadataPane && (
              <section className="rounded-lg border border-[hsl(var(--border))] p-4">
                <div className="mb-4">
                  <h3 className="font-medium">Metadata Title</h3>
                  <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
                    {allowCustomMetadataTitle ? 'Un titre commun pour les plateformes qui acceptent un titre. Les descriptions Facecam gardent leur format dédié.' : 'This title will be reused across YouTube, Facebook, Instagram, and TikTok.'}
                  </p>
                </div>

                {metadataError ? (
                  <div className="rounded-md bg-amber-500/10 p-3 text-sm text-amber-600">
                    {metadataError}
                  </div>
                ) : (
                  <>
                    <SelectionList
                      options={metadataOptions}
                      selected={useCustomMetadataTitle ? '' : effectiveMetadataTitle}
                      onSelect={value => {setSelectedMetadataTitle(value); setUseCustomMetadataTitle(false);}}
                    />
                    {allowCustomMetadataTitle && <label className={`mt-2 flex items-center gap-3 rounded-md border p-3 ${useCustomMetadataTitle ? 'border-[hsl(var(--primary))] ring-2 ring-[hsl(var(--primary))]' : 'border-[hsl(var(--border))]'}`}>
                      <span className="font-mono text-xs text-[hsl(var(--muted-foreground))]">9.</span>
                      <input aria-label="Titre personnalisé" placeholder="Saisir mon propre titre" maxLength={62}
                        className="min-w-0 flex-1 bg-transparent text-sm outline-none" value={customMetadataTitle} disabled={isSaving}
                        onFocus={() => setUseCustomMetadataTitle(true)}
                        onChange={event => {setUseCustomMetadataTitle(true); setCustomMetadataTitle(event.target.value);}}/>
                    </label>}
                  </>
                )}
              </section>
            )}

            {hasOverlayPane && (
              <section className="rounded-lg border border-[hsl(var(--border))] p-4">
                <div className="mb-4">
                  <h3 className="font-medium">Overlay Title</h3>
                  <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
                    Choose the hook displayed inside the generated video overlay.
                  </p>
                  {overlay?.category ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.18em] text-[hsl(var(--muted-foreground))]">
                      Category: {overlay.category}
                    </p>
                  ) : null}
                </div>

                {overlayError ? (
                  <div className="rounded-md bg-amber-500/10 p-3 text-sm text-amber-600">
                    {overlayError}
                  </div>
                ) : (
                  <SelectionList
                    options={overlayOptions}
                    selected={effectiveOverlayTitle}
                    onSelect={setSelectedOverlayTitle}
                  />
                )}
              </section>
            )}
          </div>

          {error && <p role="alert" className="mt-3 text-red-400">{error}</p>}
          <div className="mt-4 flex justify-end gap-3">
            {onClose && <Button variant="outline" disabled={isSaving} onClick={onClose}>Fermer</Button>}
            <Button
              onClick={() =>
              onConfirm({
                  metadataTitle:
                    metadataOptions.length > 0 ? effectiveMetadataTitle : undefined,
                  overlayTitle:
                    overlayOptions.length > 0 ? effectiveOverlayTitle : undefined,
                })
              }
              disabled={isSaving || !canConfirmMetadata || !canConfirmOverlay}
            >
              Apply Selection
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
