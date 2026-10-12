import { useEffect, useState } from "react";
import { Link, Outlet, useParams } from "react-router-dom";
import { api, ProjectArchivedError } from "@/api/client";

/** Don't mount work pages (or their media/background requests) for archives. */
export function ProjectAccessGate() {
  const { projectId } = useParams<{ projectId: string }>();
  const [access, setAccess] = useState<{
    projectId: string;
    error?: string;
    archived?: boolean;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!projectId) return;
    const checkAccess = () => {
      void api.getProject(projectId).then(
        () => { if (!cancelled) setAccess({ projectId }); },
        (error: Error) => {
          if (!cancelled) setAccess({
            projectId,
            error: error.message,
            archived: error instanceof ProjectArchivedError,
          });
        },
      );
    };
    checkAccess();
    // Project Manager may delete this project in another tab.
    window.addEventListener("focus", checkAccess);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", checkAccess);
    };
  }, [projectId]);

  useEffect(() => {
    const onArchived = (event: Event) => {
      if ((event as CustomEvent<{ projectId: string }>).detail.projectId === projectId && projectId) {
        setAccess({ projectId, archived: true, error: "This project is archive-only and is disabled." });
      }
    };
    window.addEventListener("project-archived", onArchived);
    return () => window.removeEventListener("project-archived", onArchived);
  }, [projectId]);

  if (!access || access.projectId !== projectId) {
    return <div className="p-8 text-center">Loading project…</div>;
  }
  if (access.error) {
    return (
      <main className="mx-auto max-w-xl p-8 text-center space-y-4">
        <h1 className="text-2xl font-semibold">
          {access.archived ? "Archive-only project" : "Project unavailable"}
        </h1>
        <p className="text-[hsl(var(--muted-foreground))]">
          {access.archived
            ? "This project is disabled. Its assets and work files have been deleted. Read-only project, schedule, and upload information remains available in Project Manager."
            : access.error}
        </p>
        <Link className="inline-block text-[hsl(var(--primary))] hover:underline" to="/">
          Back to projects
        </Link>
      </main>
    );
  }
  return <Outlet />;
}
