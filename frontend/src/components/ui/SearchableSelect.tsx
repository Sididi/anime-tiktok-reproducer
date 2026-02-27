import { useState, useRef, useEffect, useCallback } from "react";
import { Search, Play, Pause, ChevronDown, X } from "lucide-react";

export interface SearchableSelectOption {
  key: string;
  label: string;
  previewUrl?: string;
}

interface SearchableSelectProps {
  options: SearchableSelectOption[];
  value: string | null;
  onChange: (key: string | null) => void;
  placeholder?: string;
  allowNone?: boolean;
  noneLabel?: string;
  disabled?: boolean;
  onPreview?: (url: string, key: string) => void;
  onPreviewStop?: () => void;
  previewingKey?: string | null;
}

export function SearchableSelect({
  options,
  value,
  onChange,
  placeholder = "Select...",
  allowNone = false,
  noneLabel = "None",
  disabled = false,
  onPreview,
  onPreviewStop,
  previewingKey,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selectedOption = options.find((o) => o.key === value);
  const displayLabel = selectedOption?.label ?? (allowNone ? noneLabel : placeholder);

  const filtered = filter.trim()
    ? options.filter((o) =>
        o.label.toLowerCase().includes(filter.toLowerCase()),
      )
    : options;

  const closeDropdown = useCallback(() => {
    setOpen(false);
    setFilter("");
    onPreviewStop?.();
  }, [onPreviewStop]);

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        closeDropdown();
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open, closeDropdown]);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  const handleSelect = useCallback(
    (key: string | null) => {
      onChange(key);
      closeDropdown();
    },
    [onChange, closeDropdown],
  );

  const handlePreviewClick = useCallback(
    (e: React.MouseEvent, url: string, key: string) => {
      e.stopPropagation();
      if (previewingKey === key) {
        onPreviewStop?.();
      } else {
        onPreview?.(url, key);
      }
    },
    [previewingKey, onPreview, onPreviewStop],
  );

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          if (open) {
            closeDropdown();
            return;
          }
          setOpen(true);
        }}
        className="w-full flex items-center justify-between gap-2 px-2.5 py-1.5 text-sm rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] hover:bg-[hsl(var(--muted))] disabled:opacity-50 disabled:cursor-not-allowed text-left"
      >
        <span className={`truncate ${!selectedOption && !allowNone ? "text-[hsl(var(--muted-foreground))]" : ""}`}>
          {displayLabel}
        </span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--muted-foreground))]" />
      </button>

      {open && (
        <div className="absolute z-50 top-full left-0 right-0 mt-1 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-lg overflow-hidden">
          {/* Search input */}
          {options.length > 5 && (
            <div className="flex items-center gap-2 px-2.5 py-2 border-b border-[hsl(var(--border))]">
              <Search className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] shrink-0" />
              <input
                ref={inputRef}
                type="text"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Search..."
                className="flex-1 bg-transparent text-sm outline-none placeholder:text-[hsl(var(--muted-foreground))]"
              />
              {filter && (
                <button
                  type="button"
                  onClick={() => setFilter("")}
                  className="shrink-0 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          )}

          {/* Options list */}
          <div className="max-h-48 overflow-y-auto">
            {allowNone && (
              <button
                type="button"
                onClick={() => handleSelect(null)}
                className={`w-full text-left px-2.5 py-1.5 text-sm hover:bg-[hsl(var(--muted))] ${
                  value === null ? "bg-[hsl(var(--muted))] font-medium" : ""
                }`}
              >
                {noneLabel}
              </button>
            )}
            {filtered.map((option) => (
              <div
                key={option.key}
                className={`flex items-center gap-2 px-2.5 py-1.5 hover:bg-[hsl(var(--muted))] cursor-pointer ${
                  value === option.key ? "bg-[hsl(var(--muted))] font-medium" : ""
                }`}
                onClick={() => handleSelect(option.key)}
              >
                <span className="flex-1 text-sm truncate">{option.label}</span>
                {option.previewUrl && onPreview && (
                  <button
                    type="button"
                    onClick={(e) =>
                      handlePreviewClick(e, option.previewUrl!, option.key)
                    }
                    className="shrink-0 p-0.5 rounded hover:bg-[hsl(var(--background))]"
                    title="Preview"
                  >
                    {previewingKey === option.key ? (
                      <Pause className="h-3 w-3" />
                    ) : (
                      <Play className="h-3 w-3" />
                    )}
                  </button>
                )}
              </div>
            ))}
            {filtered.length === 0 && (
              <div className="px-2.5 py-3 text-xs text-center text-[hsl(var(--muted-foreground))]">
                No matches
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
