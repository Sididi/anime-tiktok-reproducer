import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Music, X, Loader2 } from "lucide-react";
import { Button, SearchableSelect } from "@/components/ui";
import { api } from "@/api/client";

interface CopyrightMusicModalProps {
  open: boolean;
  projectId: string;
  musicDisplayName: string;
  noMusicFileId: string;
  availableMusics: { key: string; display_name: string }[];
  onConfirm: (copyrightAudioPath: string | null) => void;
  onCancel: () => void;
}

export function CopyrightMusicModal({
  open,
  projectId,
  musicDisplayName,
  noMusicFileId,
  availableMusics,
  onConfirm,
  onCancel,
}: CopyrightMusicModalProps) {
  const [selectedMusicKey, setSelectedMusicKey] = useState<string | null>(null);
  const [audioPath, setAudioPath] = useState<string | null>(null);
  const [building, setBuilding] = useState(false);
  const [audioVersion, setAudioVersion] = useState(0);

  const [previewingKey, setPreviewingKey] = useState<string | null>(null);
  const previewAudioRef = useRef<HTMLAudioElement | null>(null);

  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  const stopPreviewAudio = useCallback(() => {
    if (previewAudioRef.current) {
      previewAudioRef.current.pause();
      previewAudioRef.current.currentTime = 0;
      previewAudioRef.current.onended = null;
      previewAudioRef.current = null;
    }
    setPreviewingKey(null);
  }, []);

  const playPreview = useCallback(
    (url: string, key: string) => {
      const isToggleStop = previewingKey === key;
      stopPreviewAudio();
      if (isToggleStop) return;

      const audio = new Audio(url);
      audio.volume = 0.1;
      previewAudioRef.current = audio;
      setPreviewingKey(key);
      audio.play().catch(() => {});
      audio.onended = () => {
        setPreviewingKey(null);
        previewAudioRef.current = null;
      };
    },
    [previewingKey, stopPreviewAudio],
  );

  const buildAudio = useCallback(
    async (musicKey: string | null) => {
      setBuilding(true);
      try {
        const result = await api.buildCopyrightAudio(
          projectId,
          musicKey,
          noMusicFileId,
        );
        setAudioPath(result.audio_path);
        setAudioVersion((v) => v + 1);
      } catch (err) {
        console.error("Failed to build copyright audio:", err);
      } finally {
        setBuilding(false);
      }
    },
    [projectId, noMusicFileId],
  );

  // On open, build the "no music" version immediately; on close, stop preview
  useEffect(() => {
    if (open) {
      setSelectedMusicKey(null);
      setAudioPath(null);
      setAudioVersion(0);
      buildAudio(null);
    } else {
      stopPreviewAudio();
    }
  }, [open, buildAudio, stopPreviewAudio]);

  // When music selection changes
  const handleMusicChange = useCallback(
    (key: string | null) => {
      setSelectedMusicKey(key);
      buildAudio(key);
    },
    [buildAudio],
  );

  // Sync audio with video
  useEffect(() => {
    const video = videoRef.current;
    const audio = audioRef.current;
    if (!video || !audio) return;

    const onPlay = () => {
      audio.currentTime = video.currentTime;
      audio.play();
    };
    const onPause = () => audio.pause();
    const onSeeked = () => {
      audio.currentTime = video.currentTime;
    };

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeked", onSeeked);
    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeked", onSeeked);
    };
  }, [audioVersion]);

  // Escape key closes the modal
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [open, onCancel]);

  const musicOptions = availableMusics.map((m) => ({
    key: m.key,
    label: m.display_name,
    previewUrl: api.previewMusicUrl(projectId, m.key),
  }));

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-60 bg-black/70 flex items-center justify-center p-6"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onCancel}
        >
          <motion.div
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-6 shadow-2xl flex flex-col gap-5"
            style={{ maxWidth: "48rem", width: "100%" }}
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <Music className="h-5 w-5 text-[hsl(var(--destructive))]" />
                  <h3 className="text-lg font-semibold">
                    Remplacement de la musique
                  </h3>
                </div>
                <p className="text-sm text-[hsl(var(--muted-foreground))] mt-1">
                  La musique &ldquo;{musicDisplayName}&rdquo; est sous copyright.
                  Choisissez une musique de remplacement ou continuez sans musique.
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={onCancel}
                className="shrink-0"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>

            {/* Two-column layout */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              {/* Left: Music selector */}
              <div className="flex flex-col gap-3">
                <label className="text-sm font-medium">
                  Musique de remplacement
                </label>
                <SearchableSelect
                  options={musicOptions}
                  value={selectedMusicKey}
                  onChange={handleMusicChange}
                  placeholder="Choisir une musique..."
                  allowNone
                  noneLabel="Pas de musique"
                  disabled={building}
                  onPreview={playPreview}
                  onPreviewStop={stopPreviewAudio}
                  previewingKey={previewingKey}
                />
                {building && (
                  <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Construction de l&apos;audio...
                  </div>
                )}
              </div>

              {/* Right: Video + audio preview */}
              <div className="flex flex-col gap-3">
                <label className="text-sm font-medium">
                  Apercu
                </label>
                <div className="relative bg-black rounded-lg overflow-hidden aspect-9/16 max-h-[50vh]">
                  <video
                    ref={videoRef}
                    src={api.getCopyrightVideoUrl(projectId)}
                    className="w-full h-full object-contain"
                    controls
                    muted
                    preload="metadata"
                  />
                  {building && (
                    <div className="absolute inset-0 bg-black/50 flex items-center justify-center">
                      <Loader2 className="h-8 w-8 animate-spin text-white" />
                    </div>
                  )}
                </div>
                {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
                <audio
                  ref={audioRef}
                  src={`${api.getCopyrightAudioUrl(projectId)}?v=${audioVersion}`}
                  preload="auto"
                />
              </div>
            </div>

            {/* Footer */}
            <div className="flex justify-end gap-3 pt-1">
              <Button
                variant="ghost"
                onClick={onCancel}
                className="active:scale-95 transition-transform"
              >
                Annuler
              </Button>
              <Button
                onClick={() => onConfirm(audioPath)}
                disabled={building}
                className="active:scale-95 transition-transform"
              >
                Confirmer et continuer
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
