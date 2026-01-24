import { useCallback, useRef, useState, useEffect } from 'react';
import type { Scene } from '@/types';
import { useVideoStore, useSceneStore } from '@/stores';
import { SceneBlock } from './SceneBlock';
import { PlayheadCursor } from './PlayheadCursor';
import { TimeRuler } from './TimeRuler';
import { cn } from '@/utils';

interface TimelineProps {
  onSeek: (time: number) => void;
  className?: string;
}

// Zoom configuration
const MIN_ZOOM = 1;
const MAX_ZOOM = 10;
const ZOOM_SENSITIVITY = 0.001;

export function Timeline({ onSeek, className }: TimelineProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { currentTime, duration } = useVideoStore();
  const { scenes, getCurrentSceneIndex } = useSceneStore();

  const currentSceneIndex = getCurrentSceneIndex(currentTime);
  
  // Drag state for cursor
  const [isDragging, setIsDragging] = useState(false);
  
  // Zoom state
  const [zoom, setZoom] = useState(1);
  const [scrollOffset, setScrollOffset] = useState(0); // 0 to 1, represents left edge position

  // Calculate visible time range based on zoom
  const visibleDuration = duration / zoom;
  const visibleStart = scrollOffset * duration;
  const visibleEnd = visibleStart + visibleDuration;

  // Convert screen X position to time
  const screenXToTime = useCallback((clientX: number) => {
    if (!containerRef.current || duration <= 0) return 0;
    const rect = containerRef.current.getBoundingClientRect();
    const x = clientX - rect.left;
    const relativePosition = x / rect.width;
    const time = visibleStart + relativePosition * visibleDuration;
    return Math.max(0, Math.min(time, duration));
  }, [duration, visibleStart, visibleDuration]);

  // Convert time to screen percentage within visible range
  const timeToPercent = useCallback((time: number) => {
    return ((time - visibleStart) / visibleDuration) * 100;
  }, [visibleStart, visibleDuration]);

  // Handle cursor dragging
  useEffect(() => {
    if (!isDragging) return;

    const handleMouseMove = (e: MouseEvent) => {
      const time = screenXToTime(e.clientX);
      onSeek(time);
    };

    const handleMouseUp = () => {
      setIsDragging(false);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isDragging, screenXToTime, onSeek]);

  // Handle left click: teleport cursor to position
  const handleClick = useCallback(
    (e: React.MouseEvent) => {
      if (isDragging) return;
      const time = screenXToTime(e.clientX);
      onSeek(time);
    },
    [screenXToTime, onSeek, isDragging]
  );

  // Handle right click: teleport to start of clicked scene
  const handleContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const clickTime = screenXToTime(e.clientX);
      
      // Find which scene was clicked
      const clickedScene = scenes.find(
        (s) => clickTime >= s.start_time && clickTime < s.end_time
      );
      
      if (clickedScene) {
        // Add small epsilon to ensure we're clearly in the target scene
        onSeek(clickedScene.start_time + 0.001);
      }
    },
    [screenXToTime, scenes, onSeek]
  );

  // Handle mouse down for dragging
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button === 0) { // Left click
        setIsDragging(true);
        const time = screenXToTime(e.clientX);
        onSeek(time);
      }
    },
    [screenXToTime, onSeek]
  );

  // Handle wheel for zoom (centered on mouse position)
  const handleWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
      
      if (!containerRef.current || duration <= 0) return;

      const rect = containerRef.current.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseRelative = mouseX / rect.width; // 0 to 1
      
      // Time at mouse position before zoom
      const timeAtMouse = visibleStart + mouseRelative * visibleDuration;
      
      // Calculate new zoom
      const zoomDelta = -e.deltaY * ZOOM_SENSITIVITY;
      const newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom * (1 + zoomDelta)));
      
      // Calculate new visible duration
      const newVisibleDuration = duration / newZoom;
      
      // Calculate new scroll offset to keep timeAtMouse under the cursor
      const newVisibleStart = timeAtMouse - mouseRelative * newVisibleDuration;
      const newScrollOffset = Math.max(0, Math.min(newVisibleStart / duration, 1 - newVisibleDuration / duration));
      
      setZoom(newZoom);
      setScrollOffset(newScrollOffset);
    },
    [duration, zoom, visibleStart, visibleDuration]
  );

  // Get scene style within visible range
  const getSceneStyle = useCallback((scene: Scene) => {
    const left = timeToPercent(scene.start_time);
    const right = timeToPercent(scene.end_time);
    
    // Clip to visible range
    const clippedLeft = Math.max(0, left);
    const clippedWidth = Math.min(100, right) - clippedLeft;
    
    if (clippedWidth <= 0) return null; // Not visible
    
    return { left: `${clippedLeft}%`, width: `${clippedWidth}%` };
  }, [timeToPercent]);

  // Check if scene is visible
  const isSceneVisible = useCallback((scene: Scene) => {
    return scene.end_time > visibleStart && scene.start_time < visibleEnd;
  }, [visibleStart, visibleEnd]);

  return (
    <div className={cn('flex flex-col', className)}>
      <TimeRuler 
        duration={duration} 
        visibleStart={visibleStart} 
        visibleEnd={visibleEnd}
      />
      <div
        ref={containerRef}
        className={cn(
          "relative h-14 bg-[hsl(var(--card))] rounded-b-lg overflow-hidden select-none",
          isDragging ? "cursor-grabbing" : "cursor-crosshair"
        )}
        onClick={handleClick}
        onContextMenu={handleContextMenu}
        onMouseDown={handleMouseDown}
        onWheel={handleWheel}
      >
        {scenes.filter(isSceneVisible).map((scene) => {
          const style = getSceneStyle(scene);
          if (!style) return null;
          
          return (
            <div
              key={scene.index}
              className="absolute top-0 bottom-0"
              style={style}
            >
              <SceneBlock
                scene={scene}
                isActive={scene.index === currentSceneIndex}
              />
            </div>
          );
        })}
        <PlayheadCursor 
          currentTime={currentTime} 
          duration={duration}
          visibleStart={visibleStart}
          visibleDuration={visibleDuration}
          isDragging={isDragging}
        />
      </div>
      
      {/* Zoom indicator */}
      {zoom > 1 && (
        <div className="flex items-center justify-between mt-1 text-xs text-[hsl(var(--muted-foreground))]">
          <span>Zoom: {zoom.toFixed(1)}x</span>
          <button 
            className="hover:text-[hsl(var(--foreground))] underline"
            onClick={() => { setZoom(1); setScrollOffset(0); }}
          >
            Reset
          </button>
        </div>
      )}
    </div>
  );
}
