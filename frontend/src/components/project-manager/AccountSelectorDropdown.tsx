import { ChevronDown, User } from "lucide-react";
import type { Account } from "@/types";

interface AccountSelectorDropdownProps {
  accounts: Account[];
  selectedAccount: Account | null;
  isOpen: boolean;
  onToggle: () => void;
  onSelect: (accountId: string | null) => void;
}

export function AccountSelectorDropdown({
  accounts,
  selectedAccount,
  isOpen,
  onToggle,
  onSelect,
}: AccountSelectorDropdownProps) {
  return (
    <div className="relative">
      <button
        type="button"
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))] text-sm transition-colors active:scale-95 transition-transform"
        onClick={(e) => {
          e.stopPropagation();
          onToggle();
        }}
      >
        {selectedAccount ? (
          <>
            <img
              src={selectedAccount.avatar_url}
              alt=""
              className="h-6 w-6 rounded-full object-cover bg-[hsl(var(--muted))]"
            />
            <span>{selectedAccount.name}</span>
          </>
        ) : (
          <>
            <User className="h-5 w-5 text-[hsl(var(--muted-foreground))]" />
            <span>All Projects</span>
          </>
        )}
        <ChevronDown
          className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] transition-transform duration-150"
          style={{ transform: isOpen ? "rotate(180deg)" : undefined }}
        />
      </button>
      {isOpen && (
        <div className="absolute top-full left-0 mt-1 z-10 bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg shadow-lg min-w-[200px] py-1">
          <button
            type="button"
            className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--muted))] text-left transition-colors"
            onClick={() => onSelect(null)}
          >
            <User className="h-5 w-5 text-[hsl(var(--muted-foreground))]" />
            <span>All Projects</span>
          </button>
          {accounts.map((acc) => (
            <button
              key={acc.id}
              type="button"
              className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[hsl(var(--muted))] text-left transition-colors"
              onClick={() => onSelect(acc.id)}
            >
              <img
                src={acc.avatar_url}
                alt=""
                className="h-5 w-5 rounded-full object-cover bg-[hsl(var(--muted))]"
              />
              <span className="flex-1">{acc.name}</span>
              <span className="text-xs text-[hsl(var(--muted-foreground))] uppercase">{acc.language}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
