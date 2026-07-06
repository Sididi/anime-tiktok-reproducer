import { motion, AnimatePresence } from "framer-motion";
import { Ban } from "lucide-react";
import { Button } from "@/components/ui";
import type { Account } from "@/types";
import { getSupportedTypeLabels } from "@/utils/libraryTypes";

interface AccountPickerPopupProps {
  open: boolean;
  accounts: Account[];
  /** Accounts blocked because they already uploaded a linked duplicated project. */
  blockedAccountIds?: Set<string>;
  onPick: (accountId: string) => void;
  onClose: () => void;
}

export function AccountPickerPopup({
  open,
  accounts,
  blockedAccountIds,
  onPick,
  onClose,
}: AccountPickerPopupProps) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg p-4 min-w-[280px] max-w-sm shadow-xl"
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="font-semibold mb-3">Select Account</h3>
            <div className="space-y-1">
              {accounts.map((acc) => {
                const blocked = !!blockedAccountIds?.has(acc.id);
                return (
                  <button
                    key={acc.id}
                    type="button"
                    disabled={blocked}
                    title={
                      blocked
                        ? "Ce compte a déjà uploadé un projet dupliqué lié — interdit à vie."
                        : undefined
                    }
                    className={`w-full flex items-center gap-3 px-3 py-2 rounded-md text-left transition-colors ${
                      blocked
                        ? "opacity-40 cursor-not-allowed"
                        : "hover:bg-[hsl(var(--muted))]"
                    }`}
                    onClick={() => onPick(acc.id)}
                  >
                    <img
                      src={acc.avatar_url}
                      alt=""
                      className="h-7 w-7 rounded-full object-cover bg-[hsl(var(--muted))]"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm">{acc.name}</div>
                      <div className="truncate text-[11px] text-[hsl(var(--muted-foreground))]">
                        {acc.language.toUpperCase()} •{" "}
                        {getSupportedTypeLabels(acc.supported_types)}
                        {blocked && " • projet dupliqué lié"}
                      </div>
                    </div>
                    {blocked && (
                      <Ban className="h-4 w-4 shrink-0 text-[hsl(var(--destructive))]" />
                    )}
                  </button>
                );
              })}
            </div>
            <div className="mt-3 text-right">
              <Button variant="ghost" size="sm" onClick={onClose}>
                Cancel
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
