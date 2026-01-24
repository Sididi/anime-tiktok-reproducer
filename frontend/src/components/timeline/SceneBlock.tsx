import type { Scene } from '@/types';
import { cn } from '@/utils';

const SCENE_COLORS = [
  'bg-blue-500',
  'bg-emerald-500',
  'bg-amber-500',
  'bg-rose-500',
  'bg-violet-500',
  'bg-pink-500',
  'bg-cyan-500',
  'bg-orange-500',
];

interface SceneBlockProps {
  scene: Scene;
  isActive: boolean;
}

export function SceneBlock({ 
  scene, 
  isActive, 
}: SceneBlockProps) {
  const colorClass = SCENE_COLORS[scene.index % SCENE_COLORS.length];

  return (
    <div
      className={cn(
        'absolute inset-0 flex items-center justify-center pointer-events-none',
        'border-r border-[hsl(var(--background))] transition-opacity',
        colorClass,
        isActive ? 'opacity-100 ring-2 ring-white ring-inset' : 'opacity-70'
      )}
      data-scene-block={scene.index}
    >
      <span className="text-xs font-bold text-white drop-shadow-md">{scene.index + 1}</span>
    </div>
  );
}
