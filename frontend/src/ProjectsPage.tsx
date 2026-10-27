import { useCallback, useEffect, useState } from "react";
import ProjectDetails from "./ProjectDetails";
import ProjectForm from "./ProjectForm";
import { createProject, listProjects, type Project } from "./api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setProjects(null);
    try {
      setProjects(await listProjects());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load projects");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const selected = projects?.find((p) => p.id === selectedId) ?? null;

  return (
    <section aria-label="Projects">
      <h2>Projects</h2>

      <h3>New project</h3>
      <ProjectForm
        submitLabel="Create project"
        onSubmit={async (input) => {
          const created = await createProject(input);
          setProjects((current) => [created, ...(current ?? [])]);
        }}
      />

      {error && (
        <div role="alert">
          <p>{error}</p>
          <button type="button" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {!error && projects === null && <p>Loading projects…</p>}
      {projects && projects.length === 0 && <p>No projects yet.</p>}
      {projects && projects.length > 0 && (
        <ul>
          {projects.map((p) => (
            <li key={p.id}>
              <button type="button" onClick={() => setSelectedId(p.id)}>
                {p.name}
              </button>{" "}
              <span>({p.status})</span>
            </li>
          ))}
        </ul>
      )}

      {selected && (
        <ProjectDetails
          key={selected.id}
          project={selected}
          onClose={() => setSelectedId(null)}
          onUpdated={(updated) =>
            setProjects((current) => current?.map((p) => (p.id === updated.id ? updated : p)) ?? null)
          }
          onDeleted={(id) => {
            setProjects((current) => current?.filter((p) => p.id !== id) ?? null);
            setSelectedId(null);
          }}
        />
      )}
    </section>
  );
}
