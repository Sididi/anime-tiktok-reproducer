import { useState } from "react";
import { Loader2, RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui";
import { AccountSelectorDropdown } from "@/components/project-manager/AccountSelectorDropdown";
import type { Account, Platform } from "@/types";
import { PlatformCheckboxes } from "./PlatformCheckboxes";

interface PlanningHeaderProps {
  accounts: Account[];
  selectedAccount: Account | null;
  onSelectAccount: (id: string | null) => void;
  selectedPlatforms: Platform[];
  onChangePlatforms: (next: Platform[]) => void;
  loading: boolean;
  onRefresh: () => void;
  onClose: () => void;
}

export function PlanningHeader({
  accounts, selectedAccount, onSelectAccount,
  selectedPlatforms, onChangePlatforms,
  loading, onRefresh, onClose,
}: PlanningHeaderProps) {
  const [accountDropdownOpen, setAccountDropdownOpen] = useState(false);

  return (
    <header className="px-6 py-4 border-b border-[hsl(var(--border))] flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-4 min-w-0">
        <AccountSelectorDropdown
          accounts={accounts}
          selectedAccount={selectedAccount}
          isOpen={accountDropdownOpen}
          onToggle={() => setAccountDropdownOpen((p) => !p)}
          onSelect={(id) => {
            onSelectAccount(id);
            setAccountDropdownOpen(false);
          }}
        />
        <div className="min-w-0">
          <h2 className="text-xl font-semibold">Planning</h2>
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            Forward upload schedule (Europe/Paris)
          </p>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <PlatformCheckboxes
          selected={selectedPlatforms}
          onChange={onChangePlatforms}
        />
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
        <Button variant="ghost" size="sm" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>
    </header>
  );
}
