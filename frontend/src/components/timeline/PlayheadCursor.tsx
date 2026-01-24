interface PlayheadCursorProps {
  currentTime: number;
  duration: number;
  visibleStart?: number;
  visibleDuration?: number;
  isDragging?: boolean;
}

export function PlayheadCursor({ currentTime, duration, visibleStart = 0, visibleDuration, isDragging }: PlayheadCursorProps) {
  const effectiveVisibleDuration = visibleDuration ?? duration;
  const left = effectiveVisibleDuration > 0 
    ? ((currentTime - visibleStart) / effectiveVisibleDuration) * 100 
    : 0;

  // Don't render if outside visible range
  if (left < 0 || left > 100) return null;

  return (
    <div
      className="absolute top-0 bottom-0 z-20 pointer-events-none"
      style={{ left: `${left}%`, transform: 'translateX(-50%)' }}
    >
      {/* Main cursor line - thicker and more visible */}
      <div className={`absolute left-1/2 -translate-x-1/2 top-0 bottom-0 w-1 ${isDragging ? 'bg-yellow-400' : 'bg-red-500'} shadow-lg`} />
      
      {/* Top handle - larger triangle */}
      <div className={`absolute -top-2 left-1/2 -translate-x-1/2 w-0 h-0 border-l-[8px] border-r-[8px] border-t-[10px] border-l-transparent border-r-transparent ${isDragging ? 'border-t-yellow-400' : 'border-t-red-500'}`} />
      
      {/* Bottom handle - small triangle */}
      <div className={`absolute -bottom-1 left-1/2 -translate-x-1/2 w-0 h-0 border-l-[6px] border-r-[6px] border-b-[8px] border-l-transparent border-r-transparent ${isDragging ? 'border-b-yellow-400' : 'border-b-red-500'}`} />
    </div>
  );
}
