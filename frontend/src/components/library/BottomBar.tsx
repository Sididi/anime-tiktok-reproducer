import { Loader2 } from "lucide-react";

interface BottomBarProps {
  tiktokUrl: string;
  onUrlChange: (url: string) => void;
  onStart: () => void;
  disabled: boolean;
  loading: boolean;
  statusText?: string;
}

export function BottomBar({
  tiktokUrl,
  onUrlChange,
  onStart,
  disabled,
  loading,
  statusText,
}: BottomBarProps) {
  return (
    <div className="flex items-center gap-3 bg-[hsl(var(--card))] rounded-lg px-4 py-3">
      <input
        type="text"
        value={tiktokUrl}
        onChange={(e) => onUrlChange(e.target.value)}
        placeholder="https://www.tiktok.com/@user/video/..."
        className="flex-1 bg-[hsl(var(--background))] border border-[hsl(var(--border))] rounded-lg px-3 py-2.5 text-sm placeholder:text-[hsl(var(--muted-foreground))] outline-none focus:ring-2 focus:ring-[hsl(var(--ring))]"
      />

      <button
        onClick={onStart}
        disabled={disabled}
        className={`flex items-center gap-2 bg-[hsl(var(--primary))] text-white font-bold rounded-lg px-6 py-2.5 transition-colors shrink-0 ${
          disabled
            ? "opacity-50 cursor-not-allowed"
            : "hover:bg-[hsl(var(--primary))]/90"
        }`}
      >
        {loading ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            {statusText && <span>{statusText}</span>}
          </>
        ) : (
          <span>Démarrer</span>
        )}
      </button>
    </div>
  );
}
