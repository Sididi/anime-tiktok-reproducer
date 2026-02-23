import { motion, AnimatePresence } from "framer-motion";
import { Button } from "@/components/ui";
import { formatScheduledAt } from "./utils";

interface ScheduledDeleteConfirmProps {
  open: boolean;
  scheduledAt: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ScheduledDeleteConfirm({ open, scheduledAt, onConfirm, onCancel }: ScheduledDeleteConfirmProps) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onCancel}
        >
          <motion.div
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg p-5 max-w-sm shadow-xl"
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="font-semibold mb-2">Delete Scheduled Project?</h3>
            <p className="text-sm text-[hsl(var(--muted-foreground))] mb-4">
              This project has a scheduled upload at <strong>{formatScheduledAt(scheduledAt)}</strong>. Delete anyway?
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={onCancel}>
                Cancel
              </Button>
              <Button variant="destructive" size="sm" onClick={onConfirm}>
                Delete
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
