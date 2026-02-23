import { motion, AnimatePresence } from "framer-motion";
import { Button } from "@/components/ui";
import type { Account } from "@/types";

interface AccountPickerPopupProps {
  open: boolean;
  accounts: Account[];
  onPick: (accountId: string) => void;
  onClose: () => void;
}

export function AccountPickerPopup({ open, accounts, onPick, onClose }: AccountPickerPopupProps) {
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
              {accounts.map((acc) => (
                <button
                  key={acc.id}
                  type="button"
                  className="w-full flex items-center gap-3 px-3 py-2 rounded-md hover:bg-[hsl(var(--muted))] text-left transition-colors"
                  onClick={() => onPick(acc.id)}
                >
                  <img
                    src={acc.avatar_url}
                    alt=""
                    className="h-7 w-7 rounded-full object-cover bg-[hsl(var(--muted))]"
                  />
                  <span className="flex-1 text-sm">{acc.name}</span>
                  <span className="text-xs text-[hsl(var(--muted-foreground))] uppercase">{acc.language}</span>
                </button>
              ))}
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
