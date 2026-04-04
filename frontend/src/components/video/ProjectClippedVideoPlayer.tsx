import { forwardRef, useMemo } from "react";
import { getProjectVideoSourceCandidates } from "@/utils/mediaSources";
import {
  ClippedVideoPlayer,
  type ClippedVideoPlayerHandle,
  type ClippedVideoPlayerProps,
} from "./ClippedVideoPlayer";

export interface ProjectClippedVideoPlayerProps
  extends Omit<ClippedVideoPlayerProps, "src" | "fallbackSrc"> {
  projectId: string;
}

export const ProjectClippedVideoPlayer = forwardRef<
  ClippedVideoPlayerHandle,
  ProjectClippedVideoPlayerProps
>(function ProjectClippedVideoPlayer({ projectId, ...props }, ref) {
  const [src, fallbackSrc] = useMemo(
    () => getProjectVideoSourceCandidates(projectId),
    [projectId],
  );

  return (
    <ClippedVideoPlayer
      ref={ref}
      {...props}
      src={src}
      fallbackSrc={fallbackSrc}
    />
  );
});
