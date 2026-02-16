import { useEffect, useCallback, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Loader2, Wand2, ArrowRight, Info } from 'lucide-react';
import { VideoPlayer } from '@/components/video';
import { Timeline } from '@/components/timeline';
import { ScenePanel } from '@/components/scenes';
import { Button } from '@/components/ui';
import { useProjectStore, useVideoStore, useSceneStore } from '@/stores';
import { VideoProvider, useVideo } from '@/contexts';
import { api } from '@/api/client';
import { readSSEStream } from '@/utils/sse';

interface DetectionProgress {
  status: string;
  progress: number;
  message: string;
  scenes?: import('@/types').Scene[];
  error: string | null;
}

// Keyboard shortcuts help tooltip
function ShortcutsHelp() {
  const [isHovered, setIsHovered] = useState(false);

  return (
    <div 
      className="relative"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <button className="p-1 rounded hover:bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]">
        <Info className="h-4 w-4" />
      </button>
      
      {isHovered && (
        <div className="absolute right-0 top-full mt-1 z-50 w-64 p-3 bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg shadow-lg text-sm">
          <h4 className="font-semibold mb-2 text-[hsl(var(--foreground))]">Keyboard Shortcuts</h4>
          <div className="space-y-1 text-[hsl(var(--muted-foreground))]">
            <div className="flex justify-between"><span>Set start</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">A</kbd></div>
            <div className="flex justify-between"><span>Set end</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">Z</kbd></div>
            <div className="flex justify-between"><span>Split scene</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">E</kbd></div>
            <div className="flex justify-between"><span>Merge prev</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">Q</kbd></div>
            <div className="flex justify-between"><span>Merge next</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">D</kbd></div>
            <hr className="my-1.5 border-[hsl(var(--border))]" />
            <div className="flex justify-between"><span>Prev scene</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">↑</kbd></div>
            <div className="flex justify-between"><span>Next scene</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">↓</kbd></div>
            <div className="flex justify-between"><span>Prev frame</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">←</kbd></div>
            <div className="flex justify-between"><span>Next frame</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">→</kbd></div>
            <hr className="my-1.5 border-[hsl(var(--border))]" />
            <div className="flex justify-between"><span>Play/Pause</span><kbd className="px-1.5 py-0.5 bg-[hsl(var(--muted))] rounded text-xs">Space</kbd></div>
            <div className="flex justify-between"><span>Zoom timeline</span><span className="text-xs">Mouse wheel</span></div>
          </div>
        </div>
      )}
    </div>
  );
}

// Wrapper component that provides video context
export function SceneValidation() {
  return (
    <VideoProvider>
      <SceneValidationContent />
    </VideoProvider>
  );
}

function SceneValidationContent() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { project, loadProject } = useProjectStore();
  const { currentTime } = useVideoStore();
  const { scenes, loadScenes, splitScene, mergeScenes, setScenes, saveScenes, getSceneAtTime, getCurrentSceneIndex } =
    useSceneStore();
  const { seekTo, nextFrame, prevFrame, togglePlay } = useVideo();

  const [detecting, setDetecting] = useState(false);
  const [detectionProgress, setDetectionProgress] = useState<DetectionProgress | null>(null);

  const currentScene = getSceneAtTime(currentTime);

  // Load project and scenes on mount
  useEffect(() => {
    if (projectId) {
      loadProject(projectId);
      loadScenes(projectId);
    }
  }, [projectId, loadProject, loadScenes]);

  const handleDetectScenes = useCallback(async () => {
    if (!projectId) return;

    setDetecting(true);
    setDetectionProgress({ status: 'starting', progress: 0, message: 'Starting detection...', error: null });

    try {
      const response = await api.detectScenes(projectId);

      await readSSEStream<DetectionProgress>(response, (data) => {
        setDetectionProgress(data);

        if (data.status === 'complete' && data.scenes) {
          setScenes(data.scenes);
        }
      });
    } catch (err) {
      setDetectionProgress({
        status: 'error',
        progress: 0,
        message: '',
        error: (err as Error).message,
      });
    } finally {
      setDetecting(false);
    }
  }, [projectId, setScenes]);

  const handleSeek = useCallback(
    (time: number) => {
      seekTo(time);
    },
    [seekTo]
  );

  const handleSetStart = useCallback(
    (sceneIndex: number, time: number) => {
      if (!projectId) return;

      const updatedScenes = [...scenes];
      const scene = updatedScenes[sceneIndex];

      if (time >= scene.end_time) return; // Invalid

      if (sceneIndex === 0 && time > 0) {
        // Create new scene before current
        updatedScenes.unshift({
          index: 0,
          start_time: 0,
          end_time: time,
          duration: time,
        });
        scene.start_time = time;
        scene.duration = scene.end_time - time;
      } else if (sceneIndex > 0) {
        // Adjust previous scene's end and current scene's start
        const prevScene = updatedScenes[sceneIndex - 1];
        if (time <= prevScene.start_time) return; // Invalid
        prevScene.end_time = time;
        prevScene.duration = time - prevScene.start_time;
        scene.start_time = time;
        scene.duration = scene.end_time - time;
      } else {
        scene.start_time = time;
        scene.duration = scene.end_time - time;
      }

      // Renumber
      updatedScenes.forEach((s, i) => (s.index = i));
      setScenes(updatedScenes);
      saveScenes(projectId);
    },
    [projectId, scenes, setScenes, saveScenes]
  );

  const handleSetEnd = useCallback(
    (sceneIndex: number, time: number) => {
      if (!projectId) return;

      const updatedScenes = [...scenes];
      const scene = updatedScenes[sceneIndex];

      if (time <= scene.start_time) return; // Invalid

      if (sceneIndex < scenes.length - 1) {
        // Adjust next scene's start
        const nextScene = updatedScenes[sceneIndex + 1];
        if (time >= nextScene.end_time) return; // Invalid
        nextScene.start_time = time;
        nextScene.duration = nextScene.end_time - time;
      }

      scene.end_time = time;
      scene.duration = time - scene.start_time;

      setScenes(updatedScenes);
      saveScenes(projectId);
    },
    [projectId, scenes, setScenes, saveScenes]
  );

  const handleMergePrev = useCallback(
    (sceneIndex: number) => {
      if (!projectId || sceneIndex <= 0) return;
      mergeScenes(projectId, sceneIndex - 1, sceneIndex);
    },
    [projectId, mergeScenes]
  );

  const handleMergeNext = useCallback(
    (sceneIndex: number) => {
      if (!projectId || sceneIndex >= scenes.length - 1) return;
      mergeScenes(projectId, sceneIndex, sceneIndex + 1);
    },
    [projectId, scenes.length, mergeScenes]
  );

  const handleSplit = useCallback(
    (sceneIndex: number, time: number) => {
      if (!projectId) return;
      splitScene(projectId, sceneIndex, time);
    },
    [projectId, splitScene]
  );

  // Navigate to previous scene
  const goToPreviousScene = useCallback(() => {
    const currentIdx = getCurrentSceneIndex(currentTime);
    if (currentIdx > 0) {
      // Add small epsilon to ensure we're clearly in the target scene
      seekTo(scenes[currentIdx - 1].start_time + 0.001);
    }
  }, [getCurrentSceneIndex, currentTime, scenes, seekTo]);

  // Navigate to next scene
  const goToNextScene = useCallback(() => {
    const currentIdx = getCurrentSceneIndex(currentTime);
    if (currentIdx < scenes.length - 1) {
      // Add small epsilon to ensure we're clearly in the target scene
      seekTo(scenes[currentIdx + 1].start_time + 0.001);
    }
  }, [getCurrentSceneIndex, currentTime, scenes, seekTo]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if typing in an input
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) {
        return;
      }

      const currentIdx = getCurrentSceneIndex(currentTime);
      const scene = scenes[currentIdx];
      
      switch (e.key.toLowerCase()) {
        case 'a': // Set start to current time
          if (scene && projectId) {
            handleSetStart(scene.index, currentTime);
          }
          break;
        case 'z': // Set end to current time
          if (scene && projectId) {
            handleSetEnd(scene.index, currentTime);
          }
          break;
        case 'e': // Split scene
          if (scene && projectId && currentTime > scene.start_time && currentTime < scene.end_time) {
            handleSplit(scene.index, currentTime);
          }
          break;
        case 'q': // Merge with previous
          if (scene && scene.index > 0) {
            handleMergePrev(scene.index);
          }
          break;
        case 'd': // Merge with next
          if (scene && scene.index < scenes.length - 1) {
            handleMergeNext(scene.index);
          }
          break;
        case 'arrowup': // Previous scene
          e.preventDefault();
          goToPreviousScene();
          break;
        case 'arrowdown': // Next scene
          e.preventDefault();
          goToNextScene();
          break;
        case 'arrowleft': // Previous frame
          e.preventDefault();
          prevFrame();
          break;
        case 'arrowright': // Next frame
          e.preventDefault();
          nextFrame();
          break;
        case ' ': // Play/Pause
          e.preventDefault();
          togglePlay();
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    projectId, currentTime, scenes, getCurrentSceneIndex,
    handleSetStart, handleSetEnd, handleSplit, handleMergePrev, handleMergeNext,
    goToPreviousScene, goToNextScene, prevFrame, nextFrame, togglePlay
  ]);

  const videoUrl = projectId ? api.getVideoUrl(projectId) : '';

  if (!project) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[hsl(var(--muted-foreground))]">Loading project...</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-4">
      <div className="max-w-6xl mx-auto space-y-4">
        <header className="flex items-center justify-between">
          <h1 className="text-xl font-bold">Scene Validation</h1>
          <div className="flex items-center gap-4">
            <span className="text-sm text-[hsl(var(--muted-foreground))]">
              {scenes.length} scenes
            </span>
            <ShortcutsHelp />
            <Button
              variant="outline"
              size="sm"
              onClick={handleDetectScenes}
              disabled={detecting}
            >
              {detecting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Detecting...
                </>
              ) : (
                <>
                  <Wand2 className="h-4 w-4 mr-2" />
                  Auto-Detect
                </>
              )}
            </Button>
            {scenes.length > 0 && (
              <Button
                size="sm"
                onClick={() => navigate(`/project/${projectId}/matches`)}
              >
                Continue
                <ArrowRight className="h-4 w-4 ml-2" />
              </Button>
            )}
          </div>
        </header>

        {detectionProgress && detectionProgress.status !== 'complete' && (
          <div className="p-3 bg-[hsl(var(--card))] rounded-lg">
            {detectionProgress.error ? (
              <p className="text-sm text-[hsl(var(--destructive))]">{detectionProgress.error}</p>
            ) : (
              <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{detectionProgress.message}</span>
              </div>
            )}
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Video player */}
          <div className="lg:col-span-2">
            <VideoPlayer src={videoUrl} />
            <Timeline onSeek={handleSeek} className="mt-2" />
          </div>

          {/* Scene panel */}
          <div>
            <ScenePanel
              scene={currentScene}
              sceneCount={scenes.length}
              currentTime={currentTime}
              onSetStart={handleSetStart}
              onSetEnd={handleSetEnd}
              onMergePrev={handleMergePrev}
              onMergeNext={handleMergeNext}
              onSplit={handleSplit}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
