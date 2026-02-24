import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { Button, Input } from "@/components/ui";
import type { PlatformMetadata } from "@/types";

interface MetadataEditorModalProps {
  isOpen: boolean;
  onClose: () => void;
  metadata: PlatformMetadata;
  onSave: (metadata: PlatformMetadata) => void;
}

function parseTags(raw: string): string[] {
  return raw
    .split(/\r?\n|,/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

export function MetadataEditorModal({
  isOpen,
  onClose,
  metadata,
  onSave,
}: MetadataEditorModalProps) {
  const [draft, setDraft] = useState<PlatformMetadata>(metadata);
  const [facebookTagsInput, setFacebookTagsInput] = useState("");
  const [youtubeTagsInput, setYoutubeTagsInput] = useState("");
  const textareaRefs = useRef<Record<string, HTMLTextAreaElement | null>>({});

  const registerTextarea =
    (key: string) => (node: HTMLTextAreaElement | null) => {
      textareaRefs.current[key] = node;
    };

  const autoResizeTextarea = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  };

  const autoResizeAllTextareas = useCallback(() => {
    Object.values(textareaRefs.current).forEach((el) => autoResizeTextarea(el));
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    setDraft(metadata);
    setFacebookTagsInput(metadata.facebook.tags.join("\n"));
    setYoutubeTagsInput(metadata.youtube.tags.join("\n"));
  }, [isOpen, metadata]);

  useEffect(() => {
    if (!isOpen) return;
    requestAnimationFrame(() => autoResizeAllTextareas());
  }, [isOpen, draft, facebookTagsInput, youtubeTagsInput, autoResizeAllTextareas]);

  const handleSave = useCallback(() => {
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

    onSave(updated);
    onClose();
  }, [draft, facebookTagsInput, youtubeTagsInput, onSave, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-[hsl(var(--card))] rounded-lg w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-[hsl(var(--border))]">
          <h2 className="text-lg font-semibold">Edit Script</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[hsl(var(--muted))] rounded"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            Edit platform metadata content with a structured form. Tags can be
            entered one per line (or comma separated).
          </p>

          <div className="grid gap-4 md:grid-cols-2">
            <section className="rounded-lg border border-[hsl(var(--border))] p-4 space-y-3">
              <h3 className="font-medium">Facebook</h3>
              <div className="space-y-1">
                <label className="text-xs text-[hsl(var(--muted-foreground))]">
                  Title
                </label>
                <Input
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
                  ref={registerTextarea("facebook_description")}
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
                  ref={registerTextarea("facebook_tags")}
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
                  ref={registerTextarea("instagram_caption")}
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
                  ref={registerTextarea("youtube_description")}
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
                  ref={registerTextarea("youtube_tags")}
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
                  ref={registerTextarea("tiktok_description")}
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
        </div>

        <div className="flex items-center justify-end gap-2 p-4 border-t border-[hsl(var(--border))]">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSave}>Save Changes</Button>
        </div>
      </div>
    </div>
  );
}
