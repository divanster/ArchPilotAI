import { useState } from "react";
import ProjectForm from "./ProjectForm";
import { Modal, StatusBadge } from "./ui";
import { deleteProject, updateProject, type Project } from "./api";

export type ProjectSection = "documents" | "requirements";

interface Props {
  project: Project;
  onUpdated: (project: Project) => void;
  onDeleted: (id: string) => void;
  onClose: () => void;
  onOpenSection?: (section: ProjectSection, projectId: string) => void;
}

export default function ProjectDetails({
  project,
  onUpdated,
  onDeleted,
  onClose,
  onOpenSection,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDelete() {
    setDeleting(true);
    setError(null);
    try {
      await deleteProject(project.id);
      onDeleted(project.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
      setDeleting(false);
    }
  }

  return (
    <section aria-label="Project details" className="card">
      <h2>{project.name}</h2>
      <p>
        <StatusBadge status={project.status} />
      </p>
      <p>{project.description ?? "No description"}</p>
      <p className="muted">
        Created: {new Date(project.created_at).toLocaleString()}
      </p>
      <p className="muted">
        Updated: {new Date(project.updated_at).toLocaleString()}
      </p>
      {onOpenSection && (
        <div className="row" style={{ marginBottom: "var(--space-3)" }}>
          <button
            type="button"
            onClick={() => onOpenSection("documents", project.id)}
          >
            Open documents
          </button>
          <button
            type="button"
            onClick={() => onOpenSection("requirements", project.id)}
          >
            Open requirements
          </button>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
      {confirmingDelete ? (
        <div role="alertdialog" aria-label="Confirm delete">
          <p>Delete “{project.name}”? This cannot be undone.</p>
          <div className="row">
            <button
              type="button"
              className="danger"
              onClick={handleDelete}
              disabled={deleting}
            >
              {deleting ? "Deleting…" : "Confirm delete"}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingDelete(false)}
              disabled={deleting}
            >
              Keep project
            </button>
          </div>
        </div>
      ) : (
        <div className="row">
          <button type="button" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button type="button" onClick={() => setConfirmingDelete(true)}>
            Delete
          </button>
          <button type="button" className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
      )}
      {editing && (
        <Modal title="Edit project" onClose={() => setEditing(false)}>
          <ProjectForm
            initial={{
              name: project.name,
              description: project.description,
              status: project.status,
            }}
            submitLabel="Save changes"
            onCancel={() => setEditing(false)}
            onSubmit={async (input) => {
              onUpdated(await updateProject(project.id, input));
              setEditing(false);
            }}
          />
        </Modal>
      )}
    </section>
  );
}
