import { useVideo } from '@/contexts';
import { VideoControls } from './VideoControls';
import { cn } from '@/utils';

interface VideoPlayerProps {
  src: string;
  className?: string;
}

export function VideoPlayer({ src, className }: VideoPlayerProps) {
  const { videoRef, play, pause, nextFrame, prevFrame, seekTo, handlers } = useVideo();

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div className="relative bg-black rounded-lg overflow-hidden aspect-[9/16] max-h-[60vh]">
        <video
          ref={videoRef}
          src={src}
          className="w-full h-full object-contain"
          onTimeUpdate={handlers.onTimeUpdate}
          onLoadedMetadata={handlers.onLoadedMetadata}
          onPlay={handlers.onPlay}
          onPause={handlers.onPause}
          playsInline
        />
      </div>
      <VideoControls
        onPlay={play}
        onPause={pause}
        onNextFrame={nextFrame}
        onPrevFrame={prevFrame}
        onSeek={seekTo}
      />
    </div>
  );
}
