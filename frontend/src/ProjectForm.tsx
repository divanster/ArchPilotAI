import { useId, useState, type FormEvent } from "react";
import { PROJECT_STATUSES, type ProjectInput, type ProjectStatus } from "./api";

interface Props {
  initial?: ProjectInput;
  submitLabel: string;
  onSubmit: (input: ProjectInput) => Promise<void>;
  onCancel?: () => void;
}

export default function ProjectForm({ initial, submitLabel, onSubmit, onCancel }: Props) {
  const uid = useId();
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [status, setStatus] = useState<ProjectStatus>(initial?.status ?? "draft");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit({ name: name.trim(), description: description.trim() || null, status });
      if (!initial) {
        setName("");
        setDescription("");
        setStatus("draft");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <div>
        <label htmlFor={`${uid}-name`}>Name</label>
        <input
          id={`${uid}-name`}
          value={name}
          maxLength={200}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={`${uid}-description`}>Description</label>
        <textarea
          id={`${uid}-description`}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={`${uid}-status`}>Status</label>
        <select
          id={`${uid}-status`}
          value={status}
          onChange={(e) => setStatus(e.target.value as ProjectStatus)}
        >
          {PROJECT_STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={saving}>
        {saving ? "Saving…" : submitLabel}
      </button>
      {onCancel && (
        <button type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      )}
    </form>
  );
}
