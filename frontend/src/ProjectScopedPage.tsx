import type { ReactNode } from "react";
import type { Project } from "./api";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "./ui";
import { useProjectList } from "./useProjectList";
import { hrefFor, type Page } from "./routes";

interface Props {
  page: Extract<Page, "documents" | "requirements" | "architecture" | "plan">;
  title: string;
  subtitle: string;
  /** The active project, from the URL or the last opened one. Chosen on the Projects page. */
  projectId?: string;
  /** The page shows its own project header (name, stepper) once a project is selected. */
  projectHeader?: boolean;
  children: (projectId: string, project: Project) => ReactNode;
}

/** Documents, requirements and architecture belong to a project opened from the Projects page. */
export default function ProjectScopedPage({
  page,
  title,
  subtitle,
  projectId,
  projectHeader = false,
  children,
}: Props) {
  const { projects, error, reload } = useProjectList();
  const selected = projects?.find((p) => p.id === projectId) ?? null;

  return (
    <section aria-label={title} data-page={page}>
      {!(projectHeader && selected) && (
        <PageHeader title={title} subtitle={subtitle} />
      )}
      {!projectId && (
        <EmptyState>
          <p>No project is open.</p>
          <a href={hrefFor("projects")}>Go to Projects to open one</a>
        </EmptyState>
      )}
      {projectId && error && <ErrorState message={error} onRetry={() => void reload()} />}
      {projectId && !error && projects === null && (
        <LoadingState label="Loading projects…" />
      )}
      {projectId && projects && !selected && (
        <>
          <ErrorState message="That project no longer exists." />
          <p>
            <a href={hrefFor("projects")}>Go to Projects</a>
          </p>
        </>
      )}
      {selected && (
        <div style={{ marginTop: "var(--space-4)" }}>{children(selected.id, selected)}</div>
      )}
    </section>
  );
}
