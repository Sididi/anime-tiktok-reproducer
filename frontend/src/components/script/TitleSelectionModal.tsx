import { useMemo, useState } from "react";

import { Button } from "@/components/ui";
import type {
  MetadataTitleCandidatesPayload,
  VideoOverlay,
} from "@/types";

interface TitleSelectionModalProps {
  isOpen: boolean;
  metadataCandidates?: MetadataTitleCandidatesPayload | null;
  metadataError?: string | null;
  overlay?: VideoOverlay | null;
  overlayError?: string | null;
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
                ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))/0.12]"
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
  metadataCandidates,
  metadataError,
  overlay,
  overlayError,
  onConfirm,
}: TitleSelectionModalProps) {
  const metadataOptions = metadataCandidates?.title_candidates ?? [];
  const overlayOptions = useMemo(() => {
    return Array.isArray(overlay?.title_hooks)
      ? overlay.title_hooks.filter(
          (hook): hook is string => typeof hook === "string" && hook.trim().length > 0,
        )
      : [];
  }, [overlay]);

  const [selectedMetadataTitle, setSelectedMetadataTitle] = useState("");
  const [selectedOverlayTitle, setSelectedOverlayTitle] = useState("");

  if (!isOpen) return null;

  const hasMetadataPane = metadataOptions.length > 0 || Boolean(metadataError);
  const hasOverlayPane = overlayOptions.length > 0 || Boolean(overlayError);
  const twoPaneLayout = hasMetadataPane && hasOverlayPane;
  const effectiveMetadataTitle =
    metadataOptions.includes(selectedMetadataTitle)
      ? selectedMetadataTitle
      : metadataOptions[0] || "";
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
      <div
        className={`w-full rounded-lg bg-[hsl(var(--card))] p-5 shadow-xl ${
          twoPaneLayout ? "max-w-6xl" : "max-w-3xl"
        }`}
      >
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
                  This title will be reused across YouTube, Facebook, Instagram, and TikTok.
                </p>
              </div>

              {metadataError ? (
                <div className="rounded-md bg-amber-500/10 p-3 text-sm text-amber-600">
                  {metadataError}
                </div>
              ) : (
                <SelectionList
                  options={metadataOptions}
                  selected={effectiveMetadataTitle}
                  onSelect={setSelectedMetadataTitle}
                />
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

        <div className="mt-4 flex justify-end">
          <Button
            onClick={() =>
            onConfirm({
                metadataTitle:
                  metadataOptions.length > 0 ? effectiveMetadataTitle : undefined,
                overlayTitle:
                  overlayOptions.length > 0 ? effectiveOverlayTitle : undefined,
              })
            }
            disabled={!canConfirmMetadata || !canConfirmOverlay}
          >
            Apply Selection
          </Button>
        </div>
      </div>
    </div>
  );
}
