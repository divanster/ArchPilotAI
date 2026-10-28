import { useEffect, useState } from "react";
import ProjectDetails, { type ProjectSection } from "./ProjectDetails";
import ProjectForm from "./ProjectForm";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  PageHeader,
  StatusBadge,
} from "./ui";
import { useProjectList } from "./useProjectList";
import { createProject, listDocuments, listProposals, listRequirements } from "./api";
import { getLastSection, setActiveProject } from "./activeProject";
import { navigate } from "./routes";

interface Stats {
  documents: number | null;
  requirements: number | null;
  architecture: string | null;
}

/** Counts shown on a project card; any lookup that fails shows "—" instead of a guess. */
function ProjectStats({ projectId }: { projectId: string }) {
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    let active = true;
    const count = (p: Promise<unknown>) =>
      p.then((r) => (Array.isArray(r) ? r.length : null)).catch(() => null);
    Promise.all([
      count(listDocuments(projectId)),
      count(listRequirements(projectId)),
      listProposals(projectId)
        .then((list) => {
          if (!Array.isArray(list)) return null;
          if (list.length === 0) return "none";
          const latest = [...list].sort((a, b) => b.version - a.version)[0];
          return `V${latest.version} ${latest.status}`;
        })
        .catch(() => null),
    ]).then(([documents, requirements, architecture]) => {
      if (active) setStats({ documents, requirements, architecture });
    });
    return () => {
      active = false;
    };
  }, [projectId]);
  const n = (v: number | null | undefined) => (v == null ? "—" : String(v));
  return (
    <dl className="card-stats" aria-label="Project summary">
      <div>
        <dt>Documents</dt>
        <dd>{n(stats?.documents)}</dd>
      </div>
      <div>
        <dt>Requirements</dt>
        <dd>{n(stats?.requirements)}</dd>
      </div>
      <div>
        <dt>Architecture</dt>
        <dd>{stats?.architecture ?? "—"}</dd>
      </div>
    </dl>
  );
}

interface Props {
  initialSelectedId?: string | null;
  initialCreate?: boolean;
  onOpenSection?: (section: ProjectSection, projectId: string) => void;
}

export default function ProjectsPage({
  initialSelectedId = null,
  initialCreate = false,
  onOpenSection,
}: Props) {
  const { projects, setProjects, error, reload } = useProjectList();
  const [selectedId, setSelectedId] = useState<string | null>(
    initialSelectedId,
  );
  const [creating, setCreating] = useState(initialCreate);

  const selected = projects?.find((p) => p.id === selectedId) ?? null;

  return (
    <section aria-label="Projects">
      <PageHeader
        title="Projects"
        subtitle="Each project groups its documents and the requirements extracted from them."
        actions={
          <button
            type="button"
            className="primary"
            onClick={() => setCreating(true)}
          >
            New project
          </button>
        }
      />

      {error && <ErrorState message={error} onRetry={() => void reload()} />}
      {!error && projects === null && (
        <LoadingState label="Loading projects…" />
      )}
      {projects && projects.length === 0 && (
        <EmptyState>No projects yet.</EmptyState>
      )}

      {projects && projects.length > 0 && (
        <div className={selected ? "two-col" : undefined}>
          <ul className="grid-cards" aria-label="Project list">
            {projects.map((p) => (
              <li
                key={p.id}
                className="project-card"
                data-selected={p.id === selectedId}
              >
                <div
                  className="row"
                  style={{ justifyContent: "space-between" }}
                >
                  <button
                    type="button"
                    className="card-title"
                    onClick={() => setSelectedId(p.id)}
                  >
                    {p.name}
                  </button>
                  <StatusBadge status={p.status} />
                </div>
                <p className="card-desc">{p.description ?? "No description"}</p>
                <ProjectStats projectId={p.id} />
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="card-meta">
                    Updated {new Date(p.updated_at).toLocaleDateString()}
                  </span>
                  <button
                    type="button"
                    className="primary"
                    aria-label={`Open ${p.name}`}
                    onClick={() => {
                      const section = getLastSection(p.id);
                      setActiveProject(p.id, section);
                      navigate(section, p.id);
                    }}
                  >
                    Open
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {selected && (
            <ProjectDetails
              key={selected.id}
              project={selected}
              onClose={() => setSelectedId(null)}
              onOpenSection={onOpenSection}
              onUpdated={(updated) =>
                setProjects(
                  (current) =>
                    current?.map((p) => (p.id === updated.id ? updated : p)) ??
                    null,
                )
              }
              onDeleted={(id) => {
                setProjects(
                  (current) => current?.filter((p) => p.id !== id) ?? null,
                );
                setSelectedId(null);
              }}
            />
          )}
        </div>
      )}

      {creating && (
        <Modal title="New project" onClose={() => setCreating(false)}>
          <ProjectForm
            submitLabel="Create project"
            onCancel={() => setCreating(false)}
            onSubmit={async (input) => {
              const created = await createProject(input);
              setProjects((current) => [created, ...(current ?? [])]);
              setCreating(false);
            }}
          />
        </Modal>
      )}
    </section>
  );
}
