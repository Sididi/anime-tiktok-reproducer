interface TooltipProps {
  text: string;
  children: React.ReactNode;
}

export function Tooltip({ text, children }: TooltipProps) {
  return (
    <div className="relative group/tooltip">
      {children}
      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2.5 py-1.5 rounded-md bg-[hsl(var(--popover))] border border-[hsl(var(--border))] text-xs text-[hsl(var(--popover-foreground))] whitespace-nowrap opacity-0 pointer-events-none group-hover/tooltip:opacity-100 transition-opacity duration-150 shadow-lg z-10">
        {text}
      </div>
    </div>
  );
}
