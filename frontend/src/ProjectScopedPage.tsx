import { useId, type ReactNode } from "react";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "./ui";
import { useProjectList } from "./useProjectList";
import { hrefFor, navigate, type Page } from "./routes";

interface Props {
  page: Extract<Page, "documents" | "requirements" | "architecture">;
  title: string;
  subtitle: string;
  projectId?: string;
  children: (projectId: string) => ReactNode;
}

/** Documents and requirements always belong to a project, so these pages start with a picker. */
export default function ProjectScopedPage({
  page,
  title,
  subtitle,
  projectId,
  children,
}: Props) {
  const { projects, error, reload } = useProjectList();
  const selectId = useId();
  const selected = projects?.find((p) => p.id === projectId) ?? null;

  return (
    <section aria-label={title}>
      <PageHeader title={title} subtitle={subtitle} />
      {error && <ErrorState message={error} onRetry={() => void reload()} />}
      {!error && projects === null && (
        <LoadingState label="Loading projects…" />
      )}
      {projects && projects.length === 0 && (
        <EmptyState>
          <p>Create a project first.</p>
          <a href={hrefFor("projects", "new")}>Create a project</a>
        </EmptyState>
      )}
      {projects && projects.length > 0 && (
        <>
          <div className="card field" style={{ maxWidth: "28rem" }}>
            <label htmlFor={selectId}>Project</label>
            <select
              id={selectId}
              value={selected?.id ?? ""}
              onChange={(e) => navigate(page, e.target.value || undefined)}
            >
              <option value="">Select a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          {projectId && !selected && (
            <ErrorState message="That project no longer exists." />
          )}
          {!projectId && <EmptyState>Choose a project to continue.</EmptyState>}
          {selected && (
            <div style={{ marginTop: "var(--space-4)" }}>
              {children(selected.id)}
            </div>
          )}
        </>
      )}
    </section>
  );
}
