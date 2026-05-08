import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Calendar, ChevronDown, Loader2, UploadCloud, Zap } from "lucide-react";
import type { Account, ProjectManagerRow } from "@/types";

interface UploadSplitButtonProps {
  row: ProjectManagerRow;
  selectedAccount: Account | null;
  uploadActive: boolean;
  uploadLabel: string | null;
  disabled: boolean;
  disabledReason?: string;
  onAuto: () => void;
  onSchedule: () => void;
  onUrgent: () => void;
}

export function UploadSplitButton({
  row, selectedAccount, uploadActive, uploadLabel,
  disabled, disabledReason,
  onAuto, onSchedule, onUrgent,
}: UploadSplitButtonProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  void row;

  // When an account is selected, it must have TT slots to manually schedule.
  // When no account is selected ("All Projects"), the row's account picker
  // will be opened first — assume some compatible account has TT.
  const scheduleDisabled = !!selectedAccount && !selectedAccount.slots_by_platform?.tiktok?.length;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div className="relative inline-flex" ref={ref} title={disabledReason}>
      <button
        type="button"
        onClick={onAuto}
        disabled={disabled || uploadActive}
        className="bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-sm px-3 py-1.5 rounded-l-md border-r border-[hsl(var(--primary-foreground))]/20 disabled:opacity-50 inline-flex items-center gap-1.5 active:scale-95 transition-transform"
      >
        {uploadActive ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            {uploadLabel || "Uploading"}
          </>
        ) : (
          <>
            <UploadCloud className="h-4 w-4" />
            Upload
          </>
        )}
      </button>
      <button
        type="button"
        onClick={() => !disabled && !uploadActive && setOpen((p) => !p)}
        disabled={disabled || uploadActive}
        className="bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-sm px-2 py-1.5 rounded-r-md disabled:opacity-50"
        aria-label="Upload options"
      >
        <ChevronDown className="h-4 w-4" />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -2 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -2 }}
            transition={{ duration: 0.12 }}
            className="absolute right-0 top-full mt-1 w-72 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-xl z-30 p-1.5 text-sm"
          >
            <button
              type="button"
              onClick={() => { setOpen(false); onAuto(); }}
              className="w-full text-left flex items-start gap-2.5 px-2.5 py-2 rounded hover:bg-[hsl(var(--muted))]"
            >
              <UploadCloud className="h-4 w-4 mt-0.5 text-[hsl(var(--primary))]" />
              <div>
                <div>Upload now</div>
                <div className="text-[11px] text-[hsl(var(--muted-foreground))]">Auto: next free slot</div>
              </div>
            </button>
            <button
              type="button"
              disabled={scheduleDisabled}
              onClick={() => { setOpen(false); onSchedule(); }}
              title={scheduleDisabled ? "This account has no TikTok configured" : undefined}
              className="w-full text-left flex items-start gap-2.5 px-2.5 py-2 rounded hover:bg-[hsl(var(--muted))] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Calendar className="h-4 w-4 mt-0.5 text-blue-500" />
              <div>
                <div>Schedule for specific slot…</div>
                <div className="text-[11px] text-[hsl(var(--muted-foreground))]">Pick a TikTok slot manually</div>
              </div>
            </button>
            <div className="border-t border-[hsl(var(--border))] my-1" />
            <button
              type="button"
              onClick={() => { setOpen(false); onUrgent(); }}
              className="w-full text-left flex items-start gap-2.5 px-2.5 py-2 rounded hover:bg-[hsl(var(--destructive))]/10 text-[hsl(var(--destructive))]"
            >
              <Zap className="h-4 w-4 mt-0.5" />
              <div>
                <div>Upload urgently (push others)</div>
                <div className="text-[11px] opacity-80">Take nearest slot · cascades existing</div>
              </div>
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
