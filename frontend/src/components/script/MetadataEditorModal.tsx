import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { Button, Input } from "@/components/ui";
import type { PlatformMetadata } from "@/types";

interface MetadataEditorModalProps {
  isOpen: boolean;
  onClose: () => void;
  metadata: PlatformMetadata;
  facecam?: boolean;
  onSave: (metadata: PlatformMetadata) => void | Promise<void>;
}

function parseTags(raw: string): string[] {
  return raw
    .split(/\r?\n|,/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

export function MetadataEditorModal(props: MetadataEditorModalProps) {
  return props.isOpen ? <MetadataEditorContent {...props}/> : null;
}

function MetadataEditorContent({
  onClose,
  metadata,
  onSave,
  facecam = false,
}: MetadataEditorModalProps) {
  const [draft, setDraft] = useState<PlatformMetadata>(metadata);
  const [facebookTagsInput, setFacebookTagsInput] = useState(metadata.facebook.tags.join("\n"));
  const [youtubeTagsInput, setYoutubeTagsInput] = useState(metadata.youtube.tags.join("\n"));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const textareaRefs = useRef<Record<string, HTMLTextAreaElement | null>>({});

  const autoResizeTextarea = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  };

  const autoResizeAllTextareas = useCallback(() => {
    Object.values(textareaRefs.current).forEach((el) => autoResizeTextarea(el));
  }, []);

  useEffect(() => {
    const frame = requestAnimationFrame(() => autoResizeAllTextareas());
    return () => cancelAnimationFrame(frame);
  }, [draft, facebookTagsInput, youtubeTagsInput, autoResizeAllTextareas]);

  const handleSave = useCallback(async () => {
    const updated: PlatformMetadata = {
      facebook: {
        ...draft.facebook,
        tags: parseTags(facebookTagsInput),
      },
      instagram: { ...draft.instagram },
      youtube: {
        ...draft.youtube,
        tags: parseTags(youtubeTagsInput),
      },
      tiktok: { ...draft.tiktok },
    };

    setSaving(true); setError("");
    try {
      await onSave(updated);
      onClose();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Enregistrement impossible.");
    } finally { setSaving(false); }
  }, [draft, facebookTagsInput, youtubeTagsInput, onSave, onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div role="dialog" aria-modal="true" aria-label="Edit metadata" className="bg-[hsl(var(--card))] rounded-lg w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col">
        <div className="shrink-0 flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <h2 className="text-lg font-semibold">Edit metadata</h2>
          <button
            onClick={onClose}
            disabled={saving}
            aria-label="Fermer les métadonnées"
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain" data-testid="metadata-scroll">
        <fieldset disabled={saving} className="min-w-0 p-4 space-y-4">
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            {facecam ? 'Modifiez le titre et la description communs ci-dessous. Les hashtags fixes sont ajoutés automatiquement. Le suffixe TikTok configuré sera ajouté lors de l’envoi.' : 'Edit platform metadata content with a structured form. Tags can be entered one per line (or comma separated).'}
          </p>

          {facecam && <section className="space-y-3 rounded-lg border border-[hsl(var(--border))] p-4">
            <label className="block space-y-1 text-sm">Titre commun
              <Input aria-label="Titre commun" maxLength={62} value={draft.youtube.title} onChange={e => setDraft(prev => ({
                ...prev, youtube: {...prev.youtube, title:e.target.value}, facebook: {...prev.facebook, title:e.target.value},
              }))}/>
            </label>
            <label className="block space-y-1 text-sm">Description commune sans CTA
              <textarea aria-label="Description commune" value={draft.youtube.description}
                ref={node => {textareaRefs.current.shared_description = node;}}
                className="w-full min-h-[96px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm resize-none"
                onChange={e => setDraft(prev => ({...prev,
                  youtube: {...prev.youtube, description:e.target.value},
                  facebook: {...prev.facebook, description:`${e.target.value.trim()}\n\n#anime #animerecommendations #manga #animefr #otaku`},
                  instagram: {caption:`${e.target.value.trim()}\n\n#anime #animefr #animerecommendations`},
                }))}/>
            </label>
          </section>}

          <div className="grid gap-4 md:grid-cols-2">
            <section className="rounded-lg border border-[hsl(var(--border))] p-4 space-y-3">
              <h3 className="font-medium">Facebook</h3>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Title
                </label>
                <Input
                  readOnly={facecam}
                  value={draft.facebook.title}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      facebook: { ...prev.facebook, title: e.target.value },
                    }))
                  }
                  placeholder="Facebook title"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Description
                </label>
                <textarea
                  readOnly={facecam}
                  ref={node => {textareaRefs.current["facebook_description"] = node;}}
                  value={draft.facebook.description}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      facebook: {
                        ...prev.facebook,
                        description: e.target.value,
                      },
                    }))
                  }
                  onInput={(e) =>
                    autoResizeTextarea(e.currentTarget as HTMLTextAreaElement)
                  }
                  placeholder="Facebook description"
                  className="w-full min-h-[96px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm overflow-hidden resize-none"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Tags
                </label>
                <textarea
                  readOnly={facecam}
                  ref={node => {textareaRefs.current["facebook_tags"] = node;}}
                  value={facebookTagsInput}
                  onChange={(e) => setFacebookTagsInput(e.target.value)}
                  onInput={(e) =>
                    autoResizeTextarea(e.currentTarget as HTMLTextAreaElement)
                  }
                  placeholder="#anime\n#storytime"
                  className="w-full min-h-[96px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm font-mono overflow-hidden resize-none"
                />
              </div>
            </section>

            <section className="rounded-lg border border-[hsl(var(--border))] p-4 space-y-3">
              <h3 className="font-medium">Instagram</h3>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Caption
                </label>
                <textarea
                  readOnly={facecam}
                  ref={node => {textareaRefs.current["instagram_caption"] = node;}}
                  value={draft.instagram.caption}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      instagram: {
                        ...prev.instagram,
                        caption: e.target.value,
                      },
                    }))
                  }
                  onInput={(e) =>
                    autoResizeTextarea(e.currentTarget as HTMLTextAreaElement)
                  }
                  placeholder="Instagram caption"
                  className="w-full min-h-[216px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm overflow-hidden resize-none"
                />
              </div>
            </section>

            <section className="rounded-lg border border-[hsl(var(--border))] p-4 space-y-3">
              <h3 className="font-medium">YouTube</h3>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Title
                </label>
                <Input
                  readOnly={facecam}
                  value={draft.youtube.title}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      youtube: { ...prev.youtube, title: e.target.value },
                    }))
                  }
                  placeholder="YouTube title"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Description
                </label>
                <textarea
                  readOnly={facecam}
                  ref={node => {textareaRefs.current["youtube_description"] = node;}}
                  value={draft.youtube.description}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      youtube: {
                        ...prev.youtube,
                        description: e.target.value,
                      },
                    }))
                  }
                  onInput={(e) =>
                    autoResizeTextarea(e.currentTarget as HTMLTextAreaElement)
                  }
                  placeholder="YouTube description"
                  className="w-full min-h-[96px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm overflow-hidden resize-none"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Tags
                </label>
                <textarea
                  ref={node => {textareaRefs.current["youtube_tags"] = node;}}
                  value={youtubeTagsInput}
                  onChange={(e) => setYoutubeTagsInput(e.target.value)}
                  onInput={(e) =>
                    autoResizeTextarea(e.currentTarget as HTMLTextAreaElement)
                  }
                  placeholder="anime, manga, shorts"
                  className="w-full min-h-[96px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm font-mono overflow-hidden resize-none"
                />
              </div>
            </section>

            <section className="rounded-lg border border-[hsl(var(--border))] p-4 space-y-3">
              <h3 className="font-medium">TikTok</h3>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Description
                </label>
                <textarea
                  readOnly={facecam}
                  ref={node => {textareaRefs.current["tiktok_description"] = node;}}
                  value={draft.tiktok.description}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      tiktok: {
                        ...prev.tiktok,
                        description: e.target.value,
                      },
                    }))
                  }
                  onInput={(e) =>
                    autoResizeTextarea(e.currentTarget as HTMLTextAreaElement)
                  }
                  placeholder="TikTok description"
                  className="w-full min-h-[216px] p-2 rounded-md border border-[hsl(var(--input))] bg-transparent text-sm overflow-hidden resize-none"
                />
              </div>
            </section>
          </div>
        </fieldset>
        </div>

        <div className="shrink-0 flex items-center justify-end gap-2 p-4 border-t border-[hsl(var(--border))]">
          {error && <p role="alert" className="mr-auto text-sm text-red-400">{error}</p>}
          <Button variant="outline" disabled={saving} onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={saving} onClick={() => void handleSave()}>{saving ? 'Enregistrement…' : 'Save Changes'}</Button>
        </div>
      </div>
    </div>
  );
}
