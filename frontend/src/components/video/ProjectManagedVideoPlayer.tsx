import { forwardRef, useMemo } from "react";
import { getProjectVideoSourceCandidates } from "@/utils/mediaSources";
import {
  ManagedVideoPlayer,
  type ManagedVideoPlayerHandle,
  type ManagedVideoPlayerProps,
} from "./ManagedVideoPlayer";

export interface ProjectManagedVideoPlayerProps
  extends Omit<ManagedVideoPlayerProps, "src" | "fallbackSrc"> {
  projectId: string;
}

export const ProjectManagedVideoPlayer = forwardRef<
  ManagedVideoPlayerHandle,
  ProjectManagedVideoPlayerProps
>(function ProjectManagedVideoPlayer({ projectId, ...props }, ref) {
  const [src, fallbackSrc] = useMemo(
    () => getProjectVideoSourceCandidates(projectId),
    [projectId],
  );

  return (
    <ManagedVideoPlayer
      ref={ref}
      {...props}
      src={src}
      fallbackSrc={fallbackSrc}
    />
  );
});
