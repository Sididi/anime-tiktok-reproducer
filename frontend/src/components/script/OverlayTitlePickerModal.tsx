import { Button } from "@/components/ui";

interface OverlayTitlePickerModalProps {
  isOpen: boolean;
  category: string;
  titleHooks: string[];
  onSelect: (title: string) => void;
}

export function OverlayTitlePickerModal({
  isOpen,
  category,
  titleHooks,
  onSelect,
}: OverlayTitlePickerModalProps) {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-2xl rounded-lg bg-[hsl(var(--card))] p-5 shadow-xl">
        <div className="mb-4">
          <h2 className="text-lg font-semibold">Select Overlay Title</h2>
          <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
            Click the hook you want to keep.
          </p>
          <p className="mt-2 text-xs uppercase tracking-[0.18em] text-[hsl(var(--muted-foreground))]">
            Category: {category || "N/A"}
          </p>
        </div>

        <div className="space-y-2">
          {titleHooks.map((title, index) => (
            <Button
              key={`${index}-${title}`}
              type="button"
              variant="outline"
              className="h-auto w-full justify-start whitespace-normal px-4 py-3 text-left text-sm"
              onClick={() => onSelect(title)}
            >
              <span className="mr-3 font-mono text-xs text-[hsl(var(--muted-foreground))]">
                {index + 1}.
              </span>
              <span>{title}</span>
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}
