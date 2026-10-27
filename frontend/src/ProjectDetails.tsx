import { useState } from "react";
import ProjectForm from "./ProjectForm";
import DocumentsSection from "./DocumentsSection";
import { deleteProject, updateProject, type Project } from "./api";

interface Props {
  project: Project;
  onUpdated: (project: Project) => void;
  onDeleted: (id: string) => void;
  onClose: () => void;
}

export default function ProjectDetails({ project, onUpdated, onDeleted, onClose }: Props) {
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

  if (editing) {
    return (
      <section aria-label="Edit project">
        <h3>Edit project</h3>
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
      </section>
    );
  }

  return (
    <section aria-label="Project details">
      <h3>{project.name}</h3>
      <p>Status: {project.status}</p>
      <p>{project.description ?? "No description"}</p>
      <p>Created: {new Date(project.created_at).toLocaleString()}</p>
      <p>Updated: {new Date(project.updated_at).toLocaleString()}</p>
      <DocumentsSection projectId={project.id} />
      {error && <p role="alert">{error}</p>}
      {confirmingDelete ? (
        <div role="alertdialog" aria-label="Confirm delete">
          <p>Delete “{project.name}”? This cannot be undone.</p>
          <button type="button" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Confirm delete"}
          </button>
          <button type="button" onClick={() => setConfirmingDelete(false)} disabled={deleting}>
            Keep project
          </button>
        </div>
      ) : (
        <>
          <button type="button" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button type="button" onClick={() => setConfirmingDelete(true)}>
            Delete
          </button>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </>
      )}
    </section>
  );
}
