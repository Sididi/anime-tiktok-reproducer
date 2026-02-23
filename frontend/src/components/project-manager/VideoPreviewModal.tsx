import { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";
import { Button } from "@/components/ui";

interface VideoPreviewModalProps {
  driveVideoId: string | null;
  onClose: () => void;
}

export function VideoPreviewModal({ driveVideoId, onClose }: VideoPreviewModalProps) {
  useEffect(() => {
    if (!driveVideoId) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [driveVideoId, onClose]);

  return (
    <AnimatePresence>
      {driveVideoId && (
        <motion.div
          className="fixed inset-0 z-[60] bg-black/70 flex items-center justify-center p-8"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          onClick={onClose}
        >
          {/* Vertical (9:16) container — max 80vh tall */}
          <motion.div
            className="relative bg-black rounded-xl overflow-hidden shadow-2xl h-[80vh] aspect-[9/16]"
            initial={{ scale: 0.9, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.9, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* X button at top-LEFT to avoid GDrive's native top-right controls */}
            <div className="absolute top-3 left-3 z-10">
              <Button variant="ghost" size="icon" onClick={onClose} className="text-white hover:bg-white/20">
                <X className="h-5 w-5" />
              </Button>
            </div>
            <iframe
              src={`https://drive.google.com/file/d/${driveVideoId}/preview`}
              className="w-full h-full"
              allow="autoplay"
              allowFullScreen
            />
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
